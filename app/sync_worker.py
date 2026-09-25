"""Validada logo, grava depois: grava no Postgres as folhas validadas.

Decisão de 25/09: o clique em Validar não espera pelo Postgres (pelo túnel da
fábrica eram vários segundos). A validação fica no SQLite com
``sync_state='pending'`` e este trabalhador, numa thread do processo, grava-a
por trás e volta a tentar sozinho quando o túnel falha.

Regras:
- Nunca refaz o cruzamento: ligaria a folha a um plano mais novo do que o que
  o operador viu. Só completa o saldo histórico que falhou por falta de
  ligação (``transient``), e grava-o no SQLite ANTES do Postgres.
- A gravação é idempotente pelo UID (pg_store): se o processo cair entre o
  commit no Postgres e a confirmação local, a tentativa seguinte confirma.
- Falha de rede → nova tentativa (10 s, 30 s, 2 min, 10 min, depois de 15 em
  15 min). Falha que não se resolve a tentar (data impossível, número já
  usado localmente…) → estado ``error``, visível, com «Tentar de novo».
- Uma folha de cada vez, com um lock partilhado com «Reabrir».
"""

from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg

from . import db, historical_quantities, pg, pg_store, validation_warnings
from .templates_spec import get_template

RETRY_DELAYS_S = (10, 30, 120, 600, 900)
_IDLE_WAIT_S = 60.0

_lock = threading.Lock()
_wake = threading.Event()
_stop = threading.Event()
_thread: threading.Thread | None = None
_thread_lock = threading.Lock()


class TransientGap(RuntimeError):
    """O saldo histórico de uma linha falhou por falta de ligação."""


# Falhas que passam sozinhas: vale a pena tentar de novo mais tarde.
_RETRYABLE = (psycopg.OperationalError, psycopg.InterfaceError, OSError,
              TimeoutError, pg_store.SheetNumberConflict, TransientGap)
# Falhas que tentar outra vez não resolve: ficam à vista com «Tentar de novo».
_FINAL = (pg_store.InvalidSheetDate, pg_store.SheetIdentityConflict,
          pg_store.SheetNumberingConfigurationError, db.LocalSheetNumberConflict)


@dataclass(frozen=True)
class SyncOutcome:
    state: str                 # done | retry | error | skipped
    sheet_no: int | None = None
    row_count: int | None = None
    previous_no: int | None = None
    error: str | None = None


def _message(exc: BaseException) -> str:
    if isinstance(exc, pg_store.InvalidSheetDate):
        return f"Data «{exc}» não é interpretável — reabre a folha e corrige-a."
    if isinstance(exc, pg_store.SheetNumberingConfigurationError):
        return f"Numeração do histórico indisponível: {exc}."
    if isinstance(exc, pg_store.SheetIdentityConflict):
        return f"A identidade da folha exige reconciliação: {exc}."
    if isinstance(exc, db.LocalSheetNumberConflict):
        return f"Gravada no histórico, mas a numeração local exige reconciliação: {exc}."
    if isinstance(exc, TransientGap):
        return str(exc)
    if isinstance(exc, _RETRYABLE):
        return "Sem ligação ao histórico (Postgres); nova tentativa automática."
    return f"Erro inesperado ao gravar: {type(exc).__name__}: {str(exc)[:200]}"


def _complete_balance(conn, sheet: dict) -> None:
    """Completa o saldo que falhou por falta de ligação, e grava-o localmente
    antes do Postgres (o que vai para o histórico é o que fica no SQLite)."""
    cross = sheet.get("cross_check") or {}
    if not historical_quantities.has_transient_gap(cross):
        return
    historical_quantities.apply(
        sheet, sheet.get("sheet_data") or {}, cross,
        decisions=db.evidence_edits(conn, sheet), complete_validated=True)
    if historical_quantities.has_transient_gap(cross):
        raise TransientGap("Saldo histórico por calcular (sem ligação ao plano); "
                           "nova tentativa automática.")
    cross["validation_warnings"] = validation_warnings.refresh_balance(
        cross.get("validation_warnings"), sheet)
    if not db.save_sync_cross(conn, sheet["uid"], cross):
        raise RuntimeError("a folha deixou de estar à espera de gravação")
    sheet["cross_check"] = cross


