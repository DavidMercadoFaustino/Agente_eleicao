"""Agente integrador: create_agent (LangChain 1.x) + ferramentas + memória persistente em SQLite.

Memória (taxonomia CoALA):
- memória de trabalho: as mensagens da conversa atual (estado do grafo);
- memória episódica: o checkpointer SQLite guarda cada conversa por thread_id e sobrevive a reinícios;
- memória semântica: o conhecimento estável vem das ferramentas (banco TSE e legislação), não do LLM;
- memória procedimental: as regras do prompt de sistema.
O SummarizationMiddleware resume o histórico antigo quando a conversa fica longa (modelo local, contexto curto).
"""
from __future__ import annotations

import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, SummarizationMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver

import auditor
import config
from callbacks import LogAgente
from prompts import SISTEMA
from tools import FERRAMENTAS

RE_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


@dataclass
class Resposta:
    pergunta: str
    texto: str
    ferramentas: list[str] = field(default_factory=list)       # trajetória (ordem das chamadas)
    chamadas: list[dict] = field(default_factory=list)          # nome + argumentos + início da saída
    segundos: float = 0.0
    thread_id: str = ""
    verificacao: auditor.Verificacao | None = None   # resultado final da conferência
    revisada: bool = False                           # a 1ª resposta foi reprovada e reescrita
    texto_original: str = ""                         # 1ª resposta (antes da revisão)

    def __str__(self) -> str:
        return self.texto


def checkpointer_sqlite(caminho=config.CAMINHO_MEMORIA) -> SqliteSaver:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(str(caminho), check_same_thread=False))


def criar_agente(modelo=None, ferramentas=None, checkpointer=None, resumir: bool = True,
                 gatilho_resumo=("tokens", 4000), manter=("messages", 8)):
    """modelo: nome no LM Studio ou um BaseChatModel (os testes usam um modelo falso).
    Janela de contexto: quando o histórico passa do gatilho, as mensagens antigas viram um resumo e só as
    últimas `manter` ficam literais; o conhecimento estável vem da busca vetorial (FAISS) e do banco."""
    llm = modelo if modelo is not None and not isinstance(modelo, str) else config.llm(modelo)
    middleware = [ModelCallLimitMiddleware(run_limit=10, exit_behavior="end")]
    if resumir:
        middleware.append(SummarizationMiddleware(model=llm, trigger=gatilho_resumo, keep=manter))
    return create_agent(llm, ferramentas or FERRAMENTAS, system_prompt=SISTEMA, middleware=middleware,
                        checkpointer=checkpointer if checkpointer is not None else checkpointer_sqlite())


def _rodada(agente, conteudo: str, cfg: dict):
    """Uma chamada ao agente; devolve (texto final, chamadas, saídas das ferramentas)."""
    # mensagens já existentes, por id: quando o resumo dispara, o histórico ENCOLHE, então não dá para
    # separar as mensagens novas pela posição na lista
    anteriores = {m.id for m in agente.get_state(cfg).values.get("messages", [])} if agente.checkpointer else set()
    cfg = {**cfg, "callbacks": [LogAgente(sessao=cfg["configurable"]["thread_id"])]}   # log de cada evento
    estado = agente.invoke({"messages": [{"role": "user", "content": conteudo}]}, config=cfg)
    novas = [m for m in estado["messages"] if m.id not in anteriores]
    chamadas, saidas = [], {}
    for m in novas:
        if isinstance(m, AIMessage):
            chamadas += [{"id": c["id"], "nome": c["name"], "args": c["args"]} for c in m.tool_calls]
        elif isinstance(m, ToolMessage):
            saidas[m.tool_call_id] = str(m.content)
    for c in chamadas:
        c["saida"] = saidas.get(c.pop("id"), "")
    final = next((m for m in reversed(novas) if isinstance(m, AIMessage) and not m.tool_calls), None)
    texto = RE_THINK.sub("", str(final.content if final else "")).strip() or "(o agente não produziu resposta)"
    return texto, chamadas


def _sufixo() -> str:
    # o Qwen3 só desliga o raciocínio longo quando "/no_think" está na mensagem do usuário
    return " /no_think" if "qwen3" in str(config.MODELO_AGENTE).lower() else ""


def perguntar(agente, pergunta: str, thread_id: str | None = None, verificar: bool = True) -> Resposta:
    """Pergunta -> agente -> verificação; se reprovada, o agente reescreve uma vez com a lista de problemas."""
    thread_id = thread_id or str(uuid.uuid4())
    cfg = {"configurable": {"thread_id": thread_id}}
    t0 = time.perf_counter()
    texto, chamadas = _rodada(agente, pergunta + _sufixo(), cfg)
    r = Resposta(pergunta, texto, [c["nome"] for c in chamadas], chamadas, 0.0, thread_id)
    if verificar:
        v = auditor.verificar(texto, [c["saida"] for c in chamadas], pergunta)
        if not v.ok:
            r.texto_original, r.revisada = texto, True
            texto2, chamadas2 = _rodada(agente, auditor.mensagem_de_correcao(v) + _sufixo(), cfg)
            r.chamadas += chamadas2
            r.ferramentas = [c["nome"] for c in r.chamadas]
            v = auditor.verificar(texto2, [c["saida"] for c in r.chamadas], pergunta)
            r.texto = texto2
            if not v.ok:
                r.texto += ("\n\n[Aviso da verificação automática] Não foi possível confirmar nas ferramentas: "
                            + " ".join(v.problemas))
        r.verificacao = v
    for c in r.chamadas:
        c["saida"] = c["saida"][:500]
    r.segundos = round(time.perf_counter() - t0, 1)
    return r


def conversar() -> None:
    """Chat no terminal. 'nova' inicia outra conversa; 'sair' encerra."""
    agente = criar_agente()
    thread = str(uuid.uuid4())
    print(f"Analista Eleitoral 2026 (modelo {config.MODELO_AGENTE}). Digite 'nova' ou 'sair'.\n")
    while True:
        try:
            pergunta = input("Você: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if pergunta.lower() in {"sair", "exit", "quit"}:
            break
        if pergunta.lower() == "nova":
            thread = str(uuid.uuid4())
            print("(nova conversa)\n")
            continue
        if not pergunta:
            continue
        try:
            r = perguntar(agente, pergunta, thread)
        except Exception as erro:  # noqa: BLE001
            print(f"[ERRO] {erro.__class__.__name__}: {erro}\n")
            continue
        print(f"\nAgente: {r.texto}")
        verif = "" if r.verificacao is None else (" | verificação: " + r.verificacao.resumo()
                                                 + (" após revisão" if r.revisada else ""))
        print(f"   [ferramentas: {' -> '.join(r.ferramentas) or 'nenhuma'}{verif} | {r.segundos}s]")
        if r.revisada:
            print("   [a 1ª resposta foi reprovada pela verificação e reescrita]")
        print()


if __name__ == "__main__":
    conversar()
