"""ETL: arquivos do TSE (zips em dados/tse) -> DuckDB (dados/artefatos/eleicoes_2026.duckdb).

Etapas:
1. extrair dos zips só os CSVs das UFs do recorte, convertendo latin-1 -> UTF-8;
2. carregar tabelas brutas (todas as colunas como texto), filtradas pelo cargo;
3. criar views tipadas e analíticas usadas pelas ferramentas do agente.

Uso: 1_carregar_dados.bat  (ou executar_etl() no notebook)
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

import duckdb

import config

DIR_CSV = config.DIR_ARTEFATOS / "csv"

# prefixo do CSV dentro do zip -> tabela bruta no DuckDB
FONTES = {
    "votacao_candidato_munzona": "bruto_votos_candidato",
    "votacao_partido_munzona": "bruto_votos_partido",
    "detalhe_votacao_munzona": "bruto_detalhe_apuracao",
    "consulta_cand": "bruto_candidatos",
    "consulta_vagas": "bruto_vagas",
    "receitas_candidatos": "bruto_receitas",
    "despesas_contratadas_candidatos": "bruto_despesas",
}

# tabelas opcionais: se existirem, ganham views próprias
OPCIONAIS = {"bruto_detalhe_apuracao", "bruto_receitas", "bruto_despesas"}

# colunas obrigatórias que as views usam (conferidas na primeira carga real)
COLUNAS_ESPERADAS = {
    "bruto_votos_candidato": ["SG_UF", "CD_CARGO", "NM_MUNICIPIO", "NR_ZONA", "SQ_CANDIDATO",
                              "NM_URNA_CANDIDATO", "SG_PARTIDO", "QT_VOTOS_NOMINAIS_VALIDOS"],
    "bruto_votos_partido": ["SG_UF", "CD_CARGO", "SG_PARTIDO", "QT_VOTOS_NOMINAIS_VALIDOS",
                            "QT_TOTAL_VOTOS_LEG_VALIDOS"],
    "bruto_candidatos": ["SG_UF", "CD_CARGO", "SQ_CANDIDATO", "NM_URNA_CANDIDATO", "SG_PARTIDO",
                         "DS_SIT_TOT_TURNO"],
    "bruto_vagas": ["SG_UF", "CD_CARGO"],
}


def extrair_csvs(ufs=config.UFS) -> list[Path]:
    """Extrai os CSVs das UFs do recorte, convertendo para UTF-8."""
    DIR_CSV.mkdir(parents=True, exist_ok=True)
    zips = sorted(config.DIR_TSE.glob("*.zip"))
    if not zips:
        raise FileNotFoundError(f"Nenhum zip em {config.DIR_TSE}. Veja dados/LEIA-ME.md.")
    padrao_uf = re.compile(r"_(%s)\.csv$" % "|".join(ufs), re.IGNORECASE)
    extraidos = []
    for z in zips:
        with zipfile.ZipFile(z) as arq:
            for nome in arq.namelist():
                base = Path(nome).name
                if not base.lower().endswith(".csv") or not padrao_uf.search(base):
                    continue
                destino = DIR_CSV / base
                with arq.open(nome) as orig, open(destino, "w", encoding="utf-8", newline="") as dest:
                    for linha in orig:
                        dest.write(linha.decode("latin-1"))
                extraidos.append(destino)
    return extraidos


def _tabela_da_fonte(nome_csv: str) -> str | None:
    for prefixo, tabela in sorted(FONTES.items(), key=lambda x: -len(x[0])):
        if nome_csv.lower().startswith(prefixo):
            return tabela
    return None


def carregar_brutas(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Cria as tabelas brutas (texto) a partir dos CSVs extraídos, só com o cargo do recorte."""
    grupos: dict[str, list[str]] = {}
    for csv in sorted(DIR_CSV.glob("*.csv")):
        tabela = _tabela_da_fonte(csv.name)
        if tabela:
            grupos.setdefault(tabela, []).append(csv.as_posix())
    contagens = {}
    for tabela, arquivos in grupos.items():
        lista = ", ".join(f"'{a}'" for a in arquivos)
        leitura = (f"read_csv([{lista}], delim=';', header=true, all_varchar=true, "
                   f"union_by_name=true, filename=true)")
        colunas = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {leitura}").fetchall()]
        filtro = f"WHERE CAST(CD_CARGO AS INTEGER) = {config.CD_CARGO}" if "CD_CARGO" in colunas else ""
        con.execute(f"CREATE OR REPLACE TABLE {tabela} AS SELECT * FROM {leitura} {filtro}")
        contagens[tabela] = con.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]
    return contagens


