"""Acesso ao DuckDB (somente leitura) e recálculo do resultado oficial por UF."""
from __future__ import annotations

from pathlib import Path

import duckdb

import config
from calculadora import Candidato, Resultado, comparar_com_oficial, distribuir_cadeiras


def conectar(caminho: Path | str | None = None) -> duckdb.DuckDBPyConnection:
    """Conexão read-only e sem acesso a arquivos externos: o agente nunca altera o banco nem lê outros
    arquivos. (Todas as conexões do processo precisam da mesma configuração no DuckDB.)"""
    return duckdb.connect(str(caminho or config.CAMINHO_DUCKDB), read_only=True,
                          config={"enable_external_access": False})


def dados_uf(con: duckdb.DuckDBPyConnection, uf: str):
    """Vagas, votos válidos por agremiação e candidatos (com votos) de uma UF."""
    vagas = con.execute("SELECT vagas FROM vagas WHERE uf = ?", [uf]).fetchone()[0]
    votos_ag = dict(con.execute(
        "SELECT agremiacao, votos_validos FROM votos_agremiacao_uf WHERE uf = ?", [uf]).fetchall())
    candidatos = [Candidato(sq, nome, ag, int(v)) for sq, nome, ag, v in con.execute(
        "SELECT sq_candidato, nome_urna, agremiacao, votos FROM ranking_candidato_uf "
        "WHERE uf = ? AND votos > 0", [uf]).fetchall()]
    oficiais = {r[0] for r in con.execute(
        "SELECT sq_candidato FROM candidatos WHERE uf = ? AND eleito", [uf]).fetchall()}
    return vagas, votos_ag, candidatos, oficiais


def recalcular_uf(con: duckdb.DuckDBPyConnection, uf: str) -> tuple[Resultado, dict]:
    """Recalcula a distribuição de cadeiras da UF e compara com a situação oficial do TSE."""
    vagas, votos_ag, candidatos, oficiais = dados_uf(con, uf)
    resultado = distribuir_cadeiras(vagas, votos_ag, candidatos)
    return resultado, comparar_com_oficial(resultado, oficiais)


if __name__ == "__main__":
    import sys
    if not config.CAMINHO_DUCKDB.exists():
        print("Banco não encontrado: rode 1_carregar_dados.bat antes.")
        sys.exit(1)
    con = conectar()
    tudo_ok = True
    for uf in config.UFS:
        resultado, comp = recalcular_uf(con, uf)
        print(resultado.resumo())
        sobras = [e for e in resultado.eleitos if e.criterio != "QP"]
        print(f"  eleitos por QP: {len(resultado.eleitos) - len(sobras)} | pelas sobras: {len(sobras)}")
        print(f"  conferência com o TSE: {comp['coincidentes']}/{comp['eleitos_oficiais']} coincidentes"
              + ("  -> IDÊNTICO" if comp["acerto_total"] else
                 f"  -> DIVERGE: só no cálculo {comp['so_no_calculo']}, só no oficial {comp['so_no_oficial']}"))
        print()
        tudo_ok &= comp["acerto_total"]
    print("[CALCULADORA] OK" if tudo_ok else "[CALCULADORA] há divergências — mande esta saída para o Claude")
    sys.exit(0 if tudo_ok else 1)
