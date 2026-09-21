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

Depois de verificar esses resultados, executar sem `--uid` para inventariar as restantes pendentes. `--limit 20 --apply` processa no máximo vinte candidatas. Usar outro nome de relatório por lote. Em falha, conservar a base e o progresso auditado; corrigir a causa antes de retomar. Um rollback de código não autoriza restaurar uma base antiga por cima de trabalho posterior.

## Evidência de aceitação

- Imagens reais: 661 → 15; 681 → 5; 677 → 3, também com inclinação ±2° e ruído.
- 661: recorte real relido por OCR confirmou H92HS4008AT / 4 na posição 12, entre H92HS3009AT e H92HS408P4AT.
- 677: snapshot `mtg_51b541990c2d95d2`, carregado em 15/09/2026. EA8B78=54 + EA8B79=52 → 106; D13F32=20 + D13FP33=20 → 40.
- Testes PostgreSQL usam exclusivamente contentores descartáveis (`RUN_PG_INTEGRATION=1`). O navegador é verificado com uma base SQLite descartável, sem permissão de validar no arquivo real.
