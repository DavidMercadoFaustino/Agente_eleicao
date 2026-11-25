# Fase 6 — 1º teste do agente (qwen3-14b, sem verificação) — 09/11/2026

| # | Pergunta | Ferramentas | Tempo | Avaliação |
|---|---|---|---|---|
| 1 | Por que o Rui Falcão não foi eleito em SP? | explicar_candidato | 221 s | **Errado no motivo**: disse que 31,5% do QE "não atingiu a barreira de 20%" (31,5% > 20%). Motivo real: 12º da lista, agremiação com 11 cadeiras. Números corretos. |
| 2 | Quem foi o eleito menos votado e por qual critério? | resultado_eleicao | 139 s | **Errado no critério**: Sônia Guajajara (54.207) está certa, mas entrou por QP, não por sobras; citou art. 12-A sem relação com o caso. |
| 3 | O que a lei diz sobre a votação mínima de 10%? | nenhuma | 62 s | **Sem ferramenta**: respondeu de memória; citou Res. 23.677 art. 11 (o correto é CE art. 108 / Res. 23.677 art. 8º). |
| 4 | 5 mais votados da Bahia | esquema_banco -> consultar_banco | 157 s | Correto (conferido no banco). |
| 5 | E no Rio de Janeiro? | nenhuma | 36 s | Correto: recusou fora do escopo. |

Diagnóstico: o LLM (14B local) interpreta mal saídas longas e "completa" lacunas com conhecimento próprio.
Correções aplicadas:
- explicar_candidato passa a abrir com uma linha CONCLUSÃO e mostra os mínimos como SIM/NÃO;
  a base legal passa a ser específica do caso (antes era uma lista genérica que induzia o art. 12-A).
- resultado_eleicao informa o eleito menos votado e o critério de eleição dele.
- prompt: toda pergunta no escopo exige ferramenta; não contradizer a CONCLUSÃO.
- "/no_think" enviado na mensagem do usuário (o Qwen3 ignora no prompt de sistema) para reduzir a latência.
Próximo passo: verificador (auditor) que confere números e citações da resposta contra as saídas das ferramentas.
