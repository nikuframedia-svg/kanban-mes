"""Mede o custo do túnel da fábrica nas leituras e na gravação da validação.

Sobe um PostgreSQL descartável (Docker) e põe-lhe à frente um intermediário
TCP que atrasa cada envio metade da ida-e-volta em cada sentido (70 ms por
omissão, o valor medido entre o servidor e a fábrica). Compara:

- leituras com uma ligação nova por consulta (código de origin/main) contra a
  ligação reaproveitada de app/pg.py;
- a gravação de uma folha validada com o pg_store de origin/main contra o
  pg_store atual.

Nada toca na base de produção.

Uso: uv run python scripts/benchmark_tunnel.py [--rtt-ms 70] [--rows 15]
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import importlib.util
import json
import os
import socket
import statistics
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402

from app import historical_quantities, pg, pg_store  # noqa: E402
from app.templates_spec import get_template  # noqa: E402


# ---------- túnel simulado ----------

class LatencyProxy:
    """Reencaminha TCP atrasando cada bloco ``one_way`` segundos por sentido.

    Cada bloco sai ``one_way`` depois de chegar (não depois do anterior), para
    que vários envios em curso se sobreponham como numa rede real.
    """

    def __init__(self, upstream_port: int, one_way: float):
        self.upstream_port = upstream_port
        self.one_way = one_way
        self.loop = asyncio.new_event_loop()
        self.port: int | None = None
        ready = threading.Event()
        threading.Thread(target=self._run, args=(ready,), daemon=True).start()
        ready.wait(10)

    def _run(self, ready: threading.Event) -> None:
        asyncio.set_event_loop(self.loop)

        async def start():
            server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
            self.port = server.sockets[0].getsockname()[1]
            ready.set()

        self.loop.run_until_complete(start())
        self.loop.run_forever()

    async def _pipe(self, reader, writer):
        queue: asyncio.Queue = asyncio.Queue()

        async def deliver():
            while True:
                due, data = await queue.get()
                delay = due - self.loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                if data is None:
                    writer.close()
                    return
                writer.write(data)
                await writer.drain()

        sender = asyncio.ensure_future(deliver())
        try:
            while data := await reader.read(65536):
                queue.put_nowait((self.loop.time() + self.one_way, data))
        finally:
            queue.put_nowait((self.loop.time() + self.one_way, None))
            await sender

    async def _handle(self, client_reader, client_writer):
        up_reader, up_writer = await asyncio.open_connection("127.0.0.1", self.upstream_port)
        await asyncio.gather(self._pipe(client_reader, up_writer),
                             self._pipe(up_reader, client_writer),
                             return_exceptions=True)


# ---------- PostgreSQL descartável ----------

def _run(*args, input_text=None):
    return subprocess.run(args, input=input_text, text=True, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def start_postgres() -> tuple[str, int, str]:
    name = f"kanban-benchmark-{uuid.uuid4().hex[:10]}"
    password = uuid.uuid4().hex
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    _run("docker", "run", "-d", "--rm", "--name", name,
         "-e", f"POSTGRES_PASSWORD={password}", "-e", "POSTGRES_DB=dataresearchmtg",
         "-p", f"127.0.0.1:{port}:5432", "postgres:16-alpine")
    admin = f"host=127.0.0.1 port={port} dbname=dataresearchmtg user=postgres password={password}"
    deadline = time.monotonic() + 30
    while True:
        try:
            with psycopg.connect(admin, connect_timeout=1):
                break
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute("CREATE SCHEMA core_mtg; CREATE SCHEMA analytics_mtg; "
                     "CREATE SCHEMA raw_mtg; CREATE SCHEMA audit_mtg")
    for number in range(10, 18):
        path = next((ROOT / "sql").glob(f"{number:03d}_*.sql"))
        _run("docker", "exec", "-i", name, "psql", "-v", "ON_ERROR_STOP=1",
             "-U", "postgres", "-d", "dataresearchmtg", input_text=path.read_text())
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f"ALTER ROLE mes_kanban_app PASSWORD '{password}'")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS validated_sheets_source_app_sheet_no_uidx "
            "ON mes_kanban.validated_sheets(source_app, sheet_no) "
            "WHERE source_app IS NOT NULL AND sheet_no IS NOT NULL")
    return name, port, password


# ---------- código de origin/main ----------

def load_origin_pg_store(ref: str):
    source = subprocess.run(["git", "-C", str(ROOT), "show", f"{ref}:app/pg_store.py"],
                            check=True, text=True, stdout=subprocess.PIPE).stdout
    spec = importlib.util.spec_from_loader("app._pg_store_origin", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "app"
    sys.modules[spec.name] = module  # as dataclasses procuram o módulo aqui
    exec(compile(source, f"{ref}:app/pg_store.py", "exec"), module.__dict__)
    return module


def read_with_new_connection_each_time(sql: str) -> None:
    """O _fetch de origin/main: ligação nova, só-leitura, BEGIN/COMMIT."""
    with psycopg.connect(os.environ["MES_PG_DSN"]) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute(sql)
            cur.fetchall()


# ---------- folha de exemplo ----------

def sample_sheet(rows: int, sheet_no: int) -> tuple[dict, object]:
    template = get_template("cantoneiras_kanban")
    data_rows, cross_rows = [], []
    for i in range(rows):
        full = i % 5 == 4  # uma em cada cinco é Perf. Comp. (com referências)
        data_rows.append({"of": "264534", "perfil": "L55X55X5", "qtd": str(2 + i),
                          "modelo": "" if full else f"EA8B{i:02d}",
                          **({"perf_comp": "X"} if full else {})})
        cross_rows.append({"row_index": i, "matched_plan_key": f"key-{i}", "cells": []})
    sheet = {"uid": uuid.uuid4().hex[:12], "sheet_no": sheet_no, "template_name": template.name,
             "image_sha256": "f" * 64, "raw_extraction": {}, "status": "in_review",
             "sheet_data": {"header": {"data": "16/09/2026", "operador": "BENCH"},
                            "rows": data_rows, "footer": {}},
             "cross_check": {"snapshot_id": "bench", "rows": cross_rows}}
    from tests.test_historical_quantities import entries
    historical_quantities.apply(sheet, sheet["sheet_data"], sheet["cross_check"],
                                snapshot_loader=lambda day: {"snapshot_id": "bench-past"},
                                order_loader=lambda *a: entries())
    return sheet, template


def timed(fn, repeat: int) -> list[float]:
    out = []
    for _ in range(repeat):
        started = time.perf_counter()
        fn()
        out.append((time.perf_counter() - started) * 1000.0)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rtt-ms", type=float, default=70.0)
    parser.add_argument("--rows", type=int, default=15)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--origin", default="origin/main")
    args = parser.parse_args()

    name, port, password = start_postgres()
    try:
        proxy = LatencyProxy(port, args.rtt_ms / 2000.0)
        os.environ["MES_PG_DSN"] = (f"host=127.0.0.1 port={proxy.port} dbname=dataresearchmtg "
                                    f"user=mes_kanban_app password={password}")
        pg.reset()
        origin_store = load_origin_pg_store(args.origin)

        reads_old = timed(lambda: read_with_new_connection_each_time("SELECT 1"), args.repeat * 2)
        pg.fetch("SELECT 1")  # primeira ligação, fora da medição (acontece uma vez)
        reads_new = timed(lambda: pg.fetch("SELECT 1"), args.repeat * 2)

        counter = iter(range(1, 10_000))

        def store_with(module):
            sheet, template = sample_sheet(args.rows, next(counter))
            rows = module.store_validated_sheet(copy.deepcopy(sheet), template, 0, "bench")
            assert rows == args.rows, rows

        writes_old = timed(lambda: store_with(origin_store), args.repeat)
        writes_new = timed(lambda: store_with(pg_store), args.repeat)

        summary = {
            "rtt_ms": args.rtt_ms,
            "rows_per_sheet": args.rows,
            "read_ms_median": {"origin": round(statistics.median(reads_old), 1),
                               "now": round(statistics.median(reads_new), 1)},
            "validation_write_ms_median": {"origin": round(statistics.median(writes_old), 1),
                                           "now": round(statistics.median(writes_new), 1)},
        }
        print(json.dumps(summary, indent=2))
        return 0
    finally:
        subprocess.run(["docker", "stop", "-t", "2", name], check=False,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)


if __name__ == "__main__":
    raise SystemExit(main())
