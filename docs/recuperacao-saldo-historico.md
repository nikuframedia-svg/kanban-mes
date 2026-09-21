# Saldo histórico e completude da leitura

A lista apresenta primeiro a captura mais recente, com desempate por número e UID decrescentes. Os números não mudam.

Um X usa o último snapshot carregado **antes do início do dia de produção em Europe/Lisbon**. A data efetiva mantém a regra existente e as exceções humanas. `cross_check.rows[].quantity_basis` guarda data, snapshot, versão, impressão digital das decisões e diagnóstico; `plan_refs` guarda as referências e quantidades usadas na revisão, validação, PostgreSQL e exports. Uma nova carga do plano atual não altera esta base. Uma data, identidade ou decisão diferente exige recálculo. Quantidade desconhecida não é zero.

Em TPL102, a conferência verifica os limites do cabeçalho e do rodapé independentemente da sequência das 15 linhas de produção. Só uma grelha comprovada fornece contagem. Uma omissão é relida numa faixa com linhas vizinhas: as linhas existentes mantêm os índices, os valores e a auditoria; a nova linha recebe um índice novo e a posição física. O OCR original permanece intacto. Exclusões antigas sem motivo são apresentadas e impedem validar; o restauro prevalece sobre a exclusão anterior.

Ao abrir uma folha pendente, a aplicação agenda uma única verificação automática quando há cobertura antiga/inexistente, cabeçalho incompleto ou saldo histórico por calcular. Cada gravação usa a revisão atual. Folhas validadas ficam excluídas.

## Instalação e recuperação

Publicar os dois main e executar no PC Windows o update habitual, que deve concluir o backup SQLite consistente e verificado antes de atualizar:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\OCR-Suite\kit\update_all.ps1"
```

Confirmar `/health` dos dois sistemas: commit, fingerprint, motor e plataforma. Esperar pelas filas OCR antes de instalar. Abrir lista, imagem e referências antes de iniciar lotes. As correções de imagem anteriores estão incluídas nesta atualização.

O comando abaixo usa os mesmos serviços do navegador, serialmente. Sem `--apply`, apenas inventaria. O relatório JSONL é acrescentado após cada folha, e o inventário é guardado à parte. Repetir o comando recalcula a elegibilidade; não duplica linhas recuperadas. Conflitos e falhas interrompem o lote. Nenhuma folha é validada.

```powershell
.\.venv\Scripts\python.exe scripts\recover_pending_review.py --base-url https://cantoneiras.nikufra.ai --report recuperacao-primeiros.jsonl --uid d60031bacd01 --uid 37ad462fb044 --restore 37ad462fb044:0
```

Acrescentar `--apply` para executar os primeiros casos. A primeira linha da 681 fica com OF por confirmar; a escolha incompatível da linha seguinte não é herdada. A auditoria anterior da exclusão fica no relatório e em `/sheet/{uid}/rows/0/audit`.

A folha 686 (`86781f537e79`) também é recuperável ao abrir ou através deste comando, acrescentando `--uid 86781f537e79`. O ajuste está sobre a atualização anterior: quem ainda não a instalou executa o update uma única vez para obter ambas.

Depois de verificar esses resultados, executar sem `--uid` para inventariar as restantes pendentes. `--limit 20 --apply` processa no máximo vinte candidatas. Usar outro nome de relatório por lote. Em falha, conservar a base e o progresso auditado; corrigir a causa antes de retomar. Um rollback de código não autoriza restaurar uma base antiga por cima de trabalho posterior.

## Evidência de aceitação

- Imagens reais: 661 → 15; 681 → 5; 677 → 3; 686 → 7, também com inclinação ±2° e ruído.
- 661: recorte real relido por OCR confirmou H92HS4008AT / 4 na posição 12, entre H92HS3009AT e H92HS408P4AT.
- 677: snapshot `mtg_51b541990c2d95d2`, carregado em 15/09/2026. EA8B78=54 + EA8B79=52 → 106; D13F32=20 + D13FP33=20 → 40.
- 686: a terceira linha física é L65×5 / X. O OCR original colocou o perfil na linha A18F31 e omitiu o X. A recuperação conserva as seis linhas existentes, acrescenta a linha completa na posição 3 e regista a leitura do perfil deslocado como evidência, sem alterar o OCR original nem ultrapassar decisões humanas. Snapshot `mtg_024ee2ac695a038d` (17/09): A18B108=24 + A18B109=24 → 48 peças para a produção de 18/09.
- Testes PostgreSQL usam exclusivamente contentores descartáveis (`RUN_PG_INTEGRATION=1`). O navegador é verificado com uma base SQLite descartável, sem permissão de validar no arquivo real.

## Proteção de linhas de perfil completo

O contador TPL102 versão 4 mede componentes de escrita depois de remover a grelha, sem diluir referências curtas pelas células vazias. Descarta pontos isolados e resíduos junto aos divisores. A recuperação distingue referências com quantidade de perfis com X, exige vizinhos inequívocos e mantém os identificadores existentes. Uma resposta OCR com contagem ou ordem incompatível passa ao próximo motor/modelo configurado; esgotada a cadeia, a folha permanece para revisão. Não se fornecem referências do plano ao OCR para preencher o papel.

As novas observações são guardadas em `_coverage_recovery.observations` e, quando a releitura comprova deslocação de um perfil para a linha anterior, em `anchor_observations`. Ambas ficam vinculadas à imagem e à geração OCR e são aplicadas antes das decisões humanas. Os controlos de confiança do motor de cruzamento mantêm-se: completar a leitura não aprova uma associação duvidosa.
