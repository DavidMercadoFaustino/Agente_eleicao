"""Indicadores de prestação de contas por candidato (lógica pura, sem LLM).

Linguagem obrigatória: "alerta" / "indício" — nunca "irregularidade".
Um indício é só um sinal para conferência humana; a análise jurídica é do TSE.

Regras (planejamento, seção 3.1):
- percentual do limite = despesa contratada / limite de gastos do cargo na UF;
- ALERTA  quando o percentual >= 90%;
- INDICIO quando o percentual > 100% (despesa acima do limite publicado);
- custo por voto = despesa contratada / votos nominais válidos.

O limite de gastos vem da Res. TSE 23.766/2026 e é preenchido em LIMITES_GASTOS
depois de conferido no documento (o RAG serve para citar a fonte na resposta).
"""
from __future__ import annotations

from dataclasses import dataclass, field

LIMIAR_ALERTA = 0.90
LIMIAR_INDICIO = 1.00

# (uf, cargo) -> limite em R$. Preencher a partir da Res. 23.766/2026.
LIMITES_GASTOS: dict[tuple[str, str], float] = {}


@dataclass
class AnaliseFinanceira:
    sq_candidato: str
    nome: str
    uf: str
    votos: int
    receita: float
    despesa_contratada: float
    limite: float | None
    custo_por_voto: float | None = None
    percentual_limite: float | None = None
    nivel: str = "OK"  # OK | ALERTA | INDICIO | SEM_LIMITE
    observacoes: list[str] = field(default_factory=list)

    def texto(self) -> str:
        partes = [f"{self.nome} ({self.uf}): receita R$ {self.receita:,.2f}, "
                  f"despesa contratada R$ {self.despesa_contratada:,.2f}"]
        if self.custo_por_voto is not None:
            partes.append(f"custo por voto R$ {self.custo_por_voto:,.2f}")
        if self.percentual_limite is not None:
            partes.append(f"{self.percentual_limite:.1%} do limite de gastos")
        partes.append(f"nível: {self.nivel}")
        return "; ".join(partes) + ("" if not self.observacoes else ". " + " ".join(self.observacoes))


def analisar(sq_candidato: str, nome: str, uf: str, votos: int, receita: float,
             despesa_contratada: float, limite: float | None) -> AnaliseFinanceira:
    a = AnaliseFinanceira(sq_candidato, nome, uf, int(votos or 0), float(receita or 0),
                          float(despesa_contratada or 0), limite)
    if a.votos > 0:
        a.custo_por_voto = a.despesa_contratada / a.votos
    else:
        a.observacoes.append("Sem votos nominais válidos: custo por voto não se aplica.")
    if a.despesa_contratada > a.receita > 0:
        a.observacoes.append("Despesa contratada maior que a receita declarada "
                             "(pode indicar dívida de campanha; conferir).")
    if not limite:
        a.nivel = "SEM_LIMITE"
        a.observacoes.append("Limite de gastos não cadastrado para esta UF/cargo.")
        return a
    a.percentual_limite = a.despesa_contratada / limite
    if a.percentual_limite > LIMIAR_INDICIO:
        a.nivel = "INDICIO"
        a.observacoes.append("Indício: despesa contratada acima do limite publicado; "
                             "requer verificação, não constitui conclusão de irregularidade.")
    elif a.percentual_limite >= LIMIAR_ALERTA:
        a.nivel = "ALERTA"
        a.observacoes.append("Alerta: despesa contratada próxima do limite de gastos.")
    return a


def analisar_linha(linha: dict, cargo: str = "DEPUTADO FEDERAL",
                   limites: dict | None = None) -> AnaliseFinanceira:
    """Adapta uma linha da view financas_candidato."""
    limites = LIMITES_GASTOS if limites is None else limites
    return analisar(linha["sq_candidato"], linha["nome_urna"], linha["uf"], linha["votos"],
                    linha["receita"], linha["despesa_contratada"], limites.get((linha["uf"], cargo)))
