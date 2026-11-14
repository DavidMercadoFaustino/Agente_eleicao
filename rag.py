"""RAG sobre a legislação eleitoral: PDFs -> chunks por artigo -> FAISS.

- Normas (leis e resoluções): um chunk por artigo, com a fonte e o número do artigo nos
  metadados, para que a resposta cite "Res.-TSE 23.677/2021, art. 11, § 4º".
  Artigos longos são subdivididos mantendo o cabeçalho "fonte, art. X".
- Decisões (acórdão do STF): divisão recursiva por tamanho.
- Índice FAISS com os embeddings do LM Studio; os chunks também ficam em JSONL para
  inspeção e para a busca exata por artigo (sem embeddings).
"""
from __future__ import annotations

import json
import re
import warnings
import unicodedata
from pathlib import Path

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

import config

CAMINHO_CHUNKS = config.CAMINHO_CHUNKS
TAM_MAX = 1800
SOBREPOSICAO = 200

# "Art. 12-A." / "Art. 12A." têm sufixo; em "Art. 1º A Resolução..." o "A" é texto
RE_ARTIGO = re.compile(r"^Art\.\s?(\d+)(?:\s?[º°o])?(?:\s?-\s?([A-Z])\b|([A-Z])\b)?\s?[.\s]", re.MULTILINE)
RE_LIXO = [
    re.compile(r"^.*—\s*Tribunal Superior Eleitoral.*$", re.MULTILINE),  # cabeçalho/rodapé do navegador
    re.compile(r"^https?://\S+.*$", re.MULTILINE),
    re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4},? \d{1,2}:\d{2}.*$", re.MULTILINE),
    re.compile(r"^\s*\d+\s*/\s*\d+\s*$", re.MULTILINE),                 # "12/167"
]


def nome_fonte(arquivo: Path) -> tuple[str, str]:
    """Nome curto para citação e tipo do documento ('norma' ou 'decisao')."""
    n = unicodedata.normalize("NFC", arquivo.stem)
    if m := re.search(r"RESOLU[ÇC][ÃA]O N[ºo°]?\s*([\d.]+).*?(\d{4})\s*—", n, re.IGNORECASE):
        return f"Res.-TSE {m.group(1)}/{m.group(2)}", "norma"
    if "Código Eleitoral" in n:
        return "Código Eleitoral (Lei 4.737/1965)", "norma"
    if "9.504" in n:
        return "Lei das Eleições (Lei 9.504/1997)", "norma"
    if m := re.search(r"ADI", n, re.IGNORECASE):
        nums = re.findall(r"\d{4}", n)
        return "STF, ADI " + "/".join(nums or ["7228"]), "decisao"
    return n, "norma"


# caracteres de uso privado que as fontes dos PDFs do TSE usam para hífen e aspas
PUA = {"\ue088": "-", "\ue081": "“", "\ue082": "”", "\u02ee": "”", "\xa0": " "}
RE_NOTA = re.compile(r"(com reda[çc][ãa]o|acrescid|revogad|inclu[íi]d|renumerad|alterad|regulamentad|"
                     r"vide|v\.|caput com)", re.IGNORECASE)


def ler_pdf(arquivo: Path) -> str:
    from pypdf import PdfReader
    texto = "\n".join((p.extract_text() or "") for p in PdfReader(str(arquivo)).pages)
    for k, v in PUA.items():
        texto = texto.replace(k, v)
    texto = re.sub(r"[\ue000-\uf8ff]", "", texto)
    for r in RE_LIXO:
        texto = r.sub("", texto)
    texto = re.sub(r"(\w)-\n(\w)", r"\1\2", texto)       # hifenização de fim de linha
    texto = re.sub(r"[ \t]+", " ", texto)
    return re.sub(r"\n{3,}", "\n\n", texto).strip()


def _dividir_longo(texto: str, cabecalho: str, meta: dict) -> list[Document]:
    if len(texto) <= TAM_MAX:
        return [Document(page_content=texto, metadata=meta)]
    partes = RecursiveCharacterTextSplitter(
        chunk_size=TAM_MAX, chunk_overlap=SOBREPOSICAO, separators=["\n§", "\n", ". ", " "]
    ).split_text(texto)
    return [Document(page_content=(p if i == 0 else f"[{cabecalho} (continuação)]\n{p}"),
                     metadata={**meta, "parte": i + 1}) for i, p in enumerate(partes)]


