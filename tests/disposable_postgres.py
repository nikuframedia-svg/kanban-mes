"""Integração da numeração com um PostgreSQL 16 descartável.

Executar explicitamente:
    RUN_PG_INTEGRATION=1 .venv/bin/python -m pytest -q \
        -m pg_integration tests/test_pg_sheet_numbering_integration.py
"""

from __future__ import annotations

import concurrent.futures
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import psycopg
import pytest

from app import pg_store
from app.templates_spec import get_template


pytestmark = [
    pytest.mark.skipif(
        os.environ.get("RUN_PG_INTEGRATION") != "1",
        reason="define RUN_PG_INTEGRATION=1 para criar o PostgreSQL descartável",
    ),
]


ROOT = Path(__file__).resolve().parents[1]


def _run(*args, input_text=None):
    return subprocess.run(
        args,
        input=input_text,
        text=True,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


@pytest.fixture(scope="module")
def postgres16():
    name = f"kanban-numbering-{uuid.uuid4().hex[:10]}"
    password = uuid.uuid4().hex
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    _run(
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "-e",
        f"POSTGRES_PASSWORD={password}",
        "-e",
        "POSTGRES_DB=dataresearchmtg",
        "-p",
        f"127.0.0.1:{port}:5432",
        "postgres:16-alpine",
    )
    admin_dsn = (
        f"host=127.0.0.1 port={port} dbname=dataresearchmtg "
        f"user=postgres password={password}"
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                with psycopg.connect(admin_dsn, connect_timeout=1):
                    break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("PostgreSQL de teste não arrancou")
                time.sleep(0.1)
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(
                "CREATE SCHEMA core_mtg; CREATE SCHEMA analytics_mtg; "
                "CREATE SCHEMA raw_mtg; CREATE SCHEMA audit_mtg"
            )
        for number in range(10, 19):
            path = next((ROOT / "sql").glob(f"{number:03d}_*.sql"))
            _run(
                "docker",
                "exec",
                "-i",
                name,
                "psql",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "postgres",
                "-d",
                "dataresearchmtg",
                input_text=path.read_text(),
            )
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(f"ALTER ROLE mes_kanban_app PASSWORD '{password}'")
        app_dsn = (
            f"host=127.0.0.1 port={port} dbname=dataresearchmtg "
            f"user=mes_kanban_app password={password}"
        )
        yield admin_dsn, app_dsn
    finally:
        subprocess.run(
            ["docker", "stop", "-t", "2", name],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )


@pytest.fixture()
def clean_history(postgres16, monkeypatch):
    admin_dsn, app_dsn = postgres16
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute("TRUNCATE mes_kanban.validated_sheets CASCADE")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "validated_sheets_source_app_sheet_no_uidx "
            "ON mes_kanban.validated_sheets(source_app, sheet_no) "
            "WHERE source_app IS NOT NULL AND sheet_no IS NOT NULL"
        )
    monkeypatch.setenv("MES_PG_DSN", app_dsn)
    return admin_dsn

