# Avaliação — qwen/qwen3-14b — 12/11/2026 19:24

- Respostas corretas (fatos do gabarito): **79%**
- Trajetória correta (ferramentas esperadas): **93%**
- Aprovadas na verificação final: **93%** (reescritas pelo verificador: 2)
- Tempo médio por pergunta: **104 s**

| # | Categoria | Fatos | Trajetória | Verif. | Revisada | Ferramentas | s |
|---|---|---|---|---|---|---|---|
| 1 | dados (SQL) | X | X | ok |  | votos_por_municipio | 69 |
| 2 | dados (município) | ok | ok | ok |  | votos_por_municipio | 82 |
| 3 | dados (agremiação) | ok | ok | ok |  | eleitos_agremiacao | 26 |
| 4 | cálculo | ok | ok | ok |  | resultado_eleicao | 23 |
| 5 | cálculo | ok | ok | ok |  | eleitos_agremiacao → eleitos_agremiacao → resultado_eleicao | 54 |
| 6 | explicação | ok | ok | ok | sim | explicar_candidato | 221 |
| 7 | explicação | ok | ok | ok |  | explicar_candidato | 63 |
| 8 | explicação | X | ok | ok |  | resultado_eleicao | 110 |
| 9 | legislação | ok | ok | ok |  | buscar_legislacao | 228 |
| 10 | legislação | ok | ok | ok |  | ler_artigo | 68 |
| 11 | legislação | X | ok | X | sim | buscar_legislacao → buscar_legislacao | 341 |
| 12 | finanças | ok | ok | ok |  | analisar_financas | 45 |
| 13 | fora de escopo | ok | ok | ok |  | — | 25 |
| 14 | memória (2 turnos) | ok | ok | ok |  | eleitos_agremiacao | 97 |

## Falhas
- **1. Quais foram os 3 candidatos a deputado federal mais votados em SP?** — faltou: ['samia bomfim'] proibido: [] | resposta: 'Os três candidatos a deputado federal mais votados em São Paulo (SP) foram:\n\n1. **LUCAS PAVANATO** (PL) — 785.870 votos — situação: ELEITO POR QP  \n2. **ERIKA HILTON** (FEDERAÇÃO PSOL REDE) — 618.361 votos — situação: ELEITO POR QP  \n3. **TABATA AMARAL** (PSB) — 238.734 votos — situação: ELEITO POR '
- **8. Por que a Federação PSOL REDE ficou com menos cadeiras que o seu quociente partidário em SP?** — faltou: ['10 ?%'] proibido: [] | resposta: 'A Federação PSOL REDE obteve um Quociente Partidário (QP) de 9, mas ficou com apenas 6 cadeiras. Isso ocorre porque o número de cadeiras obtidas por uma agremiação não sempre corresponde exatamente ao QP calculado — especialmente quando há sobras a serem distribuídas entre os partidos.\n\nNo caso da F'
- **11. Qual o critério de desempate quando dois partidos têm a mesma média nas sobras?** — faltou: ['maior vota'] proibido: [] | resposta: 'Quando dois ou mais partidos têm a mesma média nas sobras, o critério de desempate é determinado pelo **número de votos nominais recebidos pela candidata ou candidato que disputa a vaga**, conforme estabelecido no [Res.-TSE 23.677/2021, art. 11, § 7º](https://www.tse.jus.br/legislacao/resolues/resol'
