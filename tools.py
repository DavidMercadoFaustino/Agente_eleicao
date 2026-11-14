"""Ferramentas do agente (LangChain @tool). Toda a lógica numérica é determinística:
o LLM escolhe a ferramenta e redige; quem calcula é o código.

Ferramentas:
- esquema_banco           : views disponíveis e suas colunas (para o Text-to-SQL)
- consultar_banco         : SQL somente leitura no DuckDB (SELECT/WITH, até 50 linhas)
- resultado_eleicao       : recálculo QE/QP/sobras de uma UF e conferência com o TSE
- explicar_candidato      : por que um candidato foi (ou não) eleito, passo a passo
- buscar_legislacao       : busca semântica na legislação (FAISS + bge-m3), com citação
- ler_artigo              : texto exato de um artigo (sem embeddings)
- votos_por_municipio     : mais votados em um município (busca sem acento)
- eleitos_agremiacao      : eleitos e suplentes de um partido/federação
- perfil_municipio_ibge   : API EXTERNA do IBGE (população, Censo 2022) cruzada com os votos do TSE
- mais_votados_uf         : ranking estadual de candidatos
- analisar_financas       : indicadores de prestação de contas (alerta/indício)
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

import duckdb
from langchain_core.tools import tool

import config
import db
import financas
import rag

MAX_LINHAS = 50
VIEWS_DOCUMENTADAS = {
    "candidatos": "um registro por candidato (uf, sq_candidato, numero, nome, nome_urna, partido, federacao, "
                  "agremiacao, situacao, eleito)",
    "ranking_candidato_uf": "votos nominais válidos totais por candidato na UF (uf, sq_candidato, nome_urna, "
                            "partido, agremiacao, situacao, eleito, votos)",
    "votos_candidato_munzona": "votos do candidato por município e zona (uf, municipio, cd_municipio, zona, "
                               "sq_candidato, nome_urna, partido, agremiacao, votos)",
    "votos_partido_munzona": "votos nominais e de legenda por partido, município e zona",
    "votos_agremiacao_uf": "votos válidos (nominais + legenda) por agremiação na UF",
    "vagas": "número de vagas de Deputado Federal por UF",
    "financas_candidato": "receita, despesa contratada e custo por voto por candidato (se carregado)",
}
EXEMPLOS_SQL = """\
-- 10 mais votados de SP
SELECT nome_urna, partido, votos, situacao FROM ranking_candidato_uf WHERE uf = 'SP' ORDER BY votos DESC LIMIT 10;
-- votos de um candidato por município
SELECT municipio, SUM(votos) AS votos FROM votos_candidato_munzona
WHERE uf = 'BA' AND nome_urna ILIKE '%fulano%' GROUP BY municipio ORDER BY votos DESC LIMIT 10;
-- não eleitos com mais votos que o eleito menos votado
WITH corte AS (SELECT uf, MIN(votos) AS menor_eleito FROM ranking_candidato_uf WHERE eleito GROUP BY uf)
SELECT r.uf, r.nome_urna, r.agremiacao, r.votos FROM ranking_candidato_uf r JOIN corte USING (uf)
WHERE NOT r.eleito AND r.votos > corte.menor_eleito ORDER BY r.votos DESC;
-- critério de eleição: situacao = 'ELEITO POR QP' ou 'ELEITO POR MÉDIA'
SELECT nome_urna, agremiacao, votos, situacao FROM ranking_candidato_uf WHERE uf = 'SP' AND eleito ORDER BY votos LIMIT 1;"""

RE_PROIBIDO = re.compile(r"\b(insert|update|delete|drop|create|alter|attach|detach|copy|export|import|install|"
                         r"load|pragma|set|call|checkpoint|vacuum|read_csv\w*|read_parquet|read_json\w*|glob)\b",
                         re.IGNORECASE)


# ---------------------------------------------------------------- infraestrutura
@lru_cache(maxsize=1)
def _conexao_base() -> duckdb.DuckDBPyConnection:
    return db.conectar()


def _con() -> duckdb.DuckDBPyConnection:
    """Um cursor por uso: no DuckDB a conexão não pode ser compartilhada entre threads (o agente e o
    RunnableParallel executam ferramentas em paralelo); o cursor é uma conexão própria ao mesmo banco."""
    return _conexao_base().cursor()


@lru_cache(maxsize=4)
def _resultado(uf: str):
    return db.recalcular_uf(_con(), uf)


@lru_cache(maxsize=1)
def _indice():
    return rag.carregar_indice()


@lru_cache(maxsize=1)
def _chunks():
    return rag.carregar_chunks()


def _sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn").upper().strip()


def _uf(uf: str) -> str:
    uf = uf.strip().upper()
    if uf not in config.UFS:
        raise ValueError(f"UF fora do escopo: {uf}. Disponíveis: {', '.join(config.UFS)}.")
    return uf


def _fmt(n, casas: int = 0) -> str:
    """Número no padrão brasileiro: 1.234.567,89"""
    return f"{n:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _pct(x: float) -> str:
    return _fmt(100 * x, 1) + "%"


def validar_sql(sql: str) -> str:
    """Aceita só uma consulta SELECT/WITH; acrescenta LIMIT se faltar."""
    s = sql.strip().rstrip(";").strip()
    s = re.sub(r"^```(sql)?|```$", "", s, flags=re.IGNORECASE).strip()
    if ";" in s:
        raise ValueError("Envie apenas uma consulta (sem ';' no meio).")
    if not re.match(r"^(select|with)\b", s, re.IGNORECASE):
        raise ValueError("Somente consultas SELECT ou WITH são permitidas.")
    if RE_PROIBIDO.search(s):
        raise ValueError("A consulta contém um comando não permitido.")
    if not re.search(r"\blimit\s+\d+\s*$", s, re.IGNORECASE):
        s = f"{s}\nLIMIT {MAX_LINHAS}"
    return s


def localizar_candidato(uf: str, termo: str) -> list[tuple]:
    """Busca por nome de urna/nome civil, sem acento e sem caixa. Retorna (sq, nome_urna, agremiacao, votos)."""
    linhas = _con().execute("SELECT r.sq_candidato, r.nome_urna, c.nome, r.agremiacao, r.votos "
                            "FROM ranking_candidato_uf r JOIN candidatos c USING (uf, sq_candidato) WHERE r.uf = ?",
                            [uf]).fetchall()
    t = _sem_acento(termo)
    exatos = [l for l in linhas if t in (_sem_acento(l[1]), _sem_acento(l[2]))]
    if exatos:
        return [(l[0], l[1], l[3], l[4]) for l in exatos]
    partes = t.split()
    contem = sorted([(l[0], l[1], l[3], l[4]) for l in linhas
                     if all(p in _sem_acento(l[1]) + " " + _sem_acento(l[2]) for p in partes)], key=lambda x: -x[3])
    if contem:
        return contem
    # tolerância a erro de digitação: compara cada palavra digitada com as palavras dos nomes
    import difflib
    def nota(l):
        palavras = (_sem_acento(l[1]) + " " + _sem_acento(l[2])).split()
        return min(max((difflib.SequenceMatcher(None, p, w).ratio() for w in palavras), default=0) for p in partes)
    parecidos = sorted(((nota(l), l) for l in linhas), key=lambda x: (-x[0], -x[1][4]))
    return [(l[0], l[1], l[3], l[4]) for n, l in parecidos[:5] if n >= 0.8]


# ---------------------------------------------------------------- lógica (testável sem LLM)
def explicacao_candidato(uf: str, sq: str) -> str:
    resultado, _ = _resultado(uf)
    ranking = _con().execute("SELECT sq_candidato, nome_urna, agremiacao, votos, situacao FROM ranking_candidato_uf "
                             "WHERE uf = ? ORDER BY votos DESC", [uf]).fetchall()
    pos_uf = next(i for i, r in enumerate(ranking, 1) if r[0] == sq)
    _, nome, ag, votos, situacao = ranking[pos_uf - 1]
    lista_ag = [r for r in ranking if r[2] == ag]
    pos_ag = next(i for i, r in enumerate(lista_ag, 1) if r[0] == sq)
    qe = resultado.qe
    votos_ag = dict(_con().execute("SELECT agremiacao, votos_validos FROM votos_agremiacao_uf WHERE uf = ?",
                                   [uf]).fetchall())[ag]
    eleitos = {e.candidato.sq: e for e in resultado.eleitos}
    eleitos_ag = [e for e in resultado.eleitos if e.candidato.agremiacao == ag]
    menor_eleito_uf = min(resultado.eleitos, key=lambda e: e.candidato.votos)

    n_cad = resultado.cadeiras.get(ag, 0)
    n_qp = resultado.qp.get(ag, 0)
    sim = lambda b: "SIM" if b else "NÃO"  # noqa: E731
    base = ["Código Eleitoral, arts. 106 a 108"]
    if sq in eleitos:
        e = eleitos[sq]
        if e.criterio == "QP":
            conclusao = (f"ELEITO pelo quociente partidário: a agremiação teve QP {n_qp} e está em {pos_ag}º lugar na lista "
                         f"da agremiação, dentro das vagas do QP.")
        else:
            fase = "com a barreira 80/20" if e.criterio == "MEDIA_80_20" else "na fase final, sem barreira (STF)"
            conclusao = (f"ELEITO pelas sobras (maior média) {fase}, na rodada {e.rodada}, com média "
                         f"{_fmt(e.media, 2)} da agremiação.")
            base += ["Código Eleitoral, art. 109", "Res.-TSE 23.677/2021, art. 11"]
            if e.criterio == "MEDIA_FINAL":
                base.append("STF, ADI 7.228")
    elif eleitos_ag:
        ultimo = min(eleitos_ag, key=lambda e: e.candidato.votos)
        conclusao = (f"NÃO ELEITO porque a agremiação conquistou {n_cad} cadeira(s) e a candidatura ficou em {pos_ag}º lugar "
                     f"na lista interna; as cadeiras vão aos mais votados da agremiação, e o último eleito dela "
                     f"({ultimo.candidato.nome}) teve {_fmt(ultimo.candidato.votos)} votos. "
                     + ("A candidatura cumpria os mínimos individuais; o que faltou foi vaga para a agremiação."
                        if votos >= 0.2 * qe else
                        "Além disso, a votação individual ficou abaixo dos mínimos indicados em DADOS."))
        base += ["Código Eleitoral, art. 109, § 1º", "Res.-TSE 23.677/2021, art. 11, § 8º"]
    else:
        conclusao = "NÃO ELEITO porque a agremiação não conquistou nenhuma cadeira."
        if votos_ag < 0.8 * qe:
            conclusao += (f" Ela não atingiu o QE (QP 0) nem 80% do QE ({_fmt(0.8 * qe)} votos), então não "
                          f"disputou as sobras com barreira, e não venceu nenhuma média na fase final.")
        base += ["Código Eleitoral, art. 109", "Res.-TSE 23.677/2021, art. 11"]

    if votos >= qe:
        conclusao += (f" SOZINHOS, os votos dele valem {_fmt(votos / qe, 2)} QE = {votos // qe} cadeira(s) de "
                      f"quociente partidário (ele ocupa uma; as demais vão aos seguintes da lista). A agremiação "
                      f"inteira conquistou {n_cad}.")
    l = [f"CONCLUSÃO: {conclusao}",
         "",
         "DADOS:",
         f"- Candidato: {nome} ({ag}), {uf}. Situação oficial no TSE: {situacao}.",
         f"- Votos nominais válidos: {_fmt(votos)} ({pos_uf}º da UF; {pos_ag}º da agremiação).",
         f"- Quociente eleitoral (QE) de {uf}: {_fmt(qe)} = {_fmt(resultado.votos_validos)} votos válidos ÷ "
         f"{resultado.vagas} vagas.",
         f"- Agremiação: {_fmt(votos_ag)} votos válidos = {_fmt(votos_ag / qe, 2)} QE -> QP {n_qp}; "
         f"cadeiras no total (QP + sobras): {n_cad}.",
         f"- Votação individual = {_pct(votos / qe)} do QE"
         + (f" ({_fmt(votos / qe, 2)} vezes o QE)" if votos >= qe else "") + ". Atinge 10% do QE (exigido para vaga por QP)? "
         f"{sim(votos >= 0.1 * qe)}. Atinge 20% do QE (exigido nas sobras com barreira)? {sim(votos >= 0.2 * qe)}.",
         ]
    if votos >= qe:
        sozinho = votos // qe
        l.append(f"- CONTRIBUIÇÃO INDIVIDUAL (puxador de votos): só os votos nominais dele somam "
                 f"{_fmt(votos / qe, 2)} QE, o que equivale a {sozinho} cadeira(s) inteira(s) de quociente "
                 f"partidário (fração desprezada); isso é {_pct(votos / votos_ag)} dos votos válidos da "
                 f"agremiação. As outras {max(n_cad - sozinho, 0)} das {n_cad} cadeira(s) da agremiação vieram dos "
                 f"votos dos demais candidatos, da legenda e das sobras. Cada cadeira é ocupada por um candidato; "
                 f"os votos excedentes ajudam a eleger os seguintes da lista.")
    if sq not in eleitos and votos > menor_eleito_uf.candidato.votos:
        l.append(f"- Teve mais votos que {menor_eleito_uf.candidato.nome} ({menor_eleito_uf.candidato.agremiacao}, "
                 f"{_fmt(menor_eleito_uf.candidato.votos)} votos), o eleito menos votado da UF: no sistema "
                 f"proporcional as cadeiras são primeiro das agremiações, depois dos candidatos.")
    if n_qp > 0:
        aptos = sum(1 for r in lista_ag if r[3] >= 0.1 * qe)
        if aptos < n_qp:
            l.append(f"- Observação: a agremiação tinha QP {n_qp}, mas só {aptos} candidato(s) com 10% do QE; "
                     f"as vagas restantes foram para as sobras (Código Eleitoral, art. 108).")
            base.append("Código Eleitoral, art. 108")
    l.append("BASE LEGAL aplicável: " + "; ".join(dict.fromkeys(base)) + ".")
    return "\n".join(l)


# ---------------------------------------------------------------- ferramentas
@tool
def esquema_banco() -> str:
    """Lista as views do banco eleitoral (Deputado Federal 2026, SP e BA), suas colunas e exemplos de SQL.
    Use antes de escrever uma consulta para consultar_banco."""
    con = _con()
    existentes = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    partes = []
    for v, desc in VIEWS_DOCUMENTADAS.items():
        if v in existentes:
            cols = ", ".join(f"{c} {t}" for c, t, *_ in con.execute(f"DESCRIBE {v}").fetchall())
            partes.append(f"- {v}: {desc}\n  colunas: {cols}")
    obs = ("Observações: uf é 'SP' ou 'BA'; nomes em MAIÚSCULAS e COM acento — compare com "
           "strip_accents(coluna) ILIKE strip_accents('%termo%'). Para ranking por município, prefira a "
           "ferramenta votos_por_municipio.")
    return "Views (DuckDB):\n" + "\n".join(partes) + "\n\nExemplos:\n" + EXEMPLOS_SQL + "\n\n" + obs


@tool
def consultar_banco(sql: str) -> str:
    """Executa UMA consulta SQL somente leitura (SELECT ou WITH, dialeto DuckDB) no banco eleitoral e devolve
    até 50 linhas em formato tabela. Use para contagens, rankings, votos por município/zona e comparações."""
    try:
        s = validar_sql(sql)
        cur = _con().execute(s)
        cols = [d[0] for d in cur.description]
        linhas = cur.fetchmany(MAX_LINHAS)
    except Exception as erro:  # noqa: BLE001 — o erro volta para o LLM corrigir a consulta
        return f"ERRO na consulta: {str(erro).rstrip('.')}. Revise o SQL (use esquema_banco para ver as colunas)."
    if not linhas:
        return "A consulta não retornou linhas."
    cab = " | ".join(cols)
    corpo = "\n".join(" | ".join("" if v is None else str(v) for v in l) for l in linhas)
    return f"{cab}\n{corpo}\n({len(linhas)} linha(s))"


@tool
def resultado_eleicao(uf: str) -> str:
    """Recalcula a distribuição das cadeiras de Deputado Federal de uma UF ('SP' ou 'BA'): votos válidos,
    quociente eleitoral, quociente partidário e cadeiras por agremiação, e confere com o resultado oficial do TSE."""
    try:
        uf = _uf(uf)
    except ValueError as erro:
        return str(erro)
    resultado, comp = _resultado(uf)
    sobras = [e for e in resultado.eleitos if e.criterio != "QP"]
    txt = [resultado.resumo(), f"Eleitos por QP: {len(resultado.eleitos) - len(sobras)}; pelas sobras (média): "
                               f"{len(sobras)}."]
    txt += [f"  sobra {e.rodada}: {e.candidato.nome} ({e.candidato.agremiacao}) — {e.criterio}" for e in sobras]
    ranking = _con().execute("SELECT agremiacao, votos FROM ranking_candidato_uf WHERE uf = ?", [uf]).fetchall()
    for ag, n_qp in resultado.qp.items():
        aptos = sum(1 for a, v in ranking if a == ag and v >= 0.1 * resultado.qe)
        if n_qp > resultado.cadeiras.get(ag, 0):
            txt.append(f"ATENÇÃO — {ag}: QP {n_qp}, mas só {resultado.cadeiras[ag]} cadeira(s), porque apenas {aptos} "
                       f"candidato(s) atingiram a votação mínima de 10% do QE ({_fmt(0.1 * resultado.qe)} votos) "
                       f"exigida para ocupar vaga de QP; as {n_qp - resultado.cadeiras[ag]} vaga(s) não preenchidas "
                       f"foram para as sobras (Código Eleitoral, art. 108).")
    menor = min(resultado.eleitos, key=lambda e: e.candidato.votos)
    criterio = {"QP": "quociente partidário", "MEDIA_80_20": "sobras com barreira 80/20",
                "MEDIA_FINAL": "sobras na fase final (sem barreira)"}[menor.criterio]
    txt.append(f"Eleito menos votado: {menor.candidato.nome} ({menor.candidato.agremiacao}), "
               f"{_fmt(menor.candidato.votos)} votos, eleito por {criterio}.")
    txt.append("Os demais eleitos (fora da lista de sobras acima) entraram pelo quociente partidário.")
    txt.append(f"Conferência com o TSE: {comp['coincidentes']}/{comp['eleitos_oficiais']} eleitos coincidentes"
               + (" (resultado idêntico)." if comp["acerto_total"] else
                  f"; divergências: só no cálculo {comp['so_no_calculo']}, só no oficial {comp['so_no_oficial']}."))
    return "\n".join(txt)


@tool
def explicar_candidato(uf: str, candidato: str) -> str:
    """Explica, com os números do cálculo oficial, por que um candidato a Deputado Federal foi ou não eleito
    (votos, posição, QE, QP da agremiação, cláusulas de 10%/80%/20%, comparação com o eleito menos votado).
    Parâmetros: uf ('SP' ou 'BA') e o nome de urna (ou parte do nome) do candidato."""
    try:
        uf = _uf(uf)
    except ValueError as erro:
        return str(erro)
    achados = localizar_candidato(uf, candidato)
    if not achados:
        return f"Nenhum candidato encontrado em {uf} para '{candidato}'."
    aviso = ""
    if not any(_sem_acento(candidato).split()[0] in _sem_acento(a[1]) for a in achados[:1]) and len(achados) >= 1:
        aviso = f"(Nome '{candidato}' não encontrado exatamente; interpretado como {achados[0][1]}.)\n"
        if len(achados) > 1 and _sem_acento(achados[0][1]) != _sem_acento(achados[1][1]):
            opcoes = "; ".join(f"{n} ({a}, {_fmt(v)} votos)" for _, n, a, v in achados[:5])
            return f"Nenhum candidato com o nome exato '{candidato}' em {uf}. Nomes parecidos: {opcoes}. Pergunte qual é."
        return aviso + explicacao_candidato(uf, achados[0][0])
    if len(achados) > 1 and _sem_acento(achados[0][1]) != _sem_acento(candidato):
        opcoes = "; ".join(f"{n} ({a}, {_fmt(v)} votos)" for _, n, a, v in achados[:8])
        return f"Mais de um candidato corresponde a '{candidato}' em {uf}: {opcoes}. Pergunte qual é."
    return explicacao_candidato(uf, achados[0][0])


@tool
def buscar_legislacao(pergunta: str, k: int = 4) -> str:
    """Busca trechos da legislação eleitoral (Código Eleitoral, Lei 9.504/97, Resoluções do TSE 23.609, 23.677,
    23.748 e acórdão do STF na ADI 7.228) relevantes para a pergunta. Cada trecho vem com a citação
    [fonte, artigo]; cite-a na resposta."""
    try:
        return rag.formatar(_indice().similarity_search_with_score(pergunta, k=min(max(k, 1), 8)))
    except Exception as erro:  # noqa: BLE001
        return f"ERRO na busca da legislação: {erro}"


@tool
def ler_artigo(norma: str, artigo: str) -> str:
    """Retorna o texto integral de um artigo. norma: parte do nome da fonte (ex.: 'Código Eleitoral', '9.504',
    '23.677'); artigo: número (ex.: '109', '12-A'). Use quando a pergunta citar um artigo específico."""
    docs = rag.buscar_artigo(norma, artigo, list(_chunks()))
    return rag.formatar(docs) if docs else f"Artigo {artigo} não encontrado em '{norma}'."


@tool
def mais_votados_uf(uf: str, n: int = 10) -> str:
    """Ranking ESTADUAL: os candidatos a Deputado Federal mais votados em todo o estado ('SP' ou 'BA').
    Use quando a pergunta fala do estado ("mais votados em SP", "na Bahia"). Para uma cidade, use
    votos_por_municipio. n = quantos candidatos (até 30)."""
    try:
        uf = _uf(uf)
    except ValueError as erro:
        return str(erro)
    linhas = _con().execute("SELECT nome_urna, agremiacao, votos, situacao FROM ranking_candidato_uf "
                            "WHERE uf = ? ORDER BY votos DESC LIMIT ?", [uf, min(max(int(n), 1), 30)]).fetchall()
    return f"Mais votados para Deputado Federal no ESTADO {uf} (todos os municípios):\n" + "\n".join(
        f"{i}. {nome} ({ag}) — {_fmt(v)} votos — situação: {sit}" for i, (nome, ag, v, sit) in enumerate(linhas, 1))


@tool
def eleitos_agremiacao(uf: str, agremiacao: str) -> str:
    """Lista os eleitos de um partido ou federação na UF, do mais ao menos votado, com o critério de eleição
    (quociente partidário ou média), além dos primeiros suplentes. Use para "quem foram os eleitos do PL",
    "último eleito do PT", "primeiro suplente". agremiacao: sigla do partido (ex.: 'PL', 'PT') ou parte do
    nome da federação (ex.: 'PSOL REDE')."""
    try:
        uf = _uf(uf)
    except ValueError as erro:
        return str(erro)
    con = _con()
    linhas = con.execute("SELECT nome_urna, partido, agremiacao, votos, situacao, eleito FROM ranking_candidato_uf "
                         "WHERE uf = ? ORDER BY votos DESC", [uf]).fetchall()
    alvo = _sem_acento(agremiacao)
    ags = sorted({l[2] for l in linhas if alvo == _sem_acento(l[1]) or alvo == _sem_acento(l[2])}) or \
        sorted({l[2] for l in linhas if alvo in _sem_acento(l[2])})
    if not ags:
        return f"Partido/federação '{agremiacao}' não encontrado em {uf}."
    if len(ags) > 1:
        return f"Mais de uma agremiação corresponde a '{agremiacao}': {'; '.join(ags)}. Qual delas?"
    ag = ags[0]
    lista = [l for l in linhas if l[2] == ag]
    eleitos = [l for l in lista if l[5]]
    suplentes = [l for l in lista if not l[5]][:3]
    resultado, _ = _resultado(uf)
    txt = [f"{ag} ({uf}): {len(eleitos)} eleito(s); QP {resultado.qp.get(ag, 0)}."]
    if ag != lista[0][1] and any(l[1] != ag for l in lista):
        txt.append(f"(É uma federação: partidos {', '.join(sorted({l[1] for l in lista}))}.)")
    txt += [f"{i}. {n} ({p}) — {_fmt(v)} votos — {s}" for i, (n, p, _, v, s, _) in enumerate(eleitos, 1)]
    if eleitos:
        u = eleitos[-1]
        txt.append(f"Último (menos votado) eleito da agremiação: {u[0]} ({u[1]}), {_fmt(u[3])} votos, {u[4]}.")
    if suplentes:
        txt.append("Primeiros suplentes: " + "; ".join(f"{n} ({p}), {_fmt(v)} votos" for n, p, _, v, _, _ in suplentes))
    return "\n".join(txt)


@tool
def votos_por_municipio(uf: str, municipio: str, n: int = 10) -> str:
    """Ranking dos candidatos a Deputado Federal mais votados em UM MUNICÍPIO (cidade) de SP ou BA — só quando a
    pergunta citar a cidade. Para o estado inteiro, use mais_votados_uf.
    Aceita o nome com ou sem acento (ex.: 'Luis Eduardo Magalhaes'). n = quantos candidatos (até 30)."""
    try:
        uf = _uf(uf)
    except ValueError as erro:
        return str(erro)
    con = _con()
    municipios = [r[0] for r in con.execute(
        "SELECT DISTINCT municipio FROM votos_candidato_munzona WHERE uf = ?", [uf]).fetchall()]
    alvo = _sem_acento(municipio).replace("MUNICIPIO DE ", "").strip()
    achados = [m for m in municipios if _sem_acento(m) == alvo] or [m for m in municipios if alvo in _sem_acento(m)]
    if not achados:
        import difflib
        perto = difflib.get_close_matches(alvo, [_sem_acento(m) for m in municipios], n=5, cutoff=0.6)
        return f"Município '{municipio}' não encontrado em {uf}." + (f" Parecidos: {', '.join(perto)}." if perto else "")
    if len(achados) > 1:
        return f"Mais de um município corresponde a '{municipio}': {', '.join(achados[:10])}. Qual deles?"
    m = achados[0]
    linhas = con.execute("""
        SELECT v.nome_urna, v.agremiacao, SUM(v.votos) AS votos, c.situacao
        FROM votos_candidato_munzona v JOIN candidatos c USING (uf, sq_candidato)
        WHERE v.uf = ? AND v.municipio = ? GROUP BY ALL ORDER BY votos DESC LIMIT ?""",
                         [uf, m, min(max(int(n), 1), 30)]).fetchall()
    total = con.execute("SELECT SUM(votos) FROM votos_candidato_munzona WHERE uf = ? AND municipio = ?",
                        [uf, m]).fetchone()[0]
    corpo = "\n".join(f"{i}. {nome} ({ag}) — {_fmt(v)} votos — situação: {sit}"
                      for i, (nome, ag, v, sit) in enumerate(linhas, 1))
    return (f"MUNICÍPIO de {m} ({uf}) — só os votos dados nesta cidade, não no estado. Total de votos nominais "
            f"válidos para Deputado Federal: {_fmt(total)}\n{corpo}")


# ---------------------------------------------------------------- API externa (IBGE)
URL_MUNICIPIOS = "https://servicodados.ibge.gov.br/api/v1/localidades/estados/{uf}/municipios"
URL_POPULACAO = ("https://servicodados.ibge.gov.br/api/v3/agregados/4709/periodos/2022/variaveis/93"
                 "?localidades=N6[{codigo}]")   # Censo 2022, tabela 4709, variável 93 = população residente


def _ibge_get(url: str):
    """GET na API pública do IBGE (separado para poder ser simulado nos testes)."""
    import json
    import urllib.request
    import gzip
    req = urllib.request.Request(url, headers={"User-Agent": "agente-eleitoral-2026", "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=20) as r:
        corpo = r.read()
    if corpo[:2] == b"\x1f\x8b":          # a API do IBGE responde compactada (gzip)
        corpo = gzip.decompress(corpo)
    return json.loads(corpo.decode("utf-8"))


@lru_cache(maxsize=4)
def _municipios_ibge(uf: str) -> dict[str, tuple[int, str]]:
    return {_sem_acento(m["nome"]): (int(m["id"]), m["nome"]) for m in _ibge_get(URL_MUNICIPIOS.format(uf=uf))}


@tool
def perfil_municipio_ibge(uf: str, municipio: str) -> str:
    """Consulta a API do IBGE (externa) e cruza com o TSE: população residente do município no Censo 2022,
    total de votos nominais válidos para Deputado Federal, votos por 100 habitantes e o mais votado da cidade.
    Use para perguntas sobre população, tamanho do município ou votos em relação à população."""
    try:
        uf = _uf(uf)
    except ValueError as erro:
        return str(erro)
    try:
        mapa = _municipios_ibge(uf)
        alvo = _sem_acento(municipio).replace("MUNICIPIO DE ", "").strip()
        if alvo not in mapa:
            import difflib
            perto = difflib.get_close_matches(alvo, list(mapa), n=1, cutoff=0.8)
            if not perto:
                return f"Município '{municipio}' não encontrado no IBGE para {uf}."
            alvo = perto[0]
        codigo, nome = mapa[alvo]
        dados = _ibge_get(URL_POPULACAO.format(codigo=codigo))
        populacao = int(next(iter(dados[0]["resultados"][0]["series"][0]["serie"].values())))
    except Exception as erro:  # noqa: BLE001 — API externa: falha vira mensagem, não exceção
        return f"API do IBGE indisponível no momento ({erro.__class__.__name__}). Tente mais tarde."
    con = _con()
    municipios_tse = {_sem_acento(m): m for (m,) in con.execute(
        "SELECT DISTINCT municipio FROM votos_candidato_munzona WHERE uf = ?", [uf]).fetchall()}
    m_tse = municipios_tse.get(alvo)
    linhas = [f"{nome} ({uf}) — código IBGE {codigo}.",
              f"População residente (IBGE, Censo 2022): {_fmt(populacao)} habitantes."]
    if m_tse:
        total, = con.execute("SELECT SUM(votos) FROM votos_candidato_munzona WHERE uf = ? AND municipio = ?",
                             [uf, m_tse]).fetchone()
        top = con.execute("SELECT nome_urna, agremiacao, SUM(votos) v FROM votos_candidato_munzona "
                          "WHERE uf = ? AND municipio = ? GROUP BY ALL ORDER BY v DESC LIMIT 1",
                          [uf, m_tse]).fetchone()
        linhas += [f"Votos nominais válidos para Deputado Federal (TSE, 2026): {_fmt(total)}.",
                   f"Votos por 100 habitantes: {_fmt(100 * total / populacao, 1)} "
                   f"(a população inclui menores de 16 anos e não eleitores).",
                   f"Mais votado na cidade: {top[0]} ({top[1]}), {_fmt(top[2])} votos."]
    else:
        linhas.append("Município não encontrado nos dados do TSE carregados.")
    linhas.append("Fontes: IBGE (API de agregados, tabela 4709) e TSE (dados abertos).")
    return "\n".join(linhas)


@tool
def analisar_financas(uf: str, candidato: str) -> str:
    """Indicadores da prestação de contas de um candidato: receita, despesa contratada, custo por voto e % do
    limite de gastos. Retorna ALERTA (>= 90% do limite) ou INDÍCIO (> 100%) — sinais para verificação, nunca
    conclusão de irregularidade."""
    try:
        uf = _uf(uf)
    except ValueError as erro:
        return str(erro)
    existe = _con().execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name = "
                            "'financas_candidato'").fetchone()[0]
    if not existe:
        return "Os dados de prestação de contas ainda não foram carregados no banco; não é possível analisar."
    achados = localizar_candidato(uf, candidato)
    if not achados:
        return f"Nenhum candidato encontrado em {uf} para '{candidato}'."
    cur = _con().execute("SELECT * FROM financas_candidato WHERE sq_candidato = ?", [achados[0][0]])
    cols = [d[0] for d in cur.description]
    linha = cur.fetchone()
    if not linha:
        return f"Sem prestação de contas para {achados[0][1]}."
    return financas.analisar_linha(dict(zip(cols, linha))).texto()


FERRAMENTAS = [esquema_banco, consultar_banco, resultado_eleicao, explicar_candidato, mais_votados_uf,
               eleitos_agremiacao,
               votos_por_municipio, perfil_municipio_ibge,
               buscar_legislacao, ler_artigo, analisar_financas]


if __name__ == "__main__":
    # demonstração de cada ferramenta, sem LLM
    exemplos = [
        (esquema_banco, {}),
        (consultar_banco, {"sql": "SELECT nome_urna, agremiacao, votos FROM ranking_candidato_uf "
                                  "WHERE uf = 'SP' AND NOT eleito ORDER BY votos DESC LIMIT 5"}),
        (consultar_banco, {"sql": "DROP VIEW candidatos"}),
        (resultado_eleicao, {"uf": "BA"}),
        (explicar_candidato, {"uf": "SP", "candidato": "Rui Falcão"}),
        (ler_artigo, {"norma": "23.677", "artigo": "12-A"}),
        (votos_por_municipio, {"uf": "BA", "municipio": "Luis Eduardo Magalhaes", "n": 5}),
        (eleitos_agremiacao, {"uf": "SP", "agremiacao": "PL"}),
        (perfil_municipio_ibge, {"uf": "BA", "municipio": "Luís Eduardo Magalhães"}),
        (buscar_legislacao, {"pergunta": "o que acontece se nenhum partido atingir o quociente eleitoral?", "k": 2}),
        (analisar_financas, {"uf": "SP", "candidato": "Rui Falcão"}),
    ]
    for ferramenta, args in exemplos:
        print(f"\n{'=' * 80}\n>>> {ferramenta.name}({args})\n{'-' * 80}")
        saida = ferramenta.invoke(args)
        print(saida if len(saida) < 2500 else saida[:2500] + "\n[...]")
    print("\n[FERRAMENTAS] OK")
