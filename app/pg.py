"""Ligações ao Postgres, reaproveitadas entre pedidos.

Na fábrica a app fala com o Postgres por um túnel SSH (~70 ms por
ida-e-volta). Abrir uma ligação custa 7–9 idas-e-voltas (canal SSH, SSL,
autenticação SCRAM, BEGIN/COMMIT): com uma ligação nova por consulta, cada
SELECT pagava ~0,5 s antes de começar.

Leituras: ligações em autocommit e só de leitura, guardadas depois de usadas e
reaproveitadas — uma leitura passa a custar uma ida. Não há threads de fundo:
com o túnel em baixo a falha chega logo ao pedido, como antes.

Escritas: ligação nova por gravação (são raras), numa transação com
``lock_timeout`` e ``idle_in_transaction_session_timeout``. Se o túnel cair a
meio, o servidor larga o bloqueio da numeração em vez de travar as validações
seguintes. Os limites vão nas opções de arranque da ligação: não custam idas.
"""

from __future__ import annotations

import contextvars
import os
import threading
import time
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg import errors as pg_errors
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

from .config import settings

APPLICATION_NAME = "kanban-mes"
# A carga completa do índice do plano tem de caber aqui com folga.
READ_STATEMENT_TIMEOUT_MS = 60_000
WRITE_STATEMENT_TIMEOUT_MS = 60_000
WRITE_LOCK_TIMEOUT_MS = 10_000
WRITE_IDLE_IN_TRANSACTION_TIMEOUT_MS = 30_000
MAX_IDLE_CONNECTIONS = 4
# Uma ligação parada há mais tempo do que isto já não se reaproveita: o túnel
# ou o servidor podem tê-la fechado sem aviso.
MAX_IDLE_SECONDS = 300.0

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_READ_OPTIONS = (
    f"-c default_transaction_read_only=on "
    f"-c statement_timeout={READ_STATEMENT_TIMEOUT_MS}"
)
_WRITE_OPTIONS = (
    f"-c lock_timeout={WRITE_LOCK_TIMEOUT_MS} "
    f"-c idle_in_transaction_session_timeout={WRITE_IDLE_IN_TRANSACTION_TIMEOUT_MS} "
    f"-c statement_timeout={WRITE_STATEMENT_TIMEOUT_MS}"
)


def dsn() -> str:
    return os.environ.get("MES_PG_DSN") or settings.pg_dsn


def conninfo(base: str, options: str) -> str:
    """Completa o DSN configurado sem sobrepor o que lá estiver explícito."""
    params = conninfo_to_dict(base)
    hosts = {h.strip() for h in str(params.get("host") or "").split(",") if h.strip()}
    if hosts and hosts <= _LOOPBACK_HOSTS:
        # 127.0.0.1 é o túnel SSH (já cifrado) ou o próprio servidor:
        # negociar SSL/GSS só acrescentava idas-e-voltas a cada ligação.
        params.setdefault("sslmode", "disable")
        params.setdefault("gssencmode", "disable")
    params.setdefault("connect_timeout", "5")
    params.setdefault("application_name", APPLICATION_NAME)
    params["options"] = " ".join(p for p in (params.get("options"), options) if p)
    return make_conninfo("", **params)


# ---------- medição por pedido ----------

_request_stats: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "pg_request_stats", default=None)
_stats_lock = threading.Lock()


@contextmanager
def measure() -> Iterator[dict]:
    """Conta consultas, ligações abertas e tempo de Postgres de um pedido."""
    stats = {"queries": 0, "connects": 0, "ms": 0.0}
    token = _request_stats.set(stats)
    try:
        yield stats
    finally:
        _request_stats.reset(token)


def note(*, queries: int = 0, connects: int = 0, ms: float = 0.0) -> None:
    stats = _request_stats.get()
    if stats is None:
        return
    with _stats_lock:
        stats["queries"] += queries
        stats["connects"] += connects
        stats["ms"] += ms