def chunks_por_artigo(texto: str, fonte: str) -> list[Document]:
    """Um Document por artigo; o preâmbulo (ementa) vira o chunk 'art. 0'."""
    docs = []
    # descarta notas de compilação que começam com "Art. 6º com redação dada..."
    marcas = [m for m in RE_ARTIGO.finditer(texto) if not RE_NOTA.match(texto[m.end():m.end() + 30].lstrip())]
    if marcas and marcas[0].start() > 0:
        docs += _dividir_longo(texto[: marcas[0].start()].strip(), f"{fonte}, preâmbulo",
                               {"fonte": fonte, "artigo": "preâmbulo", "tipo": "norma"})
    for i, m in enumerate(marcas):
        fim = marcas[i + 1].start() if i + 1 < len(marcas) else len(texto)
        sufixo = m.group(2) or m.group(3)
        artigo = m.group(1) + (f"-{sufixo}" if sufixo else "")
        corpo = texto[m.start():fim].strip()
        cab = f"{fonte}, art. {artigo}"
        docs += _dividir_longo(corpo, cab, {"fonte": fonte, "artigo": artigo, "tipo": "norma"})
    return docs


def chunks_decisao(texto: str, fonte: str) -> list[Document]:
    partes = RecursiveCharacterTextSplitter(chunk_size=TAM_MAX, chunk_overlap=SOBREPOSICAO).split_text(texto)
    return [Document(page_content=p, metadata={"fonte": fonte, "artigo": "", "tipo": "decisao", "parte": i + 1})
            for i, p in enumerate(partes)]


def gerar_chunks(pasta: Path = config.DIR_LEGISLACAO) -> list[Document]:
    docs = []
    for arq in sorted(pasta.glob("*.pdf")) + sorted(pasta.glob("*.md")) + sorted(pasta.glob("*.txt")):
        fonte, tipo = nome_fonte(arq)
        texto = ler_pdf(arq) if arq.suffix.lower() == ".pdf" else arq.read_text(encoding="utf-8")
        novos = chunks_por_artigo(texto, fonte) if tipo == "norma" else chunks_decisao(texto, fonte)
        for d in novos:
            d.metadata["arquivo"] = arq.name
        docs += novos
    return docs


def salvar_chunks(docs: list[Document], caminho: Path = CAMINHO_CHUNKS) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with open(caminho, "w", encoding="utf-8") as f:
        for d in docs:
            f.write(json.dumps({"texto": d.page_content, **d.metadata}, ensure_ascii=False) + "\n")


def carregar_chunks(caminho: Path = CAMINHO_CHUNKS) -> list[Document]:
    docs = []
    with open(caminho, encoding="utf-8") as f:
        for linha in f:
            r = json.loads(linha)
            docs.append(Document(page_content=r.pop("texto"), metadata=r))
    return docs


def _faiss():
    with warnings.catch_warnings():  # aviso de "sunset" do langchain-community
        warnings.simplefilter("ignore", DeprecationWarning)
        from langchain_community.vectorstores import FAISS
    return FAISS


def construir_indice(docs: list[Document] | None = None, embeddings=None, caminho: Path | None = None):
    """Gera (ou regera) o índice FAISS. Requer o LM Studio com o modelo de embedding carregado."""
    FAISS = _faiss()
    caminho = Path(caminho or config.CAMINHO_FAISS)
    docs = docs or gerar_chunks()
    salvar_chunks(docs, Path(caminho).parent / CAMINHO_CHUNKS.name)
    vs = FAISS.from_documents(docs, embeddings or config.embeddings())
    vs.save_local(str(caminho))
    return vs


def carregar_indice(embeddings=None, caminho: Path | None = None):
    FAISS = _faiss()
    return FAISS.load_local(str(caminho or config.CAMINHO_FAISS), embeddings or config.embeddings(),
                            allow_dangerous_deserialization=True)  # arquivo gerado por nós


def citar(doc: Document) -> str:
    m = doc.metadata
    if m.get("tipo") == "norma" and m.get("artigo") not in ("", "preâmbulo"):
        return f"{m['fonte']}, art. {m['artigo']}"
    return m["fonte"]


def formatar(docs_scores) -> str:
    """Texto entregue ao LLM: cada trecho com a sua citação."""
    blocos = []
    for item in docs_scores:
        doc, score = item if isinstance(item, tuple) else (item, None)
        dist = "" if score is None else f" (distância {score:.3f})"
        blocos.append(f"[{citar(doc)}]{dist}\n{doc.page_content}")
    return "\n\n---\n\n".join(blocos)


