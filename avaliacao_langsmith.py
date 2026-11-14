"""Avaliação no LangSmith: dataset, experimento e avaliadores de RESPOSTA e de TRAJETÓRIA.

1. Cria (ou reaproveita) o dataset "agente-eleitoral-2026" no LangSmith com as 14 perguntas de
   avaliacao.CONJUNTO e o gabarito (fatos, proibidos, ferramentas esperadas).
2. Roda o agente em cada exemplo com evaluate(); com LANGSMITH_TRACING=true cada execução vira um trace,
   com todas as chamadas de ferramenta (a trajetória fica visível no LangSmith).
3. Avaliadores:
   - fatos               : 1 se a resposta contém todos os fatos do gabarito e nenhum proibido;
   - trajetoria_superset : 1 se cada ferramenta esperada foi chamada (chamadas extras são permitidas);
   - trajetoria_precisao : fração das chamadas que estão entre as esperadas (penaliza chamadas inúteis);
   - verificacao         : 1 se a resposta final passou no auditor;
   - juiz (opcional)     : LLM-juiz com saída estruturada Literal["CORRETO","PARCIAL","INCORRETO"].
4. Salva a tabela do experimento em evidencias/langsmith_<experimento>.csv.

Pré-requisitos no .env: LANGSMITH_API_KEY=<sua chave>, LANGSMITH_TRACING=true.
Uso: python avaliacao_langsmith.py [--ids 1,6,7] [--juiz]
"""
from __future__ import annotations

import argparse
import os
import re
import uuid

from langgraph.checkpoint.memory import InMemorySaver

import agente
import avaliacao
import config

NOME_DATASET = "agente-eleitoral-2026"


# ---------------------------------------------------------------- avaliadores (testáveis sem rede)
def av_fatos(outputs: dict, reference_outputs: dict) -> dict:
    t = avaliacao._norm(outputs.get("resposta", ""))
    ok = all(re.search(f, t) for f in reference_outputs["fatos"]) and \
        not any(re.search(p, t) for p in reference_outputs.get("proibidos", []))
    return {"key": "fatos", "score": int(ok)}


def av_trajetoria_superset(outputs: dict, reference_outputs: dict) -> dict:
    chamadas = set(outputs.get("ferramentas", []))
    if reference_outputs.get("nenhuma"):
        return {"key": "trajetoria_superset", "score": int(not chamadas)}
    ok = all(any(f in chamadas for f in grupo) for grupo in reference_outputs["ferramentas"])
    return {"key": "trajetoria_superset", "score": int(ok)}


def av_trajetoria_precisao(outputs: dict, reference_outputs: dict) -> dict:
    chamadas = outputs.get("ferramentas", [])
    if not chamadas:
        return {"key": "trajetoria_precisao", "score": 1.0 if reference_outputs.get("nenhuma") else 0.0}
    esperadas = {f for grupo in reference_outputs["ferramentas"] for f in grupo} | {"esquema_banco"}
    return {"key": "trajetoria_precisao", "score": round(sum(c in esperadas for c in chamadas) / len(chamadas), 2)}


def av_verificacao(outputs: dict) -> dict:
    return {"key": "verificacao", "score": int(bool(outputs.get("verificacao_ok")))}


def criar_av_juiz():
    juiz = config.llm(config.MODELO_JUIZ)

    def av_juiz(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
        item = {"pergunta": inputs["pergunta"], "fatos": reference_outputs["fatos"],
                "proibidos": reference_outputs.get("proibidos", [])}
        j = avaliacao.julgar(juiz, item, outputs.get("resposta", ""))
        return {"key": "juiz", "score": {"CORRETO": 1, "PARCIAL": 0.5, "INCORRETO": 0}[j.veredito],
                "comment": f"{j.veredito}: {j.justificativa}"}
    return av_juiz


# ---------------------------------------------------------------- dataset e alvo
def exemplos(ids=None):
    for item in avaliacao.CONJUNTO:
        if ids and item["id"] not in ids:
            continue
        yield ({"id": item["id"], "pergunta": item["pergunta"], "contexto": item.get("contexto", [])},
               {"fatos": item["fatos"], "proibidos": item.get("proibidos", []),
                "ferramentas": [list(g) for g in item["ferramentas"]], "nenhuma": item.get("nenhuma", False),
                "categoria": item["categoria"]})


def garantir_dataset(cliente) -> str:
    if not cliente.has_dataset(dataset_name=NOME_DATASET):
        ds = cliente.create_dataset(NOME_DATASET, description="Deputado Federal 2026 (SP e BA): 14 perguntas com "
                                                              "gabarito de fatos e de ferramentas.")
        pares = list(exemplos())
        cliente.create_examples(dataset_id=ds.id, examples=[{"inputs": i, "outputs": o} for i, o in pares])
    return NOME_DATASET


def criar_alvo():
    ag = agente.criar_agente(checkpointer=InMemorySaver())

    def alvo(inputs: dict) -> dict:
        thread = str(uuid.uuid4())
        for previa in inputs.get("contexto", []):
            agente.perguntar(ag, previa, thread)
        r = agente.perguntar(ag, inputs["pergunta"], thread)
        return {"resposta": r.texto, "ferramentas": r.ferramentas, "verificacao_ok": bool(r.verificacao and
                r.verificacao.ok), "revisada": r.revisada, "segundos": r.segundos}
    return alvo


def executar(ids=None, usar_juiz=False):
    from langsmith import Client, evaluate
    if not os.getenv("LANGSMITH_API_KEY"):
        raise SystemExit("Defina LANGSMITH_API_KEY no arquivo .env (crie a chave em smith.langchain.com).")
    os.environ.setdefault("LANGSMITH_TRACING", "true")
    cliente = Client()
    nome = garantir_dataset(cliente)
    dados = [e for e in cliente.list_examples(dataset_name=nome) if not ids or e.inputs.get("id") in ids]
    avaliadores = [av_fatos, av_trajetoria_superset, av_trajetoria_precisao, av_verificacao]
    if usar_juiz:
        avaliadores.append(criar_av_juiz())
    resultados = evaluate(criar_alvo(), data=dados, evaluators=avaliadores, client=cliente, max_concurrency=1,
                          experiment_prefix=re.sub(r"[^a-z0-9]+", "-", config.MODELO_AGENTE.lower()),
                          metadata={"modelo": config.MODELO_AGENTE, "embedding": config.MODELO_EMBEDDING})
    df = resultados.to_pandas()
    destino = config.RAIZ / "evidencias" / f"langsmith_{resultados.experiment_name}.csv"
    df.to_csv(destino, index=False, encoding="utf-8")
    metricas = [c for c in df.columns if c.startswith("feedback.")]
    print("\nExperimento:", resultados.experiment_name, "|", getattr(resultados, "url", ""))
    print(df[metricas].mean().round(2).to_string())
    print(f"\nTabela salva em {destino.name}. Veja traces e trajetórias no LangSmith, projeto/dataset '{nome}'.")
    return df


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids")
    ap.add_argument("--juiz", action="store_true")
    a = ap.parse_args()
    executar([int(x) for x in a.ids.split(",")] if a.ids else None, a.juiz)
