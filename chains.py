"""Chains (sem agente): saída estruturada com output parser, roteamento por intenção, composição declarativa
(LCEL) e imperativa (@chain), execução paralela e a interface Runnable (invoke, batch, stream).

Fluxo de conversação: pergunta -> reconhecimento de INTENÇÃO e extração de ENTIDADES (UF, candidato,
partido, município, artigo) em JSON validado por Pydantic -> roteamento para a ferramenta determinística
-> redação da resposta pelo LLM só com os dados recuperados.

Declarativo × imperativo:
- LCEL (`a | b | c`, RunnableParallel, RunnableBranch) descreve O QUE compor; o LangChain cuida de batch,
  stream, async, paralelismo e rastreamento. Bom para pipelines fixos.
- @chain é Python comum: permite if/try/laços, regras de negócio e logs no meio do fluxo, e continua sendo um
  Runnable (tem invoke/batch/stream). Bom quando o caminho depende de decisões em código.
Neste projeto o pipeline fixo é declarativo; o fluxo com tratamento de erro e desvio para "fora de escopo"
é imperativo.
"""
from __future__ import annotations

import re
from typing import Literal, Optional

from langchain_core.exceptions import OutputParserException
from langchain_core.output_parsers import PydanticOutputParser, StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import (RunnableBranch, RunnableLambda, RunnableParallel, RunnablePassthrough,
                                      chain)
from pydantic import BaseModel, Field

import config
import tools

RE_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


# ------------------------------------------------------------------ saída estruturada
class Intencao(BaseModel):
    """Intenção e entidades de uma pergunta (reconhecimento de intenção + extração de entidades)."""
    intencao: Literal["explicar_candidato", "ranking_estado", "ranking_municipio", "resultado_uf",
                      "legislacao", "fora_de_escopo"]
    uf: Literal["SP", "BA", "nenhuma"] = Field(description="UF citada; 'nenhuma' se não houver")
    candidato: Optional[str] = Field(None, description="nome do candidato, se houver")
    municipio: Optional[str] = Field(None, description="nome da cidade, se houver")
    artigo: Optional[str] = Field(None, description="artigo de lei citado, se houver")


parser = PydanticOutputParser(pydantic_object=Intencao)

PROMPT_INTENCAO = ChatPromptTemplate.from_messages([
    ("system", "Você classifica perguntas sobre a eleição de Deputado Federal 2026 em SP e BA e extrai as "
               "entidades. Outras UFs, cargos ou anos são fora_de_escopo. Perguntas sobre artigos, leis ou "
               "regras são legislacao. 'Em SP' sem a palavra cidade/município é o ESTADO (ranking_estado). "
               "Qualquer pergunta sobre UM candidato citado pelo nome é explicar_candidato.\n"
               "{instrucoes}\nResponda SOMENTE com o JSON. /no_think"),
    ("human", "{pergunta}"),
]).partial(instrucoes=parser.get_format_instructions())


def _limpar(texto) -> str:
    texto = getattr(texto, "content", texto)
    texto = RE_THINK.sub("", str(texto)).strip()
    return re.sub(r"^```(?:json)?|```$", "", texto.strip()).strip()


def criar_classificador(llm=None):
    """LCEL: prompt | llm | limpeza | PydanticOutputParser, com tratamento de erro:
    se o JSON vier inválido, o erro do parser volta ao LLM uma vez para correção; se falhar de novo,
    a pergunta é tratada como fora_de_escopo (falha segura)."""
    llm = llm or config.llm()
    base = PROMPT_INTENCAO | llm | RunnableLambda(_limpar)

    def analisar(entrada: dict) -> Intencao:
        bruto = base.invoke(entrada)
        try:
            return parser.parse(bruto)
        except OutputParserException as erro:
            correcao = llm.invoke(f"O JSON abaixo é inválido: {erro}\nJSON: {bruto}\n"
                                  f"{parser.get_format_instructions()}\nDevolva só o JSON corrigido. /no_think")
            try:
                return parser.parse(_limpar(correcao))
            except OutputParserException:
                return Intencao(intencao="fora_de_escopo", uf="nenhuma")

    return RunnableLambda(analisar)