def conferir_colunas(con: duckdb.DuckDBPyConnection) -> dict[str, list[str]]:
    """Lista colunas esperadas que não existem (para ajustar as views na primeira carga)."""
    faltando = {}
    tabelas = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    for tabela, cols in COLUNAS_ESPERADAS.items():
        if tabela not in tabelas:
            faltando[tabela] = ["<tabela ausente>"]
            continue
        existentes = {r[0] for r in con.execute(f"DESCRIBE {tabela}").fetchall()}
        ausentes = [c for c in cols if c not in existentes]
        if ausentes:
            faltando[tabela] = ausentes
    return faltando


def _int(col: str) -> str:
    return f"TRY_CAST(NULLIF(NULLIF(NULLIF({col}, '#NULO#'), '#NULO'), '-1') AS BIGINT)"


def _agremiacao(prefixo: str = "") -> str:
    """Federação conta como uma agremiação única (nome da federação); sem federação, vale o partido."""
    p = prefixo
    return (f"CASE WHEN COALESCE({p}NR_FEDERACAO, '-1') NOT IN ('-1', '#NULO', '#NULO#', '') "
            f"THEN {p}NM_FEDERACAO ELSE {p}SG_PARTIDO END")


def criar_views(con: duckdb.DuckDBPyConnection) -> None:
    """Views tipadas e analíticas consumidas pelas ferramentas."""
    con.execute(f"""
    CREATE OR REPLACE VIEW candidatos AS
    SELECT DISTINCT SG_UF AS uf, SQ_CANDIDATO AS sq_candidato, NR_CANDIDATO AS numero,
           NM_CANDIDATO AS nome, NM_URNA_CANDIDATO AS nome_urna, SG_PARTIDO AS partido,
           CASE WHEN NR_FEDERACAO NOT IN ('-1', '#NULO', '#NULO#') THEN NM_FEDERACAO END AS federacao, {_agremiacao()} AS agremiacao,
           DS_SIT_TOT_TURNO AS situacao,
           DS_SIT_TOT_TURNO IN ('ELEITO POR QP', 'ELEITO POR MÉDIA') AS eleito
    FROM bruto_candidatos""")
    con.execute(f"""
    CREATE OR REPLACE VIEW votos_candidato_munzona AS
    SELECT SG_UF AS uf, NM_MUNICIPIO AS municipio, {_int('CD_MUNICIPIO')} AS cd_municipio,
           {_int('NR_ZONA')} AS zona, SQ_CANDIDATO AS sq_candidato, NM_URNA_CANDIDATO AS nome_urna,
           SG_PARTIDO AS partido, {_agremiacao()} AS agremiacao,
           {_int('QT_VOTOS_NOMINAIS_VALIDOS')} AS votos
    FROM bruto_votos_candidato""")
    con.execute(f"""
    CREATE OR REPLACE VIEW votos_partido_munzona AS
    SELECT SG_UF AS uf, NM_MUNICIPIO AS municipio, {_int('NR_ZONA')} AS zona,
           SG_PARTIDO AS partido, {_agremiacao()} AS agremiacao,
           {_int('QT_VOTOS_NOMINAIS_VALIDOS')} AS votos_nominais,
           {_int('QT_TOTAL_VOTOS_LEG_VALIDOS')} AS votos_legenda
    FROM bruto_votos_partido""")
    if "bruto_detalhe_apuracao" in {r[0] for r in con.execute("SHOW TABLES").fetchall()}:
        con.execute(f"""
        CREATE OR REPLACE VIEW apuracao_munzona AS
        SELECT SG_UF AS uf, NM_MUNICIPIO AS municipio, {_int('NR_ZONA')} AS zona,
               {_int('QT_APTOS')} AS aptos, {_int('QT_COMPARECIMENTO')} AS comparecimento,
               {_int('QT_ABSTENCOES')} AS abstencoes, {_int('QT_VOTOS_BRANCOS')} AS brancos,
               {_int('QT_VOTOS_NULOS')} AS nulos
        FROM bruto_detalhe_apuracao""")
    con.execute("""
    CREATE OR REPLACE VIEW votos_agremiacao_uf AS
    SELECT uf, agremiacao, SUM(votos_nominais) AS votos_nominais, SUM(votos_legenda) AS votos_legenda,
           SUM(votos_nominais) + SUM(votos_legenda) AS votos_validos
    FROM votos_partido_munzona GROUP BY uf, agremiacao""")
    con.execute("""
    CREATE OR REPLACE VIEW ranking_candidato_uf AS
    SELECT v.uf, v.sq_candidato, c.nome_urna, c.partido, c.agremiacao, c.situacao, c.eleito,
           SUM(v.votos) AS votos
    FROM votos_candidato_munzona v JOIN candidatos c USING (uf, sq_candidato)
    GROUP BY ALL""")
    tabelas = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    if "bruto_vagas" in tabelas:
        col_vagas = next((r[0] for r in con.execute("DESCRIBE bruto_vagas").fetchall()
                          if r[0].startswith("QT_VAGA")), None)
        if col_vagas:
            con.execute(f"""CREATE OR REPLACE VIEW vagas AS
                SELECT SG_UF AS uf, MAX({_int(col_vagas)}) AS vagas FROM bruto_vagas GROUP BY SG_UF""")
    if {"bruto_receitas", "bruto_despesas"} <= tabelas:
        criar_views_financas(con)


