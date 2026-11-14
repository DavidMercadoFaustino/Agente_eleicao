"""Configuração central: caminhos, recorte da eleição e modelos do LM Studio."""
from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent
load_dotenv(RAIZ / ".env")

# Caminhos
DIR_DADOS = RAIZ / "dados"
DIR_TSE = DIR_DADOS / "tse"
DIR_LEGISLACAO = DIR_DADOS / "legislacao"
DIR_ARTEFATOS = DIR_DADOS / "artefatos"
CAMINHO_DUCKDB = DIR_ARTEFATOS / "eleicoes_2026.duckdb"
CAMINHO_CHUNKS = DIR_ARTEFATOS / "legislacao_chunks.jsonl"
CAMINHO_MEMORIA = DIR_ARTEFATOS / "memoria_agente.sqlite"

# Recorte
ANO = 2026
UFS = ("SP", "BA")
CARGO = "DEPUTADO FEDERAL"
CD_CARGO = 6

# Modelos (LM Studio, API compatível com OpenAI)
LMSTUDIO_BASE_URL = os.getenv("LMSTUDIO_BASE_URL", "http://192.168.15.8:1234")
MODELO_AGENTE = os.getenv("MODELO_AGENTE", "qwen/qwen3-14b")
MODELO_JUIZ = os.getenv("MODELO_JUIZ", "google/gemma-3-12b")
MODELO_EMBEDDING = os.getenv("MODELO_EMBEDDING", "text-embedding-bge-m3")


def llm(modelo: str | None = None, temperatura: float = 0.0):
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(base_url=LMSTUDIO_BASE_URL, api_key="lm-studio",
                      model=modelo or MODELO_AGENTE, temperature=temperatura)


def caminho_faiss(modelo: str | None = None) -> Path:
    """Um índice por modelo de embedding (permite comparar modelos)."""
    slug = re.sub(r"[^a-z0-9]+", "_", (modelo or MODELO_EMBEDDING).lower()).strip("_")
    return DIR_ARTEFATOS / f"faiss_{slug}"


CAMINHO_FAISS = caminho_faiss()


def embeddings(modelo: str | None = None):
    """Embeddings do LM Studio. O nomic-embed exige os prefixos de tarefa
    'search_document: ' e 'search_query: '; os demais modelos recebem o texto puro."""
    from langchain_openai import OpenAIEmbeddings
    modelo = modelo or MODELO_EMBEDDING

    class EmbeddingsLMStudio(OpenAIEmbeddings):
        def embed_documents(self, texts, chunk_size=None, **kw):
            if "nomic" in modelo:
                texts = [f"search_document: {t}" for t in texts]
            return super().embed_documents(texts, chunk_size=chunk_size, **kw)

        def embed_query(self, text, **kw):
            if "nomic" in modelo:
                text = f"search_query: {text}"
            return super().embed_query(text, **kw)

    return EmbeddingsLMStudio(base_url=LMSTUDIO_BASE_URL, api_key="lm-studio", model=modelo,
                              check_embedding_ctx_length=False)
