"""APIs clássicas do LangChain (pacote langchain-classic), exigidas na rubrica, rodando sobre as MESMAS
ferramentas do projeto, para comparação com a implementação principal (LangChain 1.x):

| Clássico (este arquivo)                          | Atual (agente.py / chains.py)                    |
|--------------------------------------------------|--------------------------------------------------|
| ConversationBufferMemory + ConversationChain     | checkpointer (SqliteSaver) por thread_id         |
| initialize_agent (ReAct conversacional)          | create_agent (ReAct com tool calling nativo)     |
| create_openai_tools_agent + AgentExecutor        | create_agent                                     |
| RunnableWithMessageHistory                       | checkpointer + SummarizationMiddleware           |
| LLMRouterChain (RouterChain)                     | RunnableBranch                                   |

Os clássicos foram marcados como legado na versão 1.0; a implementação principal usa as APIs atuais.
"""
from __future__ import annotations

import re
import warnings

from langchain_core.chat_history import InMemoryChatMessageHistory
from langchain_core.messages import SystemMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder, PromptTemplate
from langchain_core.runnables.history import RunnableWithMessageHistory

import config
import tools
from prompts import SISTEMA

warnings.filterwarnings("ignore", category=DeprecationWarning)
RE_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
limpar = lambda t: RE_THINK.sub("", str(getattr(t, "content", t))).strip()  # noqa: E731


# ---------------------------------------------------------------- 1. Chatbot com ConversationBufferMemory
def chatbot_buffer(llm=None):
    """ConversationChain + ConversationBufferMemory: o histórico inteiro vai no prompt a cada turno."""
    from langchain_classic.chains import ConversationChain
    from langchain_classic.memory import ConversationBufferMemory
    prompt = PromptTemplate.from_template(
        "Você é um assistente sobre o sistema eleitoral brasileiro. Responda em português, em até 3 frases. "
        "/no_think\n\nHistórico:\n{history}\nUsuário: {input}\nAssistente:")
    return ConversationChain(llm=llm or config.llm(), memory=ConversationBufferMemory(), prompt=prompt)


# ---------------------------------------------------------------- 2. initialize_agent (ReAct)
def _ferramentas_texto():
    """O ReAct clássico em texto aceita ferramentas de UMA entrada (string); por isso estes adaptadores."""
    from langchain_classic.agents import Tool

    def uf_e_nome(entrada: str):
        partes = [p.strip() for p in re.split(r"[,;|]", entrada, maxsplit=1)]
        return (partes[0].upper(), partes[1]) if len(partes) == 2 else ("SP", partes[0])

    return [
        Tool(name="explicar_candidato", description="Explica por que um candidato foi ou não eleito. "
             "Entrada: 'UF, nome' (ex.: 'SP, Rui Falcão').",
             func=lambda e: tools.explicar_candidato.invoke(dict(zip(("uf", "candidato"), uf_e_nome(e))))),
        Tool(name="mais_votados_uf", description="Mais votados no estado. Entrada: a UF ('SP' ou 'BA').",
             func=lambda e: tools.mais_votados_uf.invoke({"uf": e.strip().strip("'\"")[:2].upper(), "n": 5})),
        Tool(name="buscar_legislacao", description="Busca na legislação eleitoral. Entrada: a pergunta.",
             func=lambda e: tools.buscar_legislacao.invoke({"pergunta": e, "k": 2})),
    ]


def agente_initialize(llm=None):
    """initialize_agent com AgentType.CONVERSATIONAL_REACT_DESCRIPTION: ciclo Thought -> Action ->
    Observation em texto, com memória ConversationBufferMemory."""
    from langchain_classic.agents import AgentType, initialize_agent
    from langchain_classic.memory import ConversationBufferMemory
    return initialize_agent(
        _ferramentas_texto(), llm or config.llm(), agent=AgentType.CONVERSATIONAL_REACT_DESCRIPTION,
        memory=ConversationBufferMemory(memory_key="chat_history"), handle_parsing_errors=True,
        max_iterations=5, verbose=True,
        agent_kwargs={"prefix": "Você responde sobre Deputado Federal 2026 em SP e BA usando as ferramentas. "
                                "Responda em português. /no_think"})


# ---------------------------------------------------------------- 3. create_openai_tools_agent + AgentExecutor
#                                     + 4. RunnableWithMessageHistory
_historicos: dict[str, InMemoryChatMessageHistory] = {}


def agente_executor(llm=None):
    """create_openai_tools_agent (function calling) + AgentExecutor com as 11 ferramentas do projeto,
    envolvido por RunnableWithMessageHistory (histórico por session_id)."""
    from langchain_classic.agents import AgentExecutor, create_openai_tools_agent
    prompt = ChatPromptTemplate.from_messages([
        SystemMessage(content=SISTEMA),
        MessagesPlaceholder("chat_history"),
        ("human", "{input} /no_think"),
        MessagesPlaceholder("agent_scratchpad"),
    ])
    agente = create_openai_tools_agent(llm or config.llm(), tools.FERRAMENTAS, prompt)
    executor = AgentExecutor(agent=agente, tools=tools.FERRAMENTAS, max_iterations=6,
                             handle_parsing_errors=True, return_intermediate_steps=True)
    return RunnableWithMessageHistory(
        executor, lambda sid: _historicos.setdefault(sid, InMemoryChatMessageHistory()),
        input_messages_key="input", history_messages_key="chat_history")


# ---------------------------------------------------------------- 5. RouterChain (LLMRouterChain)
DESTINOS = {
    "candidato": ("perguntas sobre um candidato específico",
                  lambda q: tools.explicar_candidato.invoke({"uf": "BA" if re.search(r"\b(BA|Bahia)\b", q, re.I)
                                                             else "SP", "candidato": q})),
    "ranking": ("mais votados de um estado",
                lambda q: tools.mais_votados_uf.invoke({"uf": "BA" if re.search(r"\b(BA|Bahia)\b", q, re.I)
                                                        else "SP", "n": 5})),
    "legislacao": ("regras, artigos de lei, resoluções do TSE",
                   lambda q: tools.buscar_legislacao.invoke({"pergunta": q, "k": 2})),
}


def roteador_classico(llm=None):
    """LLMRouterChain: o LLM escolhe o destino (JSON); o despacho chama a ferramenta do destino."""
    from langchain_classic.chains.router.llm_router import LLMRouterChain, RouterOutputParser
    from langchain_classic.chains.router.multi_prompt_prompt import MULTI_PROMPT_ROUTER_TEMPLATE
    destinos = "\n".join(f"{nome}: {desc}" for nome, (desc, _) in DESTINOS.items())
    template = MULTI_PROMPT_ROUTER_TEMPLATE.format(destinations=destinos) + "\n/no_think"
    prompt = PromptTemplate(template=template, input_variables=["input"], output_parser=RouterOutputParser())
    return LLMRouterChain.from_llm(llm or config.llm(), prompt)


def rotear_classico(pergunta: str, roteador=None) -> dict:
    roteador = roteador or roteador_classico()
    decisao = roteador.invoke({"input": pergunta})
    destino = decisao.get("destination") or "DEFAULT"
    if destino not in DESTINOS:
        return {"destino": destino, "saida": "Fora do escopo dos destinos disponíveis."}
    entrada = decisao.get("next_inputs", {}).get("input", pergunta)
    return {"destino": destino, "saida": DESTINOS[destino][1](entrada)}
