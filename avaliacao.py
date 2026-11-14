"""Avaliação do agente: respostas (fatos do gabarito), trajetória (ferramentas) e verificação.

Para cada pergunta do conjunto:
- fatos: todos os padrões do gabarito precisam aparecer na resposta (sem acento/caixa; regex permitido);
- proibidos: padrões que não podem aparecer (ex.: afirmar que foi eleito quem não foi);
- trajetória: cada grupo de ferramentas esperado precisa ter ao menos uma chamada (modo "superset":
  chamadas extras são permitidas); em perguntas fora de escopo, nenhuma ferramenta pode ser chamada;
- verificação: resultado do auditor e se a resposta precisou ser reescrita;
- juiz (opcional, --juiz): LLM-juiz com saída estruturada Literal["CORRETO","PARCIAL","INCORRETO"].

Saídas em evidencias/: JSONL com tudo (respostas, chamadas, tempos) e um resumo em Markdown.
Se LANGSMITH_TRACING=true e LANGSMITH_API_KEY estiver no .env, as execuções também vão para o LangSmith.

Uso: python avaliacao.py [--modelo openai/gpt-oss-20b] [--juiz] [--ids 1,2,3]
"""
from __future__ import annotations

import argparse
import json
import re
import time
import unicodedata
import uuid
from datetime import datetime
from typing import Literal

from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, Field

import agente
import config

DIR_EVID = config.RAIZ / "evidencias"

# grupos de ferramentas: cada tupla = alternativas aceitas para aquele passo
CONJUNTO = [
    {"id": 1, "categoria": "dados (SQL)", "pergunta": "Quais foram os 3 candidatos a deputado federal mais votados em SP?",
     "fatos": ["pavanato", "erika hilton", "samia bomfim"], "ferramentas": [("mais_votados_uf", "consultar_banco")]},
    {"id": 2, "categoria": "dados (município)", "pergunta": "Quais os 5 mais votados no município de Luís Eduardo Magalhães?",
     "fatos": ["oziel oliveira", r"11\.615"], "ferramentas": [("votos_por_municipio", "consultar_banco")]},
    {"id": 3, "categoria": "dados (agremiação)", "pergunta": "Quem foi o último eleito do PL em São Paulo?",
     "fatos": ["gil diniz", r"63\.398"], "ferramentas": [("eleitos_agremiacao", "consultar_banco")]},
    {"id": 4, "categoria": "cálculo", "pergunta": "Qual foi o quociente eleitoral de deputado federal em SP?",
     "fatos": [r"338\.203"], "ferramentas": [("resultado_eleicao", "explicar_candidato", "consultar_banco")]},
    {"id": 5, "categoria": "cálculo", "pergunta": "Quem foi o eleito menos votado na Bahia e por qual critério entrou?",
     "fatos": ["felix mendonca", r"60\.159", r"sobra|media"], "proibidos": [r"quociente partidario \(qp\)\b.*felix"],
     "ferramentas": [("resultado_eleicao", "consultar_banco", "explicar_candidato")]},
    {"id": 6, "categoria": "explicação", "pergunta": "Por que o Rui Falcão não foi eleito em SP?",
     "fatos": [r"\b11\b", r"12", r"109\.548"], "ferramentas": [("explicar_candidato",)]},
    {"id": 7, "categoria": "explicação", "pergunta": "Quantas cadeiras os votos do Lucas Pavanato renderam sozinhos?",
     "fatos": [r"8,98", r"\b8\b"], "proibidos": [r"nenhuma cadeira", r"19 cadeiras sozinho"],
     "ferramentas": [("explicar_candidato",)]},
    {"id": 8, "categoria": "explicação", "pergunta": "Por que a Federação PSOL REDE ficou com menos cadeiras que o seu quociente partidário em SP?",
     "fatos": [r"\b6\b", r"\b9\b", r"10 ?%"], "ferramentas": [("resultado_eleicao", "explicar_candidato", "eleitos_agremiacao", "consultar_banco")]},
    {"id": 9, "categoria": "legislação", "pergunta": "O que acontece se nenhum partido atingir o quociente eleitoral?",
     "fatos": [r"12-?a|111", r"media"], "ferramentas": [("buscar_legislacao", "ler_artigo")]},
    {"id": 10, "categoria": "legislação", "pergunta": "O que diz o art. 108 do Código Eleitoral?",
     "fatos": [r"10 ?%", r"quociente eleitoral"], "ferramentas": [("ler_artigo", "buscar_legislacao")]},
    {"id": 11, "categoria": "legislação", "pergunta": "Qual o critério de desempate quando dois partidos têm a mesma média nas sobras?",
     "fatos": [r"(partido|federac|agremiac)[^.]{0,40}maior votacao|maior votacao[^.]{0,30}(partido|federac|agremiac)"],
     "ferramentas": [("buscar_legislacao", "ler_artigo")]},
    {"id": 12, "categoria": "finanças", "pergunta": "Qual o custo por voto do Lucas Pavanato?",
     "fatos": [r"nao (foram|estao|esta)|ainda nao|indisponi|nao e possivel"], "proibidos": [r"r\$ ?\d"],
     "ferramentas": [("analisar_financas",)]},
    {"id": 13, "categoria": "fora de escopo", "pergunta": "Quem foi o deputado federal mais votado no Rio de Janeiro?",
     "fatos": [r"nao (cubro|cobre|abrange|tenho|possuo)|apenas|somente|so (sp|sao paulo)"], "ferramentas": [], "nenhuma": True},
    {"id": 14, "categoria": "memória (2 turnos)", "pergunta": "E quem ficou com a última vaga da federação dele?",
     "contexto": ["Por que o Rui Falcão não foi eleito em SP?"],
     "fatos": [r"joao paulo cunha"], "ferramentas": [("explicar_candidato", "eleitos_agremiacao", "consultar_banco")]},
]


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower()


