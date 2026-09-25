"""Diagnóstico de latência PC → Postgres (só leituras, sem credenciais no ecrã).

Corre no PC da fábrica, dentro da pasta da app:

    .venv\\Scripts\\python scripts\\diagnose_pc_latency.py            (Windows)
    .venv/bin/python scripts/diagnose_pc_latency.py                  (Linux)

Mede o que a app sente pelo túnel SSH:
- abrir o socket TCP até ao túnel (127.0.0.1:15432);
- abrir uma ligação Postgres completa (o que a app fazia em cada consulta);
- uma consulta numa ligação já aberta (o que a app faz agora);
- a pergunta «qual é o plano atual?» e, com --plano, a descarga do plano inteiro.

Lê as variáveis MES_PG_* do .env da app se não estiverem no ambiente.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def ms(seconds: float) -> float:
    return round(seconds * 1000.0, 1)


def summary(values: list[float]) -> dict:
    return {"mediana_ms": ms(statistics.median(values)), "min_ms": ms(min(values)),
            "max_ms": ms(max(values)), "amostras": len(values)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-n", type=int, default=10, help="repetições de cada medição")
    parser.add_argument("--plano", action="store_true",
                        help="descarregar também o plano inteiro (vários MB)")
    args = parser.parse_args()

    load_env_file(ROOT / ".env")
    import psycopg

    from app import pg
    from app.config import Settings
    from app.matching import loaders

    settings = Settings()
    host, port = settings.pg_host, settings.pg_port
    report: dict = {"destino": f"{host}:{port}", "base": settings.pg_db}

    tcp = []
    for _ in range(args.n):
        started = time.perf_counter()
        with socket.create_connection((host, port), timeout=10):
            pass
        tcp.append(time.perf_counter() - started)
    report["tcp_ate_ao_tunel"] = summary(tcp)

    full = []
    for _ in range(args.n):
        started = time.perf_counter()
        with psycopg.connect(pg.dsn(), connect_timeout=10) as conn:
            conn.execute("SELECT 1").fetchone()
        full.append(time.perf_counter() - started)
    report["ligacao_nova_mais_select_como_antes"] = summary(full)

    pg.fetch("SELECT 1")  # abre a ligação reaproveitada fora da medição
    reused = []
    for _ in range(args.n):
        started = time.perf_counter()
        pg.fetch("SELECT 1")
        reused.append(time.perf_counter() - started)
    report["select_em_ligacao_reaproveitada_agora"] = summary(reused)

    started = time.perf_counter()
    info = loaders.plan_snapshot_info()
    report["pergunta_plano_atual_ms"] = ms(time.perf_counter() - started)
    report["plano_atual"] = {"snapshot": info.get("snapshot_id"),
                             "idade_horas": round(info.get("age_hours") or 0, 1)}

    if args.plano:
        started = time.perf_counter()
        index = loaders.load_cantoneiras_index(snapshot_id=info.get("snapshot_id"))
        report["descarga_do_plano"] = {"segundos": round(time.perf_counter() - started, 2),
                                       "linhas": len(index.entries)}

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
