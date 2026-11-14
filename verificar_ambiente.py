"""Diagnóstico: Python, pacotes, dados e LM Studio.  Uso: .venv\\Scripts\\python verificar_ambiente.py"""
import importlib
import json
import sys
import urllib.request

PACOTES = ["duckdb", "pandas", "dotenv", "langchain", "langchain_openai", "langchain_community",
           "langchain_text_splitters", "langgraph", "langgraph.checkpoint.sqlite", "langsmith",
           "faiss", "pypdf", "pytest", "ipykernel", "langchain_classic", "streamlit"]


def verificar() -> bool:
    ok = True
    print(f"Python {sys.version.split()[0]} em {sys.executable}")
    if ".venv" not in sys.executable:
        print("  AVISO: este não é o Python do .venv do projeto. No VS Code: Ctrl+Shift+P > "
              "'Python: Select Interpreter' > .venv")
    for p in PACOTES:
        try:
            importlib.import_module(p)
            print(f"  [ok]    {p}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"  [FALTA] {p} ({e.__class__.__name__}) -> rode 0_instalar_ambiente.bat")
    if not ok:
        return False

    import config
    zips = sorted(p.name for p in config.DIR_TSE.glob("*.zip"))
    pdfs = sorted(p.name for p in config.DIR_LEGISLACAO.glob("*.pdf"))
    print(f"Dados TSE ({len(zips)}): {', '.join(zips) or 'nenhum'}")
    print(f"Legislação: {len(pdfs)} PDF(s)")
    print(f"Banco DuckDB: {'ok' if config.CAMINHO_DUCKDB.exists() else 'ainda não gerado'}")
    print(f"Índice FAISS: {'ok' if config.CAMINHO_FAISS.exists() else 'ainda não gerado'}")
    try:
        with urllib.request.urlopen(config.LMSTUDIO_BASE_URL.rstrip("/") + "/models", timeout=5) as r:
            modelos = [m["id"] for m in json.load(r)["data"]]
        print(f"LM Studio: ok em {config.LMSTUDIO_BASE_URL}")
        for m in modelos:
            marca = " <- agente" if m == config.MODELO_AGENTE else " <- juiz" if m == config.MODELO_JUIZ else \
                " <- embedding" if m == config.MODELO_EMBEDDING else ""
            print(f"  - {m}{marca}")
        for nome, m in [("MODELO_AGENTE", config.MODELO_AGENTE), ("MODELO_JUIZ", config.MODELO_JUIZ),
                        ("MODELO_EMBEDDING", config.MODELO_EMBEDDING)]:
            if m not in modelos:
                print(f"  AVISO: {nome}={m} não está na lista; ajuste no arquivo .env")
    except Exception as e:  # noqa: BLE001
        print(f"LM Studio: sem resposta em {config.LMSTUDIO_BASE_URL} ({e.__class__.__name__}). "
              "Ligue o servidor no LM Studio e confira o endereço no .env.")
    return ok


if __name__ == "__main__":
    sys.exit(0 if verificar() else 1)