class Julgamento(BaseModel):
    """Saída estruturada do juiz (Literal evita o bug "CORRETO" in "INCORRETO")."""
    veredito: Literal["CORRETO", "PARCIAL", "INCORRETO"]
    justificativa: str = Field(description="uma frase")


def julgar(llm_juiz, item: dict, resposta: str) -> Julgamento:
    prompt = (f"Você avalia respostas de um assistente eleitoral.\nPergunta: {item['pergunta']}\n"
              f"Fatos que a resposta correta precisa conter: {item['fatos']}\n"
              f"Afirmações proibidas: {item.get('proibidos', [])}\n\nResposta do assistente:\n{resposta}\n\n"
              "CORRETO se contém os fatos e não tem erro; PARCIAL se falta algo ou há imprecisão menor; "
              "INCORRETO se erra o essencial.")
    return llm_juiz.with_structured_output(Julgamento).invoke(prompt)


def avaliar_item(ag, item: dict) -> dict:
    thread = str(uuid.uuid4())
    for previa in item.get("contexto", []):
        agente.perguntar(ag, previa, thread)
    r = agente.perguntar(ag, item["pergunta"], thread)
    texto = _norm(r.texto)
    faltando = [f for f in item["fatos"] if not re.search(f, texto)]
    violados = [p for p in item.get("proibidos", []) if re.search(p, texto)]
    chamadas = set(r.ferramentas)
    if item.get("nenhuma"):
        traj_ok = not chamadas
    else:
        traj_ok = all(any(f in chamadas for f in grupo) for grupo in item["ferramentas"])
    return {"id": item["id"], "categoria": item["categoria"], "pergunta": item["pergunta"],
            "resposta": r.texto, "resposta_original": r.texto_original, "ferramentas": r.ferramentas,
            "chamadas": r.chamadas, "fatos_ok": not faltando and not violados, "fatos_faltando": faltando,
            "proibidos_encontrados": violados, "trajetoria_ok": traj_ok,
            "verificacao_ok": bool(r.verificacao and r.verificacao.ok), "revisada": r.revisada,
            "problemas_verificacao": r.verificacao.problemas if r.verificacao else [], "segundos": r.segundos}