def buscar_artigo(fonte_parcial: str, artigo: str, docs: list[Document] | None = None) -> list[Document]:
    """Busca exata (sem embeddings): ex. buscar_artigo('23.677', '11')."""
    docs = docs or carregar_chunks()
    artigo = re.sub(r"^(\d+)\s*[º°o]?\s*-?\s*([A-Za-z])?$",
                    lambda m: m.group(1) + (f"-{m.group(2).upper()}" if m.group(2) else ""), artigo.strip())
    return [d for d in docs if fonte_parcial.lower() in d.metadata["fonte"].lower()
            and d.metadata.get("artigo", "").upper() == artigo]


# Conjunto de avaliação da recuperação: pergunta -> artigos que deveriam aparecer
AVALIACAO_RECUPERACAO = [
    ("o que acontece se nenhum partido atingir o quociente eleitoral?", [("23.677", "12-A"), ("Código", "111")]),
    ("como se calcula o quociente eleitoral?", [("Código", "106")]),
    ("como é determinado o quociente partidário?", [("Código", "107")]),
    ("votação nominal mínima de 10% do quociente eleitoral", [("Código", "108")]),
    ("distribuição das sobras pela maior média com 80% e 20% do quociente",
     [("Código", "109"), ("23.677", "11")]),
    ("empate na votação entre candidatos do mesmo partido", [("23.677", "12"), ("Código", "110")]),
    ("regras aplicáveis às federações de partidos", [("9.504", "6-A")]),
    ("critério de desempate entre médias de partidos", [("23.677", "11")]),
]


def avaliar_recuperacao(vs, k: int = 4) -> dict:
    """Hit@k e MRR: algum artigo esperado aparece entre os k primeiros?"""
    linhas, acertos, rr = [], 0, 0.0
    for pergunta, esperados in AVALIACAO_RECUPERACAO:
        achados = vs.similarity_search(pergunta, k=k)
        pos = next((i for i, d in enumerate(achados, 1)
                    if any(f.lower() in d.metadata["fonte"].lower() and d.metadata.get("artigo") == a
                           for f, a in esperados)), None)
        acertos += pos is not None
        rr += 1 / pos if pos else 0
        linhas.append((pergunta, pos, [citar(d) for d in achados]))
    n = len(AVALIACAO_RECUPERACAO)
    return {"hit": acertos / n, "mrr": rr / n, "linhas": linhas, "k": k}


def imprimir_avaliacao(modelo: str, av: dict) -> None:
    print(f"\nAvaliação da recuperação — {modelo}: Hit@{av['k']} = {av['hit']:.0%} | MRR = {av['mrr']:.2f}")
    for pergunta, pos, cits in av["linhas"]:
        print(f"  {'ok ' if pos else 'X  '} pos={pos or '-'}  {pergunta}")
        if not pos:
            print(f"        veio: {'; '.join(cits)}")


if __name__ == "__main__":
    import sys
    from collections import Counter
    comparar = "--comparar" in sys.argv
    docs = gerar_chunks()
    salvar_chunks(docs)
    print(f"{len(docs)} chunks salvos em {CAMINHO_CHUNKS}")
    for fonte, n in Counter(d.metadata["fonte"] for d in docs).items():
        print(f"  {n:5d}  {fonte}")
    modelos = ["text-embedding-nomic-embed-text-v1.5", "text-embedding-bge-m3"] if comparar else [config.MODELO_EMBEDDING]
    resultados = {}
    for modelo in modelos:
        try:
            print(f"\nGerando embeddings com {modelo}...")
            vs = construir_indice(docs, config.embeddings(modelo), config.caminho_faiss(modelo))
            print(f"Índice salvo em {config.caminho_faiss(modelo)} ({vs.index.ntotal} vetores)")
            resultados[modelo] = avaliar_recuperacao(vs)
            imprimir_avaliacao(modelo, resultados[modelo])
        except Exception as erro:  # noqa: BLE001
            print(f"[RAG] Falhou com {modelo} ({erro.__class__.__name__}: {erro}).")
            print("[RAG] Confira se o LM Studio está ligado e se o modelo aparece na lista.")
            sys.exit(1)
    if comparar:
        print("\nResumo: " + " | ".join(f"{m}: Hit@4 {r['hit']:.0%}, MRR {r['mrr']:.2f}" for m, r in resultados.items()))
    print("\n[RAG] OK")
