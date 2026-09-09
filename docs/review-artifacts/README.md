# Revisão Kanban MES — 09/09/2026

Implementação em `kanban-mes`. O código e as verificações ficaram no checkout local; não foi feito deploy nem escrita nas bases de produção.

## Alterações

- Cabeçalho compacto e editável, com **Guardar cabeçalho** independente da validação. Os valores visíveis também são guardados ao validar. Alterações pendentes sobrevivem à edição de uma linha.
- Histórico e folha com a composição do OCR original: foto/dados em duas colunas equilibradas, uma coluna abaixo de 900 px, controlos compactos e auditoria acessível em detalhes.
- Assistente **Corrigir linha via OF**, com OF/OV/referência, peças fechadas, chaves estáveis, paginação e proteção contra revisões/snapshots antigos. As escolhas são auditadas como `human / plan-picker`, incluindo no motor V3.
- Pop-ups sem reconstrução do índice; factos validados abrem sem consultar o plano. Pedidos antigos são cancelados, há retry, Escape e gestão de foco.
- Perfil completo congela todas as referências, assume a falta original como produção e exporta somente os filhos positivos. Os zeros permanecem na auditoria. O agrupamento conserva a espessura.
- BaseDados lê o arquivo validado no Postgres por aplicação/período/operador. Falhas e perfis antigos não reconstruíveis devolvem diagnóstico HTML. CPIS pode juntar rascunhos, com prioridade para a versão validada.
- Regresso ao histórico com filtros e página, por `back` e memória de sessão separada por aplicação.
- BaseDados mantém as 11 colunas. A regra de falta das cantoneiras continua QTD − Maq.

## Verificação

`320 passed in 58.84s`

Browser Chromium: fluxo histórico → folha → guardar cabeçalho → escolher referência → validar → histórico filtrado → BaseDados verificado com SQLite descartável e arquivo Postgres simulado. Foram verificadas as três larguras (1440, 1024 e 390 px), navegação rápida entre pop-ups, erro/retry, foco, pesquisa sem resultados, peças fechadas, imutabilidade e limpeza da memória de filtros. Não ocorreram erros JavaScript. [Resultado do browser](browser-verification.json).

Os testes Postgres de escrita dependem de uma base descartável; não foram executados contra a base de produção. A leitura e o benchmark abaixo usaram o Postgres real.

## Desempenho local

Medição: `2026-09-09T12:27:39.907774+00:00`. Snapshot `mtg_59e59d5a2cf4e36a`. 20 repetições, templates aquecidos, HTTP local. Estes tempos não medem o túnel de produção. Os snapshots mudaram durante a implementação; o identificador medido está registado para reprodução.

| Medição | Mediana | p95 |
|---|---:|---:|
| snapshot | 6.57 ms | 7.11 ms |
| order_query | 16.37 ms | 21.81 ms |
| http_review_largest_group | 30.82 ms | 38.00 ms |
| http_alternating_groups | 31.49 ms | 54.30 ms |
| http_validated_offline | 5.71 ms | 6.84 ms |

Reconstruções do índice: **0**. Consultas ao plano no pop-up validado: **0**. Grupos reais medidos: 263, 246, 227 referências. A sonda MTG2 antiga demorava cerca de 1,7–1,8 s. [Dados completos](benchmark.json).

## Histórico antigo

61 folhas de produção analisadas (paragens excluídas). 40 podem ser preparadas sem erro de expansão. 21 folhas têm 86 linhas de perfil completo sem informação suficiente. Não se usou a falta do plano atual, nem se substituíram zeros arquivados. [Diagnóstico por folha e linha](legacy-diagnostic.json).

As linhas não recuperáveis não têm snapshot da validação identificável. Uma exportação que inclua essas folhas apresenta o diagnóstico, sem produzir um ficheiro parcial.

| Folha | Linhas afetadas |
|---|---:|
| 383 | 8 |
| 384 | 1 |
| 390 | 2 |
| 392 | 1 |
| 396 | 3 |
| 399 | 1 |
| 403 | 1 |
| 408 | 3 |
| 409 | 12 |
| 412 | 6 |
| 414 | 7 |
| 416 | 2 |
| 417 | 6 |
| 418 | 11 |
| 422 | 3 |
| 425 | 3 |
| 427 | 1 |
| 430 | 3 |
| 432 | 1 |
| 433 | 7 |
| 437 | 4 |

## Exemplos e comparação visual

Os Excel de exemplo usam dados sintéticos: uma linha normal com 4 unidades e um perfil com faltas 5, 2 e 0 (duas linhas exportadas, 8 metros no total).

- [BaseDados normal](basedados-normal.xlsx)
- [BaseDados de perfil completo](basedados-perfil-completo.xlsx)
- [Galeria comparativa](gallery.html)

O original foi renderizado a partir dos templates do checkout OCR `4710111`, sem modificar o repositório original. As fotografias são cópias locais usadas para comparação de composição; os dados tabulares dos exemplos são sintéticos. As capturas incluem histórico, revisão, validada, seleção por OF, perfil completo, erro e ausência de resultados.

| Largura | Original | MES | Assistente OF |
|---|---|---|---|
| 1440 px | [Folha](original-sheet-1440.png) · [Histórico](original-history-1440.png) | [Folha](review-1440.png) · [Histórico](history-1440.png) | [Original](original-of-picker-1440.png) · [MES](of-picker-1440.png) |
| 1024 px | [Folha](original-sheet-1024.png) · [Histórico](original-history-1024.png) | [Folha](review-1024.png) · [Histórico](history-1024.png) | [Original](original-of-picker-1024.png) · [MES](of-picker-1024.png) |
| 390 px | [Folha](original-sheet-390.png) · [Histórico](original-history-390.png) | [Folha](review-390.png) · [Histórico](history-390.png) | [Original](original-of-picker-390.png) · [MES](of-picker-390.png) |

## Reproduzir

Na raiz do repositório:

```bash
.venv/bin/python -m pytest -q --disable-warnings
.venv/bin/python scripts/benchmark_review.py --env-file .env
.venv/bin/python scripts/verify_review_browser.py --playwright /caminho/para/node_modules/playwright --original /caminho/para/ocr/backend/app/web
```

O benchmark só lê Postgres e cria staging SQLite temporário. A verificação visual simula o plano/arquivo e também usa staging temporário.
