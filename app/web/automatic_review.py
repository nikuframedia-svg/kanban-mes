"""Serial review jobs with per-stage outcomes and their final committed revision."""
import logging
import queue
import threading
import uuid
from fastapi import Form, HTTPException, Request
from .. import db
from ..health import STARTUP_HEALTH
from ..review_guard import ReviewConflict, revision_guard, check, request_id, generation

log = logging.getLogger("uvicorn.error.automatic_review")
TERMINAL = {"complete", "error", "conflict"}


def run_stages(conn, uid, stages):
    results = {}
    for name, action in stages:
        sheet = db.get_sheet(conn, uid)
        try:
            check(uid, sheet["revision"] if sheet else None)
            if not sheet or sheet["status"] not in {"extracted", "in_review"}:
                raise ReviewConflict("A folha mudou de estado.")
        except ReviewConflict:
            results[name] = {"status": "conflict"}
            return {"stages": results, "conflict": True}
        before = sheet["revision"]
        try:
            result = action(sheet)
            if result is False:
                raise ReviewConflict("A folha mudou durante a verificação.")
            after = db.get_sheet(conn, uid)
            check(uid, after["revision"])
            status = "skipped" if result is None else "complete"
            if isinstance(result, dict) and result.get("status") in {"failed", "error"}:
                status = "error"
            details = {}
            if name == "coverage":
                coverage = (after.get("sheet_data") or {}).get("_ocr_coverage") or {}
                details = {"coverage_status": coverage.get("status"), "expected_rows": coverage.get("expected_rows")}
            elif name == "cross_history":
                details = {"historical_balances": [
                    {"row_index": row["row_index"], "status": row["quantity_basis"].get("status"),
                     "diagnostic": row["quantity_basis"].get("diagnostic")}
                    for row in (after.get("cross_check") or {}).get("rows", []) if row.get("quantity_basis")]}
            results[name] = {"status": status, "initial_revision": before,
                             "final_revision": after["revision"], "result": result, **details}
        except ReviewConflict:
            results[name] = {"status": "conflict", "initial_revision": before}
            return {"stages": results, "conflict": True}
        except Exception:
            # A stage may have committed useful work before failing. The DB
            # guard tracks exactly those commits, never a concurrent user's.
            after = db.get_sheet(conn, uid)
            try:
                check(uid, after["revision"] if after else None)
            except ReviewConflict:
                results[name] = {"status": "conflict", "initial_revision": before}
                return {"stages": results, "conflict": True}
            log.exception("[automatic-review] request=%s sheet=%s version=%s stage=%s revision=%s",
                          request_id(), uid, STARTUP_HEALTH.get("commit"), name, before)
            results[name] = {"status": "error", "initial_revision": before,
                             "final_revision": after["revision"], "error": "Esta etapa não terminou; os dados guardados foram preservados."}
    return {"stages": results}


class AutomaticReview:
    def __init__(self, app, connect, eligible, process):
        self.connect, self.eligible, self.process = connect, eligible, process
        self.jobs, self.queue, self.lock, self.worker = {}, queue.Queue(), threading.Lock(), None

        @app.post('/sheet/{uid}/automatic-review')
        def start(request: Request, uid: str, revision: int = Form(...), retry: bool = Form(False)):
            return self.enqueue(uid, revision, force=retry, request_id=getattr(request.state, "request_id", None))

        @app.get('/sheet/{uid}/automatic-review')
        def status(uid: str):
            conn = connect()
            try:
                sheet = db.get_sheet(conn, uid)
                if not sheet:
                    raise HTTPException(404)
                return {**self.jobs.get(uid, {"status": "idle"}), "current_revision": sheet["revision"], "sheet_status": sheet["status"]}
            finally:
                conn.close()

    def needed(self, conn, sheet):
        if not sheet or sheet['status'] not in {'extracted', 'in_review'}:
            return False
        previous = self.jobs.get(sheet['uid'], {})
        if previous.get('observed_revision', previous.get('final_revision', previous.get('revision'))) == sheet['revision'] and previous.get('status') in TERMINAL:
            return False
        return self.eligible(conn, sheet)

    def enqueue(self, uid, revision, force=False, request_id=None):
        conn = self.connect()
        try:
            sheet = db.get_sheet(conn, uid)
            if not sheet:
                raise HTTPException(404)
            with self.lock:
                previous = self.jobs.get(uid, {})
                if sheet['status'] not in {'extracted', 'in_review'}:
                    raise HTTPException(409, 'A folha mudou de estado; os dados foram preservados.')
                # Another tab can join an existing job after its first commit.
                if previous.get('status') in {'queued', 'running'}:
                    return dict(previous)
                if sheet['revision'] != revision:
                    raise HTTPException(409, 'A folha mudou; a verificação automática não substituiu dados.')
                if not force and not self.needed(conn, sheet):
                    return dict(previous) if previous else {'status': 'idle', 'revision': revision, 'final_revision': revision}
                job = {'status': 'queued', 'revision': revision, 'initial_revision': revision,
                       'request_id': request_id or uuid.uuid4().hex, 'job_id': uuid.uuid4().hex, 'stages': {}, 'generation': generation(uid)}
                self.jobs[uid] = job
                self.queue.put((uid, dict(job)))
                if self.worker is None or not self.worker.is_alive():
                    self.worker = threading.Thread(target=self._work, daemon=True)
                    self.worker.start()
                return dict(job)
        finally:
            conn.close()

    def _work(self):
        while True:
            uid, job = self.queue.get()
            job = {**job, 'status': 'running'}
            self.jobs[uid] = job
            conn = None
            with revision_guard(uid, job['revision'], job['request_id'],
                                automatic_generation=job['generation']) as guard:
                try:
                    check(uid, job['revision'])
                    conn = self.connect()
                    result = self.process(conn, uid, job['revision']) or {}
                    stages = result.get('stages', {})
                    status = ('conflict' if result.get('conflict') else
                              'error' if any(v['status'] == 'error' for v in stages.values()) else 'complete')
                    job = {**job, 'status': status, 'result': result, 'stages': stages}
                except ReviewConflict:
                    job = {**job, 'status': 'conflict'}
                except Exception:
                    job = {**job, 'status': 'error'}
                    log.exception('[automatic-review] request=%s sheet=%s version=%s stage=job',
                                  job['request_id'], uid, STARTUP_HEALTH.get('commit'))
                finally:
                    job['final_revision'] = guard['revision']
                    if conn is not None:
                        try:
                            current = db.get_sheet(conn, uid)
                            job['observed_revision'] = current['revision'] if current else guard['revision']
                            if current and current['revision'] != guard['revision']:
                                job['status'] = 'conflict'
                        except Exception:
                            job['status'] = 'error'
                            log.exception('[automatic-review] request=%s sheet=%s version=%s stage=finalize',
                                          job['request_id'], uid, STARTUP_HEALTH.get('commit'))
                        finally:
                            conn.close()
                    if job['status'] == 'error':
                        job['error'] = 'Parte da verificação não terminou. Os resultados guardados foram preservados.'
                    elif job['status'] == 'conflict':
                        job['error'] = 'A folha foi alterada durante a verificação. As alterações foram preservadas.'
                    self.jobs[uid] = job
                    log.info('[automatic-review] request=%s sheet=%s version=%s status=%s initial=%s final=%s stages=%s',
                             job['request_id'], uid, STARTUP_HEALTH.get('commit'), job['status'], job['revision'], job['final_revision'], job['stages'])
                    self.queue.task_done()
