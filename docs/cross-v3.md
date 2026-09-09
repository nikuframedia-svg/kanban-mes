# Cross V3 — Cantoneiras / Rapid 20T

O V3 do TPL102 reconstrói a evidência da transcrição original e das edições humanas da mesma extração. Não usa substituições automáticas anteriores como novas observações. Escolhe globalmente OF/perfil e aplica OF, OV, cliente, perfil e modelo canónicos, mesmo quando há pouca evidência. A revisão conserva a origem, as alternativas e a indicação de confiança estimada. Sem plano utilizável, conserva os valores atuais.

## Contratos do setor

O adaptador interpreta `60x5` e `L60x5` como `L60X60X5`, conserva as três dimensões de `100x50x6` e distingue `2,9` de `29`. `perf_comp` e o alias antigo `comp_mm` são marcas de Perfil Completo; nunca fornecem comprimento de corte. Quantidade, visto, metros e horas escritos são preservados.

Perfil Completo escolhe o conjunto OF/perfil no snapshot atual e limpa o modelo singular. A expansão mantém todas as referências, incluindo falta zero, pai agregado e filhos de proveniência. Os exports incluem apenas quantidades positivas. Falta inválida ou desconhecida bloqueia a validação; comprimento desconhecido mantém metragem parcial. A falta vem exclusivamente do contrato MTG3 `QTD − Maq.`; não se desconta produção MES nem se recupera a coluna MTG2 «Qtd em Falta».

O histórico consulta somente snapshots MTG3 e desempata apenas candidatos com igual evidência. Prefere a data mais próxima, o passado em empate e a última carga do mesmo dia. Contexto posterior à folha aparece como aproximação. O índice preparado e as pesquisas são reutilizáveis; evidência, escolhas temporais e resultados pertencem a cada folha.

Atividades e linhas vazias interrompem o contexto e não geram produção. Linhas apagadas conservam índices e auditoria, permanecendo excluídas; produção interna continua elegível. Chapa/nesting e paragens conservam o tratamento existente.

As migrações aditivas 4 e 5 acrescentam geração e fronteira de evidência, preservando UIDs, números públicos e contador. Escrita de dados, cruzamento e auditoria usa a mesma transação com controlo de revisão. Seleções explícitas válidas prevalecem; seleções expiradas são recalculadas e auditadas. A escolha manual e a validação voltam a verificar snapshot e revisão.

## Avaliação reproduzível

O corpus em `tests/fixtures/cross_v3` contém sete transcrições originais, 83 linhas OCR correspondentes a 80 linhas físicas e anotações feitas diretamente das fotografias. Três fragmentações do OCR explicam a diferença. Imagens e nomes de operadores não são publicados. Os snapshots congelados têm 64.219 ou 73.772 referências. O legado é executado a partir do código congelado do commit `ca557f0`, sem depender de alterações posteriores.

Os documentos de desenvolvimento, diagnóstico de 20 de agosto e avaliação reservada de 27 de agosto estão identificados no manifesto. Os parâmetros `cross-v3-cantoneiras-2` foram fixados antes de abrir a avaliação reservada; não foram afinados com os seus resultados.

| Medida no snapshot de 7 de setembro | Legado | V3 |
|---|---:|---:|
| Linhas elegíveis com associação | 83/83 | 83/83 |
| OF correta, rótulos presentes no plano | 49/82 | 44/82 |
| Referência OF/modelo correta | 35/45 | 35/45 |
| Perfil correto, anotação visual | 56/83 | 83/83 |
| OF correta, avaliação reservada de 27/08 | 14/27 | 8/27 |

**O V3 melhora a geometria e perde acerto de OF nesta amostra; associação completa não significa acerto completo.** Numa folha reservada, a OF escrita 264324 não tem o perfil escrito 55x5 no snapshot (tem 55x4); a escolha de outro conjunto afeta a continuidade das linhas seguintes. Noutra folha, o OCR omitiu todas as OFs e modelos escritos, limitando os dois motores. Estes erros estão expostos no relatório, sem usar substituições como verdade nem excluir divergências do denominador de OF.

Na mesma máquina Linux e na amostra de 17 linhas/73.772 referências, as medianas com caches quentes foram 1,873 s no V3 e 1,746 s no legado: **1,072 vezes**, abaixo do limite 1,5. A primeira execução V3 demorou 7,860 s. A medição compara uma passagem por motor e exclui I/O PostgreSQL e carregamento histórico; não é uma promessa de latência no Windows.

```bash
.venv/bin/python scripts/benchmark_cross_v3.py --report docs/benchmark-cross-v3-current.json
.venv/bin/python scripts/benchmark_cross_v3.py --performance --report docs/benchmark-cross-v3-performance.json
```

A suite completa passou com **300 testes em 58,19 segundos**. Os testes cobrem geometrias, aliases, Perfil Completo com falta zero/inválida, evidência e geração, seleções explícitas, atividades, apagadas, invariância de repetição/reordenação/duplicação, equivalência da pesquisa exaustiva, revisão HTTP e conflitos durante a aplicação.

## Preparação e ativação

O valor por omissão de `MES_CROSS_ENGINE` é `legacy`. Publicar código não ativa o V3. Cada instalação prepara o seu próprio relatório sobre a sua própria base SQLite; nunca se transporta a base ou o relatório Linux para Windows.

Com o ambiente PostgreSQL do serviço carregado:

```bash
.venv/bin/python scripts/recalculate_cross_v3.py --db data/app.db --report data/cross-v3/review.json
.venv/bin/python scripts/recalculate_cross_v3.py --db data/app.db --check --from-report data/cross-v3/review.json
# Com o serviço parado, depois de rever o relatório:
.venv/bin/python scripts/recalculate_cross_v3.py --db data/app.db --apply --from-report data/cross-v3/review.json --result data/cross-v3/result.json
```

O relatório identifica aplicação, plataforma, instalação, commit, código, parâmetros e snapshot; inclui alterações, proveniência, auditoria e factos derivados antes/depois. O dry-run usa uma cópia consistente. Validadas, OCR pendente e folhas sem produção elegível ficam excluídos. A aplicação verifica novamente estado, template, geração, revisão, transcrição, dados e evidência humana, incluindo dentro da transação. Cria backup SQLite com `quick_check`, verifica preservação e correspondência com o relatório e exige zero conflitos antes de ativar.

No Windows usar `cross_v3.ps1` do repositório `pc-suite-kit`, com `Prepare` e `Apply`. As versões aprovadas estão em `cross_v3_releases.json`; relatórios e temporários ficam em `data`, fisicamente em F:. O kit verifica caminhos físicos, propriedade do PID, health local e público com plataforma `win32`, commit e fingerprint do processo. A execução nativa continua a ser uma verificação feita no próprio PC.

`GET /health` expõe aplicação, motor, plataforma, commit e fingerprint capturados no arranque, sem dados de folhas ou credenciais. `MES_CROSS_ENGINE=legacy` e reinício revertem os próximos cruzamentos; não desfazem alterações já materializadas. Uma reposição SQLite deve ser feita com o serviço parado e nunca deve sobrescrever edições posteriores. O recálculo não executa OCR nem escreve no PostgreSQL.
