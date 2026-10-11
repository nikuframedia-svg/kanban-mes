"""Header-only requests use revision guards and never enqueue full-sheet OCR."""
from fastapi import Form, HTTPException
from fastapi.responses import RedirectResponse
from .. import db, header_recovery, image_storage

import threading

automatic = None
_deps: dict = {}
# Várias folhas podem ser lidas ao mesmo tempo; a recuperação do cabeçalho
# tem um bloqueio que não espera (recovery_lock), por isso faz fila aqui.
_ingest_lock = threading.Lock()


def job_status(uid):
    return automatic.jobs.get(uid, {}) if automatic else {}


def header_eligible(conn, sheet):
    if image_storage.for_processing(sheet) is None:
        return False
    if header_recovery.current_recovery(sheet):
        return False  # One automatic attempt per image/generation; failures offer retry.
    protected = db.human_header_fields(conn, sheet['uid'])
    header = (sheet.get('sheet_data') or {}).get('header') or {}
    return any(not str(header.get(field) or '').strip() and field not in protected
               for field in ('operador', 'n_operador', 'setor_maquina', 'data'))


def recover_at_ingest(conn, uid):
    """Recuperação do cabeçalho na leitura, em segundo plano — uma vez.

    Corria quando o operador abria a folha (com o cabeçalho incompleto, ex.:
    «N.º» em branco), numa fila única e com ele à espera do OCR. Feita aqui,
    a folha abre já com a tentativa registada e a revisão automática não a
    repete. Nunca falha a leitura: o resultado fica para a revisão."""
    with _ingest_lock:
        sheet = db.get_sheet(conn, uid)
        if not _deps or not sheet or not header_eligible(conn, sheet):
            return None
        return header_recovery.recover(
            conn, uid, sheet['revision'], _deps['provider'](),
            _safe(_deps['employees'], {}), _safe(_deps['machines'], []),
            _deps['assumed_date'](sheet))


def register(app, connect, provider, employees, machines, assumed_date, location, recheck):
    from .automatic_review import AutomaticReview
    from .. import coverage_recovery, historical_quantities
    global automatic
    _deps.update(provider=provider, employees=employees, machines=machines,
                 assumed_date=assumed_date)

    def process(conn, uid, revision):
        from .automatic_review import run_stages
        return run_stages(conn, uid, [
            ('header', lambda sheet: header_recovery.recover(conn, uid, sheet['revision'], provider(),
                _safe(employees, {}), _safe(machines, []), assumed_date(sheet))
                if header_eligible(conn, sheet) or header_recovery.current_recovery(sheet).get('status') == 'failed' else None),
            ('coverage', lambda sheet: coverage_recovery.automatic(conn, uid, sheet['revision'], provider)
                if coverage_recovery.needs_automatic(conn, sheet) else None),
            ('cross_history', lambda sheet: recheck(conn, uid)),
        ])

    def eligible(conn, sheet):
        from ..full_profile_identity import corrections
        return (header_eligible(conn, sheet) or coverage_recovery.needs_automatic(conn, sheet)
                or historical_quantities.needs_refresh(sheet)
                or bool(corrections(sheet, db.evidence_edits(conn, sheet))))

    automatic = AutomaticReview(app, connect, eligible, process)

    @app.post('/sheet/{uid}/header-recovery')
    def recover(uid: str, revision: int = Form(...), back: str = Form('')):
        automatic.enqueue(uid, revision, force=True)
        return RedirectResponse(location(uid, back), status_code=303)

    @app.post('/sheet/{uid}/header-recovery/confirm-date')
    def confirm_date(uid: str, revision: int = Form(...), back: str = Form('')):
        conn = connect()
        try:
            try:
                header_recovery.confirm_rule(conn, uid, revision)
            except ValueError as exc:
                return RedirectResponse(location(uid, back, erro=str(exc), erro_context='edit'), status_code=303)
        finally:
            conn.close()
        return RedirectResponse(location(uid, back), status_code=303)


def _safe(loader, default):
    try:
        return loader()
    except Exception:
        return default