def _close_quietly(conn: psycopg.Connection) -> None:
    try:
        conn.close()
    except Exception:
        pass


# ---------- leituras ----------

class _ReadConnections:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._idle: list[tuple[float, str, psycopg.Connection]] = []

    def _open(self, base: str) -> psycopg.Connection:
        started = time.perf_counter()
        conn = psycopg.connect(conninfo(base, _READ_OPTIONS),
                               autocommit=True, row_factory=dict_row)
        note(connects=1, ms=(time.perf_counter() - started) * 1000.0)
        return conn

    def _take(self, base: str) -> psycopg.Connection | None:
        now = time.monotonic()
        found, stale = None, []
        with self._lock:
            while self._idle:
                returned_at, owner, candidate = self._idle.pop()
                # O DSN muda nos testes (Postgres descartável); nunca se
                # entrega uma ligação aberta para outra base.
                if (owner != base or candidate.closed
                        or now - returned_at > MAX_IDLE_SECONDS):
                    stale.append(candidate)
                    continue
                found = candidate
                break
        for conn in stale:
            _close_quietly(conn)
        return found

    def _give_back(self, base: str, conn: psycopg.Connection) -> None:
        if (conn.closed or conn.broken
                or conn.info.transaction_status != TransactionStatus.IDLE):
            _close_quietly(conn)
            return
        with self._lock:
            if len(self._idle) < MAX_IDLE_CONNECTIONS:
                self._idle.append((time.monotonic(), base, conn))
                return
        _close_quietly(conn)

    def discard_idle(self) -> None:
        with self._lock:
            idle, self._idle = self._idle, []
        for _, _, conn in idle:
            _close_quietly(conn)

    @contextmanager
    def connection(self) -> Iterator[tuple[psycopg.Connection, bool]]:
        base = dsn()
        conn = self._take(base)
        reused = conn is not None
        if conn is None:
            conn = self._open(base)
        try:
            yield conn, reused
        except BaseException:
            # Depois de um erro o estado da ligação já não é de confiança.
            _close_quietly(conn)
            raise
        else:
            self._give_back(base, conn)


_reads = _ReadConnections()


def fetch(sql: str, params: tuple | list | None = None) -> list[dict]:
    """SELECT numa ligação reaproveitada; devolve linhas como dicts."""
    for attempt in range(2):
        with _reads.connection() as (conn, reused):
            try:
                started = time.perf_counter()
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    rows = cur.fetchall() if cur.description else []
                note(queries=1, ms=(time.perf_counter() - started) * 1000.0)
                return rows
            except psycopg.OperationalError as exc:
                # Uma ligação guardada que morreu entretanto (túnel que caiu
                # entre pedidos) repete-se uma vez numa ligação nova. Um
                # timeout ou uma falha numa ligação nova sobem ao chamador.
                if attempt or not reused or isinstance(exc, pg_errors.QueryCanceled):
                    raise
                # As outras guardadas morreram com a mesma queda do túnel.
                _reads.discard_idle()
    raise AssertionError("inalcançável: a 2.ª tentativa devolve ou lança")


@contextmanager
def read_connection() -> Iterator[psycopg.Connection]:
    """Ligação de leitura para quem precisa de várias consultas seguidas."""
    with _reads.connection() as (conn, _reused):
        yield conn


def reset() -> None:
    """Fecha as ligações guardadas (testes e mudança de configuração)."""
    _reads.discard_idle()


# ---------- escritas ----------

@contextmanager
def write_connection() -> Iterator[psycopg.Connection]:
    """Ligação nova numa transação: commit no fim, rollback se houver erro."""
    started = time.perf_counter()
    conn = psycopg.connect(conninfo(dsn(), _WRITE_OPTIONS))
    note(connects=1)
    try:
        with conn:
            yield conn
    finally:
        # conta a gravação inteira (ligação + transação) como uma operação
        note(queries=1, ms=(time.perf_counter() - started) * 1000.0)
