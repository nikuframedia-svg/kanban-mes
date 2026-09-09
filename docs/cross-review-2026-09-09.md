# Revisão do cross — 9 de setembro de 2026

## Parecer

**MTG2:** favorável para publicar as alterações revistas, com as limitações de avaliação abaixo. **Cantoneiras (MTG3):** o V3 ainda não está aprovado como substituto do motor legado: a avaliação reproduzida mostra uma regressão na escolha da OF. Publicação e ativação são operações distintas; o código mantém `legacy` por omissão. Não foi alterado o motor de uma instalação nem recalculada produção existente.

Após a revisão, foi autorizada a publicação integral dos dois projetos na branch `codex/cross-v3-rollout`, com PRs em rascunho para `main`. A regressão de OF das cantoneiras permanece documentada e impede recomendar a ativação desse V3. A publicação do código não altera o motor configurado nas instalações.

## Âmbito e contratos verificados

Revisão do Cross V3 existente face a `main` e das alterações posteriores ainda no workspace: reconstrução da evidência, geometria, candidatos, pontuação, continuidade, desempate histórico, aplicação auditada, seleção manual, validação, congelamento de referências e materialização para exportação.

- O V3 reconstrói observações do OCR original e das correções humanas da geração atual. Valores preenchidos automaticamente pelo cross não se tornam nova evidência.
- A seleção explícita de uma referência exige chave e snapshot válidos. O editor grava também os campos vazios da identidade, preserva quantidades e lotes e aplica controlo de revisão. No V3, uma associação antiga é recalculada e auditada, conforme o comportamento já definido.
- Histórico e falta positiva servem para desempatar candidatos com a mesma pontuação; não descontam produção MES às quantidades do planeamento.
- Perfil Completo usa todas as referências da OF e do perfil físico escolhido. Falta positiva torna-se quantidade assumida; zero fica na auditoria e não gera linha de exportação. Falta negativa/desconhecida impede a validação.
- A identidade e os filhos ficam guardados na validação. Exportação e consulta de folhas validadas usam os factos congelados. Dados antigos incompletos só podem ser recuperados de um snapshot identificado; a falta de prova origina diagnóstico.
- A coluna O do MTG2 é metadado de apresentação/exportação. A identidade física mantém as dimensões e a espessura.

## Problemas encontrados e corrigidos nesta revisão

1. **Cantoneiras: colisão de espessuras decimais.** A nova anexação de factos reutilizava um índice legado que removia separadores decimais. `L60X60X2.9` podia colidir com `L60X60X29`. O índice, a normalização, a comparação legada e a consulta usam agora dimensões preservadas; a abreviatura `60x2,9` continua equivalente a `L60X60X2.9`. O teste percorre V3 e legado e confirma que o grupo exportável contém apenas a referência correta.
2. **Ambos: referências sem chave ou com chave duplicada.** A expansão saltava essas entradas e podia declarar válido um grupo incompleto. Agora assinala erro e deixa quantidade total e metros desconhecidos.
3. **Ambos: todos os comprimentos desconhecidos.** A soma podia mostrar zero metros apesar de todos os comprimentos estarem em falta. Agora o total permanece desconhecido e parcial; não calcula desperdício.

## Avaliação congelada reproduzida

| Aplicação / medida | Legado | V3 |
|---|---:|---:|
| MTG2 — OF correta | 33/34 | 34/34 |
| MTG2 — referência OF/modelo correta | 21/22 | 22/22 |
| Cantoneiras — OF correta | 49/82 | 44/82 |
| Cantoneiras — referência OF/modelo correta | 35/45 | 35/45 |
| Cantoneiras — perfil correto (anotação visual) | 56/83 | 83/83 |
| Cantoneiras — OF correta no conjunto reservado | 14/27 | 8/27 |

A regressão das cantoneiras já estava documentada no commit V3 anterior à última alteração da interface; esta revisão confirmou-a. As correções acima não alteram os pesos V3 nem tentam afinar o motor sobre o conjunto reservado.

O MTG2 tem uma amostra pequena, usada no desenvolvimento, e algumas anotações fora do universo corrente ficam excluídas conforme o relatório. Não é uma garantia de acerto em novas folhas. Os indicadores de confiança são diagnósticos, não probabilidades calibradas de acerto. Nas cantoneiras, acertar o perfil ou atribuir um candidato a todas as linhas não compensa escolher a OF errada. É necessária uma melhoria avaliada com dados independentes antes de recomendar ativação desse V3.

Resultados completos da avaliação desta revisão: [JSON do benchmark](cross-review-benchmark.json). A avaliação usa os snapshots congelados da fixture; não é uma medição de acerto contra o planeamento de hoje. O comparador legado é o código congelado da avaliação, não a versão com as correções desta revisão.

## Verificação

- MTG2: **448 testes passaram**, em 77,24 s. Os **5 testes de integração de numeração PostgreSQL** passaram depois, em 5,55 s, num container PostgreSQL 16 descartável (`RUN_PG_INTEGRATION=1`); não usaram a base de produção. Total MTG2: **453 testes**.
- Cantoneiras: **326 testes passaram**, em 60,07 s.
- Dez casos novos de regressão no total: chaves inválidas/repetidas, metros totalmente desconhecidos e espessura decimal nos dois motores das cantoneiras.
- `git diff --check` passou nos dois repositórios. Os benchmarks foram repetidos com as fixtures congeladas e os resultados acima reproduzidos.

Não houve escrita na base PostgreSQL de produção, novo OCR, deploy ou merge nesta revisão.