# ------------------------------------------------------------------ roteamento (RunnableBranch)
def _uf(x):
    return x["intencao"].uf if x["intencao"].uf != "nenhuma" else "SP"


roteador = RunnableBranch(
    # candidato citado tem prioridade: a explicação determinística evita que o redator invente o motivo
    (lambda x: x["intencao"].candidato and x["intencao"].intencao != "fora_de_escopo",
     RunnableLambda(lambda x: tools.explicar_candidato.invoke({"uf": _uf(x), "candidato": x["intencao"].candidato}))),
    (lambda x: x["intencao"].intencao == "ranking_municipio" and x["intencao"].municipio,
     RunnableLambda(lambda x: tools.votos_por_municipio.invoke({"uf": _uf(x), "municipio": x["intencao"].municipio,
                                                                "n": 5}))),
    (lambda x: x["intencao"].intencao == "ranking_estado",
     RunnableLambda(lambda x: tools.mais_votados_uf.invoke({"uf": _uf(x), "n": 5}))),
    (lambda x: x["intencao"].intencao == "resultado_uf",
     RunnableLambda(lambda x: tools.resultado_eleicao.invoke({"uf": _uf(x)}))),
    (lambda x: x["intencao"].intencao == "legislacao",
     RunnableLambda(lambda x: tools.buscar_legislacao.invoke({"pergunta": x["pergunta"], "k": 3}))),
    RunnableLambda(lambda x: "FORA_DE_ESCOPO"),   # ramo padrão
)

PROMPT_REDATOR = ChatPromptTemplate.from_messages([
    ("system", "Responda em português, de forma objetiva, usando SOMENTE os dados abaixo. Não invente números "
               "nem links. Cite a legislação entre colchetes como aparece nos dados.\n\nDADOS:\n{dados} /no_think"),
    ("human", "{pergunta}"),
])


def criar_redator(llm=None):
    return PROMPT_REDATOR | (llm or config.llm()) | StrOutputParser() | RunnableLambda(_limpar)


# ------------------------------------------------------------------ composição declarativa (LCEL)
def criar_pipeline_lcel(llm=None):
    """Chain SEQUENCIAL declarativa: classificar -> rotear para a ferramenta -> redigir."""
    return (RunnablePassthrough.assign(intencao=criar_classificador(llm))
            | RunnablePassthrough.assign(dados=roteador)
            | criar_redator(llm))


# ------------------------------------------------------------------ composição imperativa (@chain)
def criar_pipeline_imperativo(llm=None):
    classificador, redator = criar_classificador(llm), criar_redator(llm)

    @chain
    def pipeline(pergunta: str) -> dict:
        """Mesma lógica em Python comum, com desvio explícito e retorno da trajetória."""
        intencao = classificador.invoke({"pergunta": pergunta})
        if intencao.intencao == "fora_de_escopo":
            return {"intencao": intencao, "dados": None,
                    "resposta": "Não cubro essa pergunta: trabalho com Deputado Federal 2026 em SP e BA."}
        dados = roteador.invoke({"pergunta": pergunta, "intencao": intencao})
        if dados == "FORA_DE_ESCOPO" or dados.startswith(("Nenhum", "Município", "Mais de um")):
            return {"intencao": intencao, "dados": dados, "resposta": dados}   # sem LLM: devolve a mensagem
        return {"intencao": intencao, "dados": dados,
                "resposta": redator.invoke({"pergunta": pergunta, "dados": dados})}

    return pipeline


# ------------------------------------------------------------------ paralelismo (RunnableParallel)
comparar_ufs = RunnableParallel(
    SP=RunnableLambda(lambda _: tools.resultado_eleicao.invoke({"uf": "SP"})),
    BA=RunnableLambda(lambda _: tools.resultado_eleicao.invoke({"uf": "BA"})),
)