def _sync_locked(uid: str) -> SyncOutcome:
    conn = db.connect()
    try:
        sheet = db.get_sheet(conn, uid)
        if (not sheet or sheet["status"] != "validated"
                or sheet.get("sync_state") not in db.SYNC_WAITING):
            return SyncOutcome("skipped")
        try:
            _complete_balance(conn, sheet)
            template = get_template(sheet["template_name"])
            result = pg_store.store_validated_sheet(
                sheet, template, db.edit_count(conn, uid),
                sheet.get("validated_by") or "operador",
                minimum_sheet_no=db.minimum_sheet_number(conn),
                validated_at=sheet.get("validated_at"))
            previous = db.confirm_sync(conn, uid, definitive_sheet_no=result.sheet_no,
                                       next_sheet_no=result.next_sheet_no)
            return SyncOutcome("done", result.sheet_no, result.row_count, previous)
        except _FINAL as exc:
            message = _message(exc)
            print(f"[sync] folha {uid}: erro: {message}", flush=True)
            db.schedule_sync_retry(conn, uid, message, None, final=True)
            return SyncOutcome("error", error=message)
        except _RETRYABLE as exc:
            attempts = int(sheet.get("sync_attempts") or 0)
            delay = RETRY_DELAYS_S[min(attempts, len(RETRY_DELAYS_S) - 1)]
            next_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(
                timespec="seconds")
            message = _message(exc)
            print(f"[sync] folha {uid}: {type(exc).__name__}: {exc} — nova tentativa em {delay}s",
                  flush=True)
            db.schedule_sync_retry(conn, uid, message, next_at)
            return SyncOutcome("retry", error=message)
        except Exception as exc:
            # Provável defeito do código: fica à vista em vez de repetir em
            # silêncio para sempre.
            traceback.print_exc()
            message = _message(exc)
            db.schedule_sync_retry(conn, uid, message, None, final=True)
            return SyncOutcome("error", error=message)
    finally:
        conn.close()


def sync_one(uid: str) -> SyncOutcome:
    with _lock:
        return _sync_locked(uid)


def run_due() -> int:
    conn = db.connect()
    try:
        uids = db.due_syncs(conn)
    finally:
        conn.close()
    done = 0
    for uid in uids:
        done += sync_one(uid).state == "done"
    return done


def _seconds_until_next() -> float:
    conn = db.connect()
    try:
        next_at = db.next_sync_at(conn)
    finally:
        conn.close()
    if next_at is None:
        return _IDLE_WAIT_S
    if next_at == "":
        return 0.5
    wait = (datetime.fromisoformat(next_at) - datetime.now(timezone.utc)).total_seconds()
    return min(_IDLE_WAIT_S, max(0.5, wait))


def _loop() -> None:
    while not _stop.is_set():
        try:
            run_due()
            wait = _seconds_until_next()
        except Exception:
            traceback.print_exc()
            wait = RETRY_DELAYS_S[0]
        _wake.wait(wait)
        _wake.clear()


def start() -> None:
    """Arranca o trabalhador (uma vez por processo); retoma o que ficou por
    gravar num arranque anterior."""
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="sync-worker", daemon=True)
        _thread.start()


def stop(timeout: float = 5.0) -> None:
    global _thread
    _stop.set()
    _wake.set()
    with _thread_lock:
        thread, _thread = _thread, None
    if thread is not None:
        thread.join(timeout)


def wake() -> None:
    _wake.set()


def _stored_in_postgres(uid: str) -> bool:
    return bool(pg.fetch(
        "SELECT 1 FROM mes_kanban.validated_sheets WHERE sheet_uid = %s", (uid,)))


def reopen(uid: str, actor: str) -> str | None:
    """Volta a pôr em revisão uma validada que ainda não chegou ao histórico.

    Devolve None se reabriu, ou a razão pela qual recusou. Recusa quando o
    Postgres já a tem ou quando não se consegue confirmar que não tem: o
    histórico só aceita INSERT, e uma correção local que nunca lá chegasse
    perdia-se em silêncio.
    """
    if not _lock.acquire(timeout=30):
        return "A folha está a ser gravada neste momento; tenta daqui a pouco."
    try:
        conn = db.connect()
        try:
            sheet = db.get_sheet(conn, uid)
            if (not sheet or sheet["status"] != "validated"
                    or sheet.get("sync_state") not in db.SYNC_WAITING):
                return "Esta folha já está gravada no histórico e não pode ser reaberta."
            try:
                stored = _stored_in_postgres(uid)
            except Exception:
                return ("Sem ligação ao histórico: não é possível confirmar que a folha "
                        "ainda não foi gravada. Tenta de novo quando houver ligação.")
            if stored:
                wake()
                return "Esta folha já chegou ao histórico e não pode ser reaberta."
            if not db.reopen_unsynced(conn, uid, actor):
                return "A folha mudou de estado; recarrega a página."
            return None
        finally:
            conn.close()
    finally:
        _lock.release()
