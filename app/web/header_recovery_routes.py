"""Header-only requests use revision guards and never enqueue full-sheet OCR."""
import threading
from fastapi import Form, HTTPException
from fastapi.responses import RedirectResponse
from .. import db, header_recovery

# Serialize expensive reads in this process; the CLI also uses one worker.
_lock = threading.Lock()
_jobs = {}


def job_status(uid):
    return _jobs.get(uid, {})


def register(app, connect, provider, employees, machines, assumed_date, location):
    @app.post('/sheet/{uid}/header-recovery')
    def recover(uid: str, revision: int = Form(...), back: str = Form('')):
        if not _lock.acquire(blocking=False):
            return RedirectResponse(location(uid, back, erro='Já existe uma recuperação em curso. Tenta novamente quando terminar.', erro_context='edit'), status_code=303)
        conn = connect()
        try:
            sheet = db.get_sheet(conn, uid)
            if not sheet:
                raise HTTPException(404)
            if sheet['revision'] != revision or sheet['status'] not in {'extracted', 'in_review'}:
                raise HTTPException(409, 'A folha mudou ou não admite recuperação.')
            # HTTP request only starts a bounded background header job. The
            # snapshot revision is checked again after OCR; a restart leaves no
            # pending sheet status and the request can safely be repeated.
            _jobs[uid] = {"status": "running"}
            def work():
                worker = None
                try:
                    worker = connect()
                    result = header_recovery.recover(worker, uid, revision, provider(),
                        _safe(employees, {}), _safe(machines, []), assumed_date(sheet))
                    _jobs[uid] = result
                except Exception as exc:
                    _jobs[uid] = {'status': 'error', 'error': str(exc)}
                    print(f'[header-recovery] {uid}: {type(exc).__name__}: {exc}', flush=True)
                finally:
                    if worker is not None:
                        worker.close()
                    _lock.release()
            threading.Thread(target=work, daemon=True).start()
        except Exception:
            _lock.release()
            raise
        finally:
            conn.close()
        return RedirectResponse(location(uid, back, recovery_started=1), status_code=303)

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
