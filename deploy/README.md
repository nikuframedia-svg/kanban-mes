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

## Nota de segurança

O link trycloudflare é público e **a app não tem autenticação** (decisão do
Luís, 2026-08-07). A única proteção é o URL ser aleatório. Quem tiver o link
pode ver e editar folhas em staging e validar para o Postgres. Para desligar o
acesso público basta parar o serviço `kanban-tunnel`.