def criar_views_financas(con: duckdb.DuckDBPyConnection) -> None:
    """Receitas e despesas contratadas por candidato, cruzadas com a votação."""
    con.execute("""
    CREATE OR REPLACE VIEW financas_candidato AS
    WITH rec AS (SELECT SQ_CANDIDATO AS sq_candidato,
                        SUM(TRY_CAST(REPLACE(VR_RECEITA, ',', '.') AS DOUBLE)) AS receita
                 FROM bruto_receitas GROUP BY 1),
         desp AS (SELECT SQ_CANDIDATO AS sq_candidato,
                         SUM(TRY_CAST(REPLACE(VR_DESPESA_CONTRATADA, ',', '.') AS DOUBLE)) AS despesa
                  FROM bruto_despesas GROUP BY 1)
    SELECT r.uf, r.sq_candidato, r.nome_urna, r.partido, r.situacao, r.votos,
           COALESCE(rec.receita, 0) AS receita, COALESCE(desp.despesa, 0) AS despesa_contratada,
           COALESCE(desp.despesa, 0) / NULLIF(r.votos, 0) AS custo_por_voto
    FROM ranking_candidato_uf r LEFT JOIN rec USING (sq_candidato) LEFT JOIN desp USING (sq_candidato)""")


def resumo(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    """Conferência rápida por UF: vagas, candidatos, eleitos oficiais e votos válidos."""
    return con.execute("""
        SELECT v.uf, v.vagas,
               (SELECT COUNT(*) FROM candidatos c WHERE c.uf = v.uf) AS candidatos,
               (SELECT COUNT(*) FROM candidatos c WHERE c.uf = v.uf AND c.eleito) AS eleitos,
               (SELECT SUM(votos_validos) FROM votos_agremiacao_uf a WHERE a.uf = v.uf) AS votos_validos
        FROM vagas v ORDER BY v.uf""").fetchall()


def executar_etl(recriar_csv: bool = True) -> dict:
    """Roda a ETL completa e devolve um relatório (usado no notebook)."""
    config.DIR_ARTEFATOS.mkdir(parents=True, exist_ok=True)
    extraidos = extrair_csvs() if recriar_csv or not any(DIR_CSV.glob("*.csv")) else list(DIR_CSV.glob("*.csv"))
    with duckdb.connect(str(config.CAMINHO_DUCKDB)) as con:
        contagens = carregar_brutas(con)
        faltando = conferir_colunas(con)
        if not faltando:
            criar_views(con)
        tabela = resumo(con) if not faltando else []
    return {"csvs_extraidos": [p.name for p in extraidos], "linhas": contagens,
            "colunas_faltando": faltando, "resumo": tabela}


if __name__ == "__main__":
    import sys
    try:
        rel = executar_etl()
    except FileNotFoundError as erro:
        print(f"[ETL] {erro}")
        sys.exit(1)
    print(f"CSVs extraídos: {len(rel['csvs_extraidos'])}")
    for t, n in rel["linhas"].items():
        print(f"  {t:28s} {n:>12,}".replace(",", "."))
    if rel["colunas_faltando"]:
        print("[ETL] Colunas faltando (mande esta mensagem para o Claude):", rel["colunas_faltando"])
        sys.exit(1)
    print("\nUF | vagas | candidatos | eleitos (TSE) | votos válidos")
    for uf, vagas, cand, eleitos, vv in rel["resumo"]:
        print(f"{uf} | {vagas} | {cand} | {eleitos} | {vv:,}".replace(",", "."))
    print("\n[ETL] OK — banco gerado em", config.CAMINHO_DUCKDB)
