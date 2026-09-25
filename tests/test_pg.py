"""Ligações reaproveitadas ao Postgres (app/pg.py) — sem Postgres real."""

from __future__ import annotations

from types import SimpleNamespace

import psycopg
import pytest
from psycopg import errors as pg_errors
from psycopg.conninfo import conninfo_to_dict
from psycopg.pq import TransactionStatus

from app import pg


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        if self.conn.fail_with is not None:
            error, self.conn.fail_with = self.conn.fail_with, None
            self.conn.broken = True
            raise error
        self.description = [("x",)]

    def fetchall(self):
        return [{"x": 1}]


class FakeConnection:
    def __init__(self, conninfo):
        self.conninfo = conninfo
        self.executed = []
        self.closed = False
        self.broken = False
        self.fail_with = None
        self.info = SimpleNamespace(transaction_status=TransactionStatus.IDLE)

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


@pytest.fixture()
def fake_connect(monkeypatch):
    monkeypatch.setenv("MES_PG_DSN", "host=127.0.0.1 port=15432 dbname=d user=u password=p")
    opened: list[FakeConnection] = []

    def connect(conninfo, **kwargs):
        conn = FakeConnection(conninfo)
        conn.kwargs = kwargs
        opened.append(conn)
        return conn

    monkeypatch.setattr(psycopg, "connect", connect)
    return opened


def test_leituras_reaproveitam_a_mesma_ligacao(fake_connect):
    assert pg.fetch("SELECT 1") == [{"x": 1}]
    assert pg.fetch("SELECT 2") == [{"x": 1}]
    assert len(fake_connect) == 1, "a segunda leitura não pode abrir outra ligação"
    assert fake_connect[0].kwargs["autocommit"] is True


def test_ligacao_de_leitura_e_so_de_leitura_e_sem_ssl_no_tunel(fake_connect):
    pg.fetch("SELECT 1")
    params = conninfo_to_dict(fake_connect[0].conninfo)
    assert "default_transaction_read_only=on" in params["options"]
    assert "statement_timeout=" in params["options"]
    # 127.0.0.1 é o túnel SSH: negociar SSL só acrescentava idas-e-voltas
    assert params["sslmode"] == "disable"
    assert params["gssencmode"] == "disable"
    assert params["connect_timeout"] == "5"
    assert params["application_name"] == pg.APPLICATION_NAME


def test_dsn_explicito_nao_e_sobreposto():
    params = conninfo_to_dict(pg.conninfo(
        "host=db.example.com dbname=d sslmode=require connect_timeout=9 "
        "options='-c search_path=x'", "-c statement_timeout=1"))
    assert params["sslmode"] == "require"
    assert params["connect_timeout"] == "9"
    assert params["options"] == "-c search_path=x -c statement_timeout=1"
    remote = conninfo_to_dict(pg.conninfo("host=db.example.com dbname=d", ""))
    assert "sslmode" not in remote, "fora do túnel mantém-se a negociação normal"


def test_ligacao_guardada_que_morreu_repete_uma_vez_em_ligacao_nova(fake_connect):
    pg.fetch("SELECT 1")
    fake_connect[0].fail_with = psycopg.OperationalError("server closed the connection")
    assert pg.fetch("SELECT 2") == [{"x": 1}]
    assert len(fake_connect) == 2
    assert fake_connect[0].closed


def test_timeout_nao_se_repete(fake_connect):
    pg.fetch("SELECT 1")
    fake_connect[0].fail_with = pg_errors.QueryCanceled("canceling statement due to statement timeout")
    with pytest.raises(pg_errors.QueryCanceled):
        pg.fetch("SELECT pg_sleep(99)")
    assert len(fake_connect) == 1


def test_falha_em_ligacao_nova_sobe_logo(monkeypatch):
    monkeypatch.setenv("MES_PG_DSN", "host=127.0.0.1 dbname=d")
    calls = []

    def refused(conninfo, **kwargs):
        calls.append(conninfo)
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(psycopg, "connect", refused)
    with pytest.raises(psycopg.OperationalError):
        pg.fetch("SELECT 1")
    assert len(calls) == 1, "túnel em baixo: falhar logo, sem segunda tentativa"


def test_mudanca_de_dsn_nunca_reaproveita_ligacao_de_outra_base(fake_connect, monkeypatch):
    pg.fetch("SELECT 1")
    monkeypatch.setenv("MES_PG_DSN", "host=127.0.0.1 port=15432 dbname=outra user=u")
    pg.fetch("SELECT 1")
    assert len(fake_connect) == 2
    assert "dbname=outra" in fake_connect[1].conninfo


def test_ligacao_com_transacao_aberta_nao_volta_ao_reservatorio(fake_connect):
    with pg.read_connection() as conn:
        conn.info = SimpleNamespace(transaction_status=TransactionStatus.INTRANS)
    pg.fetch("SELECT 1")
    assert len(fake_connect) == 2
    assert fake_connect[0].closed


def test_medicao_conta_consultas_e_ligacoes(fake_connect):
    with pg.measure() as stats:
        pg.fetch("SELECT 1")
        pg.fetch("SELECT 2")
    assert stats["queries"] == 2
    assert stats["connects"] == 1
    assert stats["ms"] >= 0
    pg.fetch("SELECT 3")  # fora de measure(): não rebenta nem conta


def test_escrita_usa_ligacao_propria_com_limites_de_bloqueio(monkeypatch):
    monkeypatch.setenv("MES_PG_DSN", "host=127.0.0.1 dbname=d")
    seen = {}

    class WriteConnection:
        def __enter__(self):
            seen["entered"] = True
            return self

        def __exit__(self, *args):
            seen["exited"] = True
            return False

    def connect(conninfo, **kwargs):
        seen["conninfo"] = conninfo
        seen["kwargs"] = kwargs
        return WriteConnection()

    monkeypatch.setattr(psycopg, "connect", connect)
    with pg.measure() as stats:
        with pg.write_connection() as conn:
            assert isinstance(conn, WriteConnection)
    options = conninfo_to_dict(seen["conninfo"])["options"]
    assert "lock_timeout=" in options
    assert "idle_in_transaction_session_timeout=" in options
    assert "default_transaction_read_only" not in options
    assert "autocommit" not in seen["kwargs"], "a gravação é uma transação"
    assert seen["entered"] and seen["exited"]
    assert stats["connects"] == 1 and stats["queries"] == 1
