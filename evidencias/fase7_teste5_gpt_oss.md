# Fase 7 — 5º teste: openai/gpt-oss-20b — 10/11/2026

Carga: a 1ª tentativa falhou por falta de memória ("SetProcessWorkingSetSize failed: não existem recursos de
sistema suficientes"; LM Studio com n_slots = 4 e outros modelos carregados). Funcionou após ejetar os demais
modelos e reduzir o paralelismo.

| Pergunta | Ferramentas | Verificação | Tempo | Avaliação |
|---|---|---|---|---|
| Quantas cadeiras o Pavanatto conseguiu sozinho? | resultado_eleicao | ok (falso positivo) | 85 s | **Errado**: ferramenta inadequada e afirmações inventadas ("não atingiu o quociente eleitoral", "menor votação dentro do PL"). Correto: 3.038.438 votos = 8,98 QE -> 8 cadeiras; 1º do PL. |

Conclusão: o gpt-oss-20b não foi melhor que o qwen3-14b na escolha de ferramenta e exigiu mais memória;
o qwen3-14b segue como modelo principal.
Ajustes: regra no prompt "qualquer pergunta sobre um candidato -> explicar_candidato"; o verificador passa a
exigir coerência com a CONCLUSÃO da ferramenta (números da conclusão presentes; sem inverter eleito/não eleito;
sem negar as cadeiras do "puxador").
