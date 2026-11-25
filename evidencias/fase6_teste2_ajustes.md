# Fase 6 — 2º teste do agente (qwen3-14b, após ajustes de ferramentas e prompt) — 09/11/2026

| # | Pergunta | Ferramentas | Tempo | Avaliação |
|---|---|---|---|---|
| 1 | Por que o Rui Falcão não foi eleito em SP? | explicar_candidato | 161 s | **Correto** (motivo, números e base legal). |
| 2 | Eleito menos votado e critério | resultado_eleicao | 114 s | Núcleo correto (Sônia Guajajara, 54.207, QP), mas **inventou** "1.386.945 votos válidos" e "4,1 QE" da federação (reais: 3.060.751 e 9,05) e disse que era a mais votada da agremiação (é a 6ª). |
| 3 | O que a lei diz sobre a votação mínima de 10%? | buscar_legislacao | 257 s | **Correto**: citou e transcreveu o CE art. 108. |
| 4 | 5 mais votados de Luís Eduardo Magalhães | esquema_banco -> consultar_banco ×3 | 412 s | **Falhou**: o filtro por nome com acento não casou e o agente desistiu. |

Ajustes após este teste:
- Nova ferramenta determinística votos_por_municipio (busca sem acento e sugestão de nomes parecidos).
- esquema_banco orienta strip_accents(...) ILIKE strip_accents(...).
- Verificador (auditor.py): números e citações da resposta são conferidos contra as saídas das ferramentas;
  resposta reprovada volta ao agente com a lista de problemas e é reescrita uma vez.
  Aplicado à resposta 2 acima, o verificador acusa: "1.386.945, 4,1" e citações não fornecidas pela ferramenta.
