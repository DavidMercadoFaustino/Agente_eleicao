"""Distribuição de cadeiras no sistema proporcional (Deputado Federal).

Regras implementadas (Código Eleitoral, arts. 106 a 109; decisão do STF nas ADIs 7228/7263/7325,
válida desde as eleições de 2024):

1. Votos válidos = votos nominais + votos de legenda.
2. QE = votos válidos / vagas, desprezada a fração <= 0,5 e arredondada para 1 se > 0,5 (art. 106).
3. QP = votos da agremiação / QE, desprezada a fração (art. 107).
4. Eleitos por QP: candidatos com votação nominal >= 10% do QE, por ordem de votação (art. 108).
5. Sobras com barreira (art. 109): maior média = votos / (cadeiras + 1), entre agremiações com
   >= 80% do QE e que tenham candidato com >= 20% do QE.
6. Sobras finais (STF): quando ninguém mais cumpre 80/20, as cadeiras restantes vão pelas maiores
   médias entre todas as agremiações que ainda tenham candidatos.

Desempates (Res.-TSE 23.677/2021, art. 11, §§ 6º e 7º): empate de médias -> maior votação da
agremiação -> maior votação nominal do candidato que disputa a vaga (por fim, nome, só para ser
determinístico). Empate entre candidatos da mesma agremiação: o mais idoso (art. 12) — não
modelado, pois exige a data de nascimento; aqui vale a ordem alfabética.
Nenhuma agremiação atinge o QE (art. 12-A, ADI 7.228): todas as cadeiras vão pelas médias, primeiro
com a barreira 80/20 e depois sem ela — é exatamente o que o laço de sobras faz quando QP = 0.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Candidato:
    sq: str            # SQ_CANDIDATO
    nome: str
    agremiacao: str    # partido ou federação
    votos: int         # votos nominais válidos


@dataclass
class Eleito:
    candidato: Candidato
    criterio: str      # "QP", "MEDIA_80_20" ou "MEDIA_FINAL"
    rodada: int = 0
    media: float | None = None


@dataclass
class Resultado:
    vagas: int
    votos_validos: int
    qe: int
    qp: dict[str, int]
    cadeiras: dict[str, int]
    eleitos: list[Eleito] = field(default_factory=list)
    rodadas_sobras: list[dict] = field(default_factory=list)

    def resumo(self) -> str:
        linhas = [f"Vagas: {self.vagas} | Votos válidos: {self.votos_validos:,} | QE: {self.qe:,}".replace(",", ".")]
        for ag, n in sorted(self.cadeiras.items(), key=lambda x: -x[1]):
            if n:
                linhas.append(f"  {ag}: {n} cadeira(s) (QP = {self.qp.get(ag, 0)})")
        return "\n".join(linhas)


def quociente_eleitoral(votos_validos: int, vagas: int) -> int:
    """Art. 106: fração <= 0,5 desprezada; > 0,5 arredonda para cima."""
    if vagas <= 0:
        raise ValueError("vagas deve ser positivo")
    inteiro, resto = divmod(votos_validos, vagas)
    return inteiro + 1 if resto * 2 > vagas else inteiro


def distribuir_cadeiras(vagas: int, votos_agremiacao: dict[str, int],
                        candidatos: list[Candidato]) -> Resultado:
    """Calcula QE, QP, eleitos por QP e as duas fases de sobras."""
    votos_validos = sum(votos_agremiacao.values())
    qe = quociente_eleitoral(votos_validos, vagas)
    if qe == 0:
        raise ValueError("QE igual a zero: votos válidos insuficientes")

    por_ag: dict[str, list[Candidato]] = {ag: [] for ag in votos_agremiacao}
    for c in candidatos:
        por_ag.setdefault(c.agremiacao, []).append(c)
    for lista in por_ag.values():
        lista.sort(key=lambda c: (-c.votos, c.nome))

    qp = {ag: v // qe for ag, v in votos_agremiacao.items()}
    cadeiras = {ag: 0 for ag in votos_agremiacao}
    eleitos: list[Eleito] = []
    eleitos_sq: set[str] = set()
    minimo_10 = 0.10 * qe
    minimo_20 = 0.20 * qe
    barreira_80 = 0.80 * qe

    # Fase 1: quociente partidário com mínimo individual de 10% do QE
    for ag, n_qp in qp.items():
        aptos = [c for c in por_ag.get(ag, []) if c.votos >= minimo_10]
        for c in aptos[:n_qp]:
            eleitos.append(Eleito(c, "QP"))
            eleitos_sq.add(c.sq)
            cadeiras[ag] += 1

    def proximo(ag: str, minimo: float) -> Candidato | None:
        for c in por_ag.get(ag, []):
            if c.sq not in eleitos_sq and c.votos >= minimo:
                return c
        return None

    rodadas: list[dict] = []
    restantes = vagas - len(eleitos)
    rodada = 0
    while restantes > 0:
        rodada += 1
        # Fase 2: barreira 80% (agremiação) e 20% (candidato)
        concorrentes = {ag: proximo(ag, minimo_20) for ag, v in votos_agremiacao.items() if v >= barreira_80}
        concorrentes = {ag: c for ag, c in concorrentes.items() if c is not None}
        criterio = "MEDIA_80_20"
        if not concorrentes:
            # Fase 3 (STF): todas as agremiações, qualquer candidato ainda não eleito
            concorrentes = {ag: proximo(ag, 0) for ag in votos_agremiacao}
            concorrentes = {ag: c for ag, c in concorrentes.items() if c is not None}
            criterio = "MEDIA_FINAL"
        if not concorrentes:
            break  # não há mais candidatos para preencher as vagas

        medias = {ag: votos_agremiacao[ag] / (cadeiras[ag] + 1) for ag in concorrentes}
        vencedor = max(medias, key=lambda ag: (medias[ag], votos_agremiacao[ag], concorrentes[ag].votos, ag))
        c = concorrentes[vencedor]
        eleitos.append(Eleito(c, criterio, rodada, round(medias[vencedor], 2)))
        eleitos_sq.add(c.sq)
        cadeiras[vencedor] += 1
        rodadas.append({"rodada": rodada, "criterio": criterio, "agremiacao": vencedor,
                        "media": round(medias[vencedor], 2), "candidato": c.nome,
                        "medias": {ag: round(m, 2) for ag, m in sorted(medias.items(), key=lambda x: -x[1])}})
        restantes -= 1

    return Resultado(vagas, votos_validos, qe, qp, cadeiras, eleitos, rodadas)


def comparar_com_oficial(resultado: Resultado, eleitos_oficiais: set[str]) -> dict:
    """Compara os SQ_CANDIDATO eleitos pelo cálculo com os eleitos oficiais."""
    calculados = {e.candidato.sq for e in resultado.eleitos}
    return {
        "eleitos_calculados": len(calculados),
        "eleitos_oficiais": len(eleitos_oficiais),
        "coincidentes": len(calculados & eleitos_oficiais),
        "so_no_calculo": sorted(calculados - eleitos_oficiais),
        "so_no_oficial": sorted(eleitos_oficiais - calculados),
        "acerto_total": calculados == eleitos_oficiais,
    }
