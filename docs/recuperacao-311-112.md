# Correção dos incidentes 311 e 112

Esta entrega altera o código dos dois sistemas. A instalação e a recuperação da base Windows têm de ser executadas no PC: esta sessão não tem acesso de consola. As correções são publicadas em branches próprias no GitHub, para revisão antes da instalação. O pacote contém bundles Git, commits e fingerprints verificáveis; não contém bases reais, credenciais ou confirmações inventadas.

## Comportamento

**Cantoneiras:** ao abrir uma folha com campos de identificação em falta, a recuperação arranca automaticamente em segundo plano e lê apenas a faixa superior. Há uma tentativa automática por imagem/geração; uma falha apresenta uma opção de repetição. Cabeçalhos coerentes não mostram controlos adicionais. Guarda observações, propostas, identidade da imagem/geração e auditoria automática. Preenche campos vazios apenas quando a referência é inequívoca e não existe decisão humana. Preserva OCR original, linhas, rodapé e associações ao plano. A data final mantém o dia útil anterior à digitalização. Uma divergência recuperada exige confirmar a regra ou guardar uma exceção manual. Um bloqueio de ficheiro partilhado entre processos permite apenas uma recuperação de cada vez.

Na folha 311, conferir operador, 2849, Peddi 8 e M; a leitura esperada é 21/08/2026 no papel e 20/08/2026 pela regra. As 13 linhas devem permanecer iguais. Os testes simulam essa resposta OCR; a leitura do OCR instalado no Windows ainda precisa de ser verificada.

**Perfis:** OpenCV deteta e agrupa segmentos da grelha, corrige a inclinação e remove os traços numa cópia. Uma grelha incompleta ou uma remoção sem confiança devolve contagem não verificada e não desencadeia recuperação de linhas. A imagem real da folha 112 dá seis linhas. As variantes com inclinação até três graus e ruído também dão seis.

«Linhas excluídas» mantém os índices originais. Fora do âmbito conta como linha física; duplicação identifica uma linha incluída; artefacto e duplicação não acrescentam linhas físicas. Exclusões antigas ficam por justificar. O restauro mantém a mesma linha, correções e histórico, e é respeitado pelo cruzamento seguinte. A confirmação humana fica vinculada à imagem, geração OCR e estrutura. Uma contagem recalculada coerente com as linhas incluídas e as exclusões justificadas resolve-se automaticamente, mesmo quando a estimativa antiga estava errada. Uma confirmação humana antiga que tenha perdido validade não é reutilizada; a imagem pode resolver a conferência de forma independente.

Ao abrir uma folha, as coberturas antigas são recalculadas automaticamente. Se faltarem linhas de OCR, uma segunda leitura tenta identificá-las: só acrescenta linhas novas quando todas as existentes têm correspondência única e ordenada no OCR original. Preserva as linhas atuais e as correções humanas; em dúvida, apresenta a exceção. Nunca desfaz exclusões automaticamente. Novas observações ficam auditadas e são usadas pelo cruzamento, sem substituir o OCR original.

Na folha 112, preservar as quatro linhas incluídas e rever as exclusões das linhas originais **1 e 3**. O inventário mostra os respetivos eventos da auditoria Windows. A interface pede apenas a decisão sobre essas exclusões, sem pedir que o utilizador conte novamente o papel. Não confirmar quatro nem restaurar ou justificar automaticamente essas duas linhas.

## Instalação específica

Extrair o pacote completo numa pasta fora dos dois repositórios, por exemplo `C:\OCR-Suite\review-recovery`. Abrir PowerShell no PC Windows. O script usa os auxiliares existentes em `C:\OCR-Suite\kit`, verifica os caminhos físicos em `F:\Apps\OCR-Suite` e recusa versões ou alterações locais diferentes das esperadas.

```powershell
cd C:\OCR-Suite\review-recovery
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_review_recovery.ps1 -Mode Check
```

O modo Check é o padrão. Verifica processos, motor legacy, commits, fingerprints, filas OCR, integridade dos bundles, ancestralidade e inventário direto das bases; prepara os wheels Windows de OpenCV/NumPy. Não recupera folhas. Os inventários ficam em `C:\OCR-Suite\saida\review-recovery\<data-hora>`; os 177 casos da análise anterior eram candidatos, não um total fixo de alterações.

