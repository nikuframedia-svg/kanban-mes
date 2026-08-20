# Deploy do Kanban MES

App + túnel Cloudflare como serviços systemd de **utilizador** (sem sudo,
arrancam no boot porque o linger está ativo para o `luis`).

## Instalar / atualizar

```bash
mkdir -p ~/.config/systemd/user
cp ~/projects/kanban-mes/deploy/kanban-mes.service ~/.config/systemd/user/
cp ~/projects/kanban-mes/deploy/kanban-tunnel.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now kanban-mes kanban-tunnel
```

## Ver o URL público

O túnel escreve o URL em `~/projects/kanban-mes/data/tunnel_url.txt` e a app
mostra-o no rodapé da página Folhas. O URL **muda a cada restart do túnel**.

```bash
cat ~/projects/kanban-mes/data/tunnel_url.txt
```

## Logs e gestão

```bash
journalctl --user -u kanban-mes -f      # app
journalctl --user -u kanban-tunnel -f   # túnel
systemctl --user restart kanban-mes kanban-tunnel
systemctl --user stop kanban-tunnel     # desligar o acesso público
```

## Variáveis de ambiente do OCR (`.env`)

Além de `GEMINI_API_KEY` / `MES_OCR_MODEL`:

```bash
# OCR LOCAL (Qwen na GPU do PC da fábrica, via Ollama). Ativar SÓ na
# migração para o PC da empresa: com MES_QWEN_URL definido, o Qwen passa a
# motor PRINCIPAL (sem quotas, imagens nunca saem da infraestrutura) e a
# cadeia Gemini→Claude fica como fallback quando o PC/GPU estiver em baixo.
# Antes de confiar no primário local, correr o benchmark das 7 folhas do
# teste verídico de 19/08 (ground truth visual) Qwen vs Gemini.
#MES_QWEN_URL=http://localhost:11434
#MES_QWEN_MODEL=qwen3.5:9b
#MES_QWEN_TIMEOUT_S=600
#MES_QWEN_NO_THINK=1

# Último recurso PAGO quando toda a cadeia anterior falha (quota, 503, outage).
# Sem chave, este elo não existe e o comportamento é o de sempre.
# Custo ≈ 0,6 cêntimos por folha, só nas falhas.
#ANTHROPIC_API_KEY=
#MES_CLAUDE_OCR_MODEL=claude-haiku-4-5    # subir para claude-sonnet-5 se a leitura dececionar

# Página com menos tinta do que isto é um verso em branco do scanner: não gasta OCR.
#MES_BLANK_INK_THRESHOLD=0.0008

# Pasta onde o sync do Drive deixa os PDFs de kanban (ingest automático).
#MES_DRIVE_DIR=/home/luis/projects/DATARESEARCHMTG
```

Depois de mudar o `.env`: `systemctl --user restart kanban-mes`.

## Nota de segurança

O link trycloudflare é público e **a app não tem autenticação** (decisão do
Luís, 2026-08-07). A única proteção é o URL ser aleatório. Quem tiver o link
pode ver e editar folhas em staging e validar para o Postgres. Para desligar o
acesso público basta parar o serviço `kanban-tunnel`.
