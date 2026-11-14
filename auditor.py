"""Verificação cognitiva da resposta (determinística, sem LLM).

Confere a resposta final contra as saídas das ferramentas da mesma pergunta:
1. Números: todo número relevante da resposta (>= 1.000, decimal ou percentual) precisa aparecer em
   alguma saída de ferramenta (tolerância de arredondamento) ou na própria pergunta.
2. Citações: todo [fonte, art. N] da resposta precisa ter vindo de uma ferramenta.
3. Ancoragem: resposta com números ou citações sem nenhuma ferramenta chamada é rejeitada.
4. Linguagem de prestação de contas: proíbe afirmar fraude/crime/irregularidade.

Se houver problema, o agente recebe a lista e reescreve a resposta uma vez (ver agente.perguntar).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# números de normas, anos e afins que podem aparecer sem vir das ferramentas
NUMEROS_LIVRES = {4737, 9504, 9096, 23609, 23677, 23748, 23734, 23607, 23766, 7228, 7263, 7325, 14211, 5420,
                  1965, 1995, 1997, 2019, 2021, 2022, 2024, 2025, 2026}

RE_NUM_BR = re.compile(r"(?<![\w.,])(\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+,\d+|\d{4,})(\s*%)?(?![\w])")
RE_NUM_PONTO = re.compile(r"(?<![\w.,])(\d+\.\d+)(?![\w.])")          # 0.315 (saída de SQL)
RE_PCT_INT = re.compile(r"(?<![\w.,])(\d{1,3})\s*%")
RE_COLCHETE = re.compile(r"\[([^\[\]]{3,200})\]")
RE_PROIBIDO = re.compile(r"\b(fraude|crime|ilegal|irregularidade|irregular)\b", re.IGNORECASE)

FONTES = [
    ("Código Eleitoral", re.compile(r"C[óo]digo Eleitoral|CE/1965|Lei\s*(?:n[º°.]*\s*)?4\.737", re.IGNORECASE)),
    ("Lei 9.504", re.compile(r"Lei das Elei[çc][õo]es|Lei\s*(?:n[º°.]*\s*)?9\.504", re.IGNORECASE)),
    ("Res", re.compile(r"Res(?:olu[çc][ãa]o)?\.?(?:\s*-?\s*TSE)?\s*(?:n[º°.]*\s*)?(23\.\d{3})", re.IGNORECASE)),
    ("ADI", re.compile(r"\bADIs?\b", re.IGNORECASE)),
]
_ART = r"(\d+)(?:\s?[º°o])?(?:-([A-Z])|([A-Z])\b)?"   # 108, 6º-A, 12A (sufixo só maiúsculo)
RE_ARTS = re.compile(r"[\s,;:(]*(?:[^\[\]\n]{0,25}?)\b[Aa]rts?\.\s*" + _ART +
                     r"((?:\s*(?:,|e|a)\s*\d+(?:\s?[º°o])?(?:-[A-Z]|[A-Z]\b)?)*)")


def _valor_br(txt: str) -> float:
    return float(txt.replace(".", "").replace(",", "."))


def numeros_resposta(texto: str) -> list[tuple[str, float]]:
    """Números relevantes de um texto em português (ignora os de dentro de citações)."""
    sem_cit = RE_COLCHETE.sub(" ", texto)
    achados = []
    for m in RE_NUM_BR.finditer(sem_cit):
        bruto, pct = m.group(1), m.group(2)
        v = _valor_br(bruto) if ("," in bruto or "." in bruto) else float(bruto)
        if not pct and v in NUMEROS_LIVRES:
            continue
        achados.append((bruto + (pct or "").strip(), v))
    for m in RE_PCT_INT.finditer(sem_cit):
        if not RE_NUM_BR.match(sem_cit, m.start()):
            achados.append((m.group(0).replace(" ", ""), float(m.group(1))))
    return achados


def numeros_fonte(texto: str) -> set[float]:
    """Todos os números das saídas das ferramentas, nos formatos BR e de SQL."""
    vals = set()
    for m in RE_NUM_BR.finditer(texto):
        b = m.group(1)
        vals.add(_valor_br(b) if ("," in b or "." in b) else float(b))
    vals |= {float(m.group(1)) for m in RE_NUM_PONTO.finditer(texto)}
    vals |= {float(x) for x in re.findall(r"(?<![\d.,])\d+(?![\d])", texto)}
    vals |= {v * 100 for v in list(vals) if 0 < v < 1}                   # 0.315 -> 31,5%
    return vals


def _confere(v: float, fonte: set[float]) -> bool:
    return any(abs(v - f) <= max(0.051, abs(f) * 0.006) for f in fonte)


def _expandir(inicio: str, sufixo: str | None, resto: str) -> list[str]:
    arts = [inicio + (f"-{sufixo}" if sufixo else "")]
    partes = re.findall(r"(,|e|a)\s*(\d+)(?:\s?[º°o])?(-[A-Z]|[A-Z]\b)?", resto or "")
    anterior = int(inicio)
    for sep, num, suf in partes:
        if sep.lower() == "a" and not suf:
            arts += [str(i) for i in range(anterior + 1, int(num) + 1)]
        else:
            arts.append(num + (f"-{suf.strip('-').upper()}" if suf else ""))
        anterior = int(num)
    return arts


def citacoes(texto: str) -> set[tuple[str, str]]:
    """Pares (fonte, artigo) mencionados. Artigo '' quando a citação é só da norma (ex.: ADI)."""
    pares = set()
    for nome, rx in FONTES:
        for m in rx.finditer(texto):
            chave = f"Res {m.group(1)}" if nome == "Res" else nome
            seguinte = RE_ARTS.match(texto, m.end())
            if seguinte and nome != "ADI":
                for a in _expandir(seguinte.group(1), seguinte.group(2) or seguinte.group(3), seguinte.group(4)):
                    pares.add((chave, a))
            else:
                pares.add((chave, ""))
    return pares


@dataclass
class Verificacao:
    ok: bool
    problemas: list[str] = field(default_factory=list)
    numeros_checados: int = 0
    citacoes_checadas: int = 0

    def resumo(self) -> str:
        return "ok" if self.ok else f"{len(self.problemas)} problema(s)"


def verificar(resposta: str, saidas_ferramentas: list[str], pergunta: str = "") -> Verificacao:
    fonte_txt = "\n".join(saidas_ferramentas)
    fonte_nums = numeros_fonte(fonte_txt) | numeros_fonte(pergunta)
    problemas = []

    nums = numeros_resposta(resposta)
    sem_fonte = [bruto for bruto, v in nums if not _confere(v, fonte_nums)]
    if sem_fonte:
        problemas.append("Valores que não aparecem nas saídas das ferramentas: "
                         + ", ".join(dict.fromkeys(sem_fonte)) + ".")

    cit_resp = set()
    for m in RE_COLCHETE.finditer(resposta):
        cit_resp |= citacoes(m.group(1))
    cit_fonte = citacoes(fonte_txt)
    normas_fonte = {f for f, _ in cit_fonte}
    nao_vieram = sorted(c for c in cit_resp
                        if (c[1] and c not in cit_fonte) or (not c[1] and c[0] not in normas_fonte))
    if nao_vieram:
        problemas.append("Citações que não vieram das ferramentas: "
                         + "; ".join(f"{f}, art. {a}" if a else f for f, a in nao_vieram) + ".")

    if not saidas_ferramentas and (nums or cit_resp):
        problemas.append("A resposta traz números ou citações, mas nenhuma ferramenta foi consultada.")

    # 5. Coerência com a CONCLUSÃO entregue pela ferramenta (pega raciocínio invertido)
    for concl in re.findall(r"CONCLUSÃO:\s*(.+)", fonte_txt):
        obrig = [b for b, v in numeros_resposta(concl) if "," in b or v >= 1000]
        faltam = [b for b in obrig if not any(_confere(v, {_valor_br(b.rstrip('%'))}) for _, v in nums)]
        if faltam:
            problemas.append("A resposta omite números da CONCLUSÃO da ferramenta: " + ", ".join(faltam)
                             + ". A conclusão da ferramenta deve ser mantida.")
        negativa = re.search(r"n[ãa]o (?:foi )?eleit|suplente", resposta, re.IGNORECASE)
        if concl.startswith("NÃO ELEITO") and not negativa:
            problemas.append("A ferramenta concluiu NÃO ELEITO, mas a resposta não diz isso.")
        if concl.startswith("ELEITO") and re.search(r"n[ãa]o foi eleit", resposta, re.IGNORECASE):
            problemas.append("A ferramenta concluiu ELEITO, mas a resposta diz que não foi eleito.")
        if "SOZINHOS" in concl and re.search(r"(nenhuma|n[ãa]o conseguiu|n[ãa]o obteve)[^.]{0,40}cadeira",
                                             resposta, re.IGNORECASE):
            problemas.append("A resposta contradiz a CONCLUSÃO: os votos do candidato sozinhos renderam cadeiras.")

    # 6. Links: o agente não tem ferramenta que devolva URL; qualquer link é inventado
    urls = [u for u in re.findall(r"https?://[^\s)\]]+", resposta) if u not in fonte_txt]
    if urls:
        problemas.append("Links que não vieram das ferramentas (remova-os): " + ", ".join(urls[:3]) + ".")

    for frase in re.split(r"(?<=[.!?])\s+", resposta):
        if RE_PROIBIDO.search(frase) and not re.search(r"\bn[ãa]o\b|\bnunca\b|\bsem\b", frase, re.IGNORECASE):
            problemas.append(f"Linguagem não permitida (use 'alerta' ou 'indício'): \"{frase.strip()[:120]}\"")

    return Verificacao(not problemas, problemas, len(nums), len(cit_resp))


def mensagem_de_correcao(v: Verificacao) -> str:
    return ("[VERIFICAÇÃO AUTOMÁTICA] A resposta anterior não passou na conferência:\n- "
            + "\n- ".join(v.problemas)
            + "\nReescreva a resposta completa usando somente números e citações que aparecem nas saídas das "
              "ferramentas (chame as ferramentas de novo se precisar). Remova o que não puder confirmar.")