Quando Check terminar e os inventários estiverem revistos:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_review_recovery.ps1 -Mode Apply
```

Apply repete as verificações, para os dois processos identificados, verifica novamente a fila e cria backups SQLite consistentes com `integrity_check` e SHA-256. Instala os commits exatos em checkout destacado, preservando as branches locais. Mantém o motor atual e verifica processo, health local/público, fingerprint e páginas principais, incluindo os dois incidentes. Não executa recuperação nem valida folhas. Não reutilizar `update_review.ps1` antigo.

Se falhar, o instalador para o código novo, tenta repor o commit anterior e verifica o arranque. **Não restaura a base automaticamente**, para preservar trabalho feito após o reinício. Os backups e os relatórios permanecem na pasta do lote. A sintaxe PowerShell e os auxiliares de backup foram testados em Linux; a execução real do instalador Windows continua pendente.

## Recuperação após verificar as páginas

Ao abrir os incidentes no navegador, a verificação automática inicia-se sem clicar num botão de recuperação. Se o cabeçalho 311 já tiver sido recuperado dessa forma, o comando em lote ignora-o; isso é esperado.

O padrão de ambos os comandos é inventário, sem alterações. Cada execução com `--apply` exige um caminho de backup novo, verifica a revisão antes de gravar e deixa as folhas em revisão. Executar primeiro os dois incidentes, conferir os resultados no navegador e só depois avançar.

```powershell
$cant = 'C:\OCR-Suite\kanban-mes'
$perf = 'C:\OCR-Suite\kanban-mes-mtg2'
$out = 'C:\OCR-Suite\saida\review-recovery'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'

& "$cant\.venv\Scripts\python.exe" "$cant\scripts\recover_headers_pending.py" --uid 745a6fadc3f4 --limit 1
& "$perf\.venv\Scripts\python.exe" "$perf\scripts\recover_coverage_pending.py" --uid dfb96386112e --limit 1

& "$cant\.venv\Scripts\python.exe" "$cant\scripts\recover_headers_pending.py" --uid 745a6fadc3f4 --limit 1 --apply --backup "$out\311-$stamp.db" > "$out\311-$stamp.json"
if ($LASTEXITCODE -ne 0) { throw 'Recuperacao 311 interrompida; consultar relatorio' }
& "$perf\.venv\Scripts\python.exe" "$perf\scripts\recover_coverage_pending.py" --uid dfb96386112e --limit 1 --apply --backup "$out\112-$stamp.db" > "$out\112-$stamp.json"
if ($LASTEXITCODE -ne 0) { throw 'Recuperacao 112 interrompida; consultar relatorio' }
```

Depois da folha 311, escolher dez folhas representativas do inventário (datas, máquinas, cabeçalhos parcial/totalmente vazios e decisões humanas), passando `--uid UID` uma vez por folha e `--limit 10`. Conferir o resultado; depois usar `--limit 20` sem filtro de UID, sempre com novo backup e relatório. A execução é sequencial. Resultados: `recovered` (preenchido), `partial` (ainda vazio), `review` (dúvida/divergência), `failed` ou `conflict`. Falha ou conflito interrompe o lote. Repetir retoma os casos ainda elegíveis; tentativas concluídas são ignoradas. Casos parciais e dúvidas ficam para revisão humana.

Em Perfis, repetir lotes de vinte até o inventário deixar de apresentar coberturas antigas pendentes. Consultar a área de exclusões e confirmar no papel os casos assinalados. Nenhum comando confirma contagens, exclui/restaura linhas ou altera folhas validadas.

Os backups são SQLite, não cópias dos ficheiros de imagem; as imagens originais permanecem intactas. Em recuperação interrompida, a auditoria de cada folha já concluída permanece gravada. Uma eventual reposição de dados requer avaliação das alterações posteriores, não a substituição cega da base.

## Verificação de desenvolvimento

- Suites existentes e novos testes de contagem, grelha danificada, exclusões, restauro, concorrência, proteção de validadas, falha de OCR, idempotência, retoma e backups WAL.
- Chromium em 1440 e 390 px, usando bases temporárias, referências sintéticas e OCR simulado. Arranque automático, desaparecimento dos controlos em folhas coerentes, justificação/restauro sem nova contagem, confirmação de data e exceção manual após novo cruzamento. Conclusão de trabalho em segundo plano não recarrega a página sobre texto que o utilizador esteja a escrever.
- Os testes de validação e exportação usam armazenamento descartável/simulado. Nenhuma folha real foi validada como teste.

Os commits, resultados finais e hashes estão no `release.json` e no `ENTREGA.md` do pacote. Os bundles permitem também publicar as branches num terminal com acesso GitHub, antes da instalação:

```powershell
git -C C:\OCR-Suite\kanban-mes fetch C:\OCR-Suite\review-recovery\cantoneiras.bundle refs/heads/codex/cantoneiras-recuperacao-cabecalhos:refs/heads/codex/cantoneiras-recuperacao-cabecalhos
git -C C:\OCR-Suite\kanban-mes push origin codex/cantoneiras-recuperacao-cabecalhos
git -C C:\OCR-Suite\kanban-mes-mtg2 fetch C:\OCR-Suite\review-recovery\perfis.bundle refs/heads/codex/perfis-conferencia-linhas:refs/heads/codex/perfis-conferencia-linhas
git -C C:\OCR-Suite\kanban-mes-mtg2 push origin codex/perfis-conferencia-linhas
```