def resumo_markdown(modelo: str, linhas: list[dict]) -> str:
    n = len(linhas)
    pct = lambda k: sum(1 for l in linhas if l[k]) / n  # noqa: E731
    tempo = sum(l["segundos"] for l in linhas) / n
    md = [f"# Avaliação — {modelo} — {datetime.now():%d/%m/%Y %H:%M}", "",
          f"- Respostas corretas (fatos do gabarito): **{pct('fatos_ok'):.0%}**",
          f"- Trajetória correta (ferramentas esperadas): **{pct('trajetoria_ok'):.0%}**",
          f"- Aprovadas na verificação final: **{pct('verificacao_ok'):.0%}** "
          f"(reescritas pelo verificador: {sum(l['revisada'] for l in linhas)})",
          f"- Tempo médio por pergunta: **{tempo:.0f} s**"]
    if any("juiz" in l for l in linhas):
        md.append(f"- Juiz LLM: " + ", ".join(f"{v}: {sum(1 for l in linhas if l.get('juiz') == v)}"
                                             for v in ("CORRETO", "PARCIAL", "INCORRETO")))
    md += ["", "| # | Categoria | Fatos | Trajetória | Verif. | Revisada | Ferramentas | s |", "|---|---|---|---|---|---|---|---|"]
    ok = lambda b: "ok" if b else "X"  # noqa: E731
    for l in linhas:
        md.append(f"| {l['id']} | {l['categoria']} | {ok(l['fatos_ok'])} | {ok(l['trajetoria_ok'])} | "
                  f"{ok(l['verificacao_ok'])} | {'sim' if l['revisada'] else ''} | "
                  f"{' → '.join(l['ferramentas']) or '—'} | {l['segundos']:.0f} |")
    falhas = [l for l in linhas if not (l["fatos_ok"] and l["trajetoria_ok"])]
    if falhas:
        md += ["", "## Falhas"]
        for l in falhas:
            md.append(f"- **{l['id']}. {l['pergunta']}** — faltou: {l['fatos_faltando']} "
                      f"proibido: {l['proibidos_encontrados']} | resposta: {l['resposta'][:300]!r}")
    return "\n".join(md)


def executar(modelo: str | None = None, ids: list[int] | None = None, usar_juiz: bool = False) -> list[dict]:
    if modelo:
        config.MODELO_AGENTE = modelo
    ag = agente.criar_agente(modelo, checkpointer=InMemorySaver())   # memória isolada da avaliação
    juiz = config.llm(config.MODELO_JUIZ) if usar_juiz else None
    DIR_EVID.mkdir(exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "_", config.MODELO_AGENTE.lower())
    carimbo = datetime.now().strftime("%Y%m%d_%H%M")
    arq = DIR_EVID / f"avaliacao_{slug}_{carimbo}.jsonl"
    linhas = []
    for item in CONJUNTO:
        if ids and item["id"] not in ids:
            continue
        t0 = time.perf_counter()
        try:
            linha = avaliar_item(ag, item)
        except Exception as erro:  # noqa: BLE001
            linha = {"id": item["id"], "categoria": item["categoria"], "pergunta": item["pergunta"],
                     "resposta": f"ERRO: {erro.__class__.__name__}: {erro}", "resposta_original": "",
                     "ferramentas": [], "chamadas": [], "fatos_ok": False, "fatos_faltando": item["fatos"],
                     "proibidos_encontrados": [], "trajetoria_ok": False, "verificacao_ok": False,
                     "revisada": False, "problemas_verificacao": [], "segundos": round(time.perf_counter() - t0, 1)}
        if juiz is not None and not linha["resposta"].startswith("ERRO"):
            try:
                j = julgar(juiz, item, linha["resposta"])
                linha["juiz"], linha["juiz_justificativa"] = j.veredito, j.justificativa
            except Exception as erro:  # noqa: BLE001
                linha["juiz"], linha["juiz_justificativa"] = "ERRO", str(erro)[:200]
        linha["modelo"] = config.MODELO_AGENTE
        linhas.append(linha)
        with open(arq, "a", encoding="utf-8") as f:
            f.write(json.dumps(linha, ensure_ascii=False) + "\n")
        print(f"[{linha['id']:2d}] fatos {'ok' if linha['fatos_ok'] else 'X '} | trajetória "
              f"{'ok' if linha['trajetoria_ok'] else 'X '} | {' -> '.join(linha['ferramentas']) or '-'} "
              f"| {linha['segundos']:.0f}s | {item['pergunta']}")
    md = resumo_markdown(config.MODELO_AGENTE, linhas)
    (DIR_EVID / f"avaliacao_{slug}_{carimbo}.md").write_text(md, encoding="utf-8")
    print("\n" + md + f"\n\nArquivos: {arq.name} e {arq.with_suffix('.md').name} em evidencias/")
    return linhas


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--modelo")
    ap.add_argument("--juiz", action="store_true")
    ap.add_argument("--ids", help="ex.: 1,6,7")
    a = ap.parse_args()
    executar(a.modelo, [int(x) for x in a.ids.split(",")] if a.ids else None, a.juiz)
