#!/usr/bin/env bash
# Túnel Cloudflare (quick tunnel) para o Kanban MES.
# Captura o URL *.trycloudflare.com do output e escreve-o em data/tunnel_url.txt
# para a app o mostrar no rodapé. O URL muda a cada arranque do túnel.
set -euo pipefail

APP_DIR=/home/luis/projects/kanban-mes
URL_FILE="$APP_DIR/data/tunnel_url.txt"
CLOUDFLARED=/home/luis/.local/bin/cloudflared

mkdir -p "$APP_DIR/data"
: > "$URL_FILE"

"$CLOUDFLARED" tunnel --url http://127.0.0.1:8100 --no-autoupdate 2>&1 | while IFS= read -r line; do
    printf '%s\n' "$line"
    url=$(printf '%s' "$line" | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | head -1 || true)
    if [[ -n "$url" ]]; then
        printf '%s\n' "$url" > "$URL_FILE"
    fi
done
