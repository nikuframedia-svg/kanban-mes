"""Header-only requests use revision guards and never enqueue full-sheet OCR."""
from fastapi import Form, HTTPException
from fastapi.responses import RedirectResponse
from .. import db, header_recovery, image_storage

automatic = None


def job_status(uid):
    return automatic.jobs.get(uid, {}) if automatic else {}


def register(app, connect, provider, employees, machines, assumed_date, location):
    from .automatic_review import AutomaticReview
    global automatic

    def eligible(conn, sheet):
        if image_storage.for_processing(sheet) is None:
            return False
        if header_recovery.current_recovery(sheet):
            return False  # One automatic attempt per image/generation; failures offer retry.
        protected = db.human_header_fields(conn, sheet['uid'])
        header = (sheet.get('sheet_data') or {}).get('header') or {}
        return any(not str(header.get(field) or '').strip() and field not in protected
                   for field in ('operador', 'n_operador', 'setor_maquina', 'data'))

    def process(conn, uid, revision):
        sheet = db.get_sheet(conn, uid)
        return header_recovery.recover(conn, uid, revision, provider(),
            _safe(employees, {}), _safe(machines, []), assumed_date(sheet))

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
