# Fase 7 — 3º teste (qwen3-14b + verificador) — 10/11/2026

| # | Pergunta | Ferramentas | Verificação | Tempo | Avaliação |
|---|---|---|---|---|---|
| 1 | 5 mais votados de Luís Eduardo Magalhães | votos_por_municipio | ok | 115 s | **Correto** (antes falhava). |
| 2 | Por que o Rui Falcão não foi eleito em SP? | explicar_candidato | ok | 130 s | **Correto**. |
| 3 | Quantas cadeiras o "Pavanatto" conseguiu sozinho? | explicar_candidato | ok | 44 s | Não achou o nome (erro de digitação: o correto é PAVANATO). |
| 4 | idem, "Pavanato" | explicar_candidato | **ok (falso positivo)** | 81 s | **Errado**: disse que ele "conquistou 19 cadeiras sozinho". Correto: os votos dele = 8,98 QE -> 8 cadeiras de QP; as 19 são da agremiação. |

Achado importante para o relatório: o verificador determinístico confere *números e citações*, mas não o
*raciocínio*. Na resposta 4 todos os números existiam na saída da ferramenta (19, 3.038.438, 19,01 QE), só que
foram ligados de forma errada. Isso justifica a avaliação com LLM-juiz (fase de avaliação) e reforça a estratégia
de entregar a conclusão pronta pela ferramenta.

Ajustes:
- explicar_candidato ganha a linha "CONTRIBUIÇÃO INDIVIDUAL (puxador de votos)": votos do candidato ÷ QE,
  cadeiras inteiras equivalentes e % dos votos da agremiação; acima de 100% do QE mostra "8,98 vezes o QE".
- Busca de candidato tolerante a erro de digitação (difflib): "Pavanatto" -> LUCAS PAVANATO, com aviso.
