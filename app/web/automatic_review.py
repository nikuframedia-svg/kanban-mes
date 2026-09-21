"""Serial, deduplicated review jobs; all writes still use service revision guards."""
import queue
import threading
from fastapi import Form, HTTPException
from .. import db


class AutomaticReview:
    def __init__(self, app, connect, eligible, process):
        self.connect, self.eligible, self.process = connect, eligible, process
        self.jobs = {}
        self.queue = queue.Queue()
        self.lock = threading.Lock()
        self.worker = None

        @app.post('/sheet/{uid}/automatic-review')
        def start(uid: str, revision: int = Form(...)):
            return self.enqueue(uid, revision)

        @app.get('/sheet/{uid}/automatic-review')
        def status(uid: str):
            conn = connect()
            try:
                sheet = db.get_sheet(conn, uid)
                if not sheet:
                    raise HTTPException(404)
                return {**self.jobs.get(uid, {'status': 'idle'}), 'current_revision': sheet['revision']}
            finally:
                conn.close()

    def needed(self, conn, sheet):
        if not sheet or sheet['status'] not in {'extracted', 'in_review'}:
            return False
        previous = self.jobs.get(sheet['uid'], {})
        if previous.get('revision') == sheet['revision'] and previous.get('status') in {'complete', 'error'}:
            return False
        return self.eligible(conn, sheet)

    def enqueue(self, uid, revision, force=False):
        conn = self.connect()
        try:
            sheet = db.get_sheet(conn, uid)
            if not sheet:
                raise HTTPException(404)
            if sheet['revision'] != revision or sheet['status'] not in {'extracted', 'in_review'}:
                raise HTTPException(409, 'A folha mudou; a verificação automática não substituiu dados.')
            with self.lock:
                previous = self.jobs.get(uid, {})
                if previous.get('status') in {'queued', 'running'}:
                    return previous
                if not force and not self.needed(conn, sheet):
                    return {'status': 'complete', 'revision': revision}
                self.jobs[uid] = {'status': 'queued', 'revision': revision}
                self.queue.put((uid, revision))
                if self.worker is None or not self.worker.is_alive():
                    self.worker = threading.Thread(target=self._work, daemon=True)
                    self.worker.start()
                return dict(self.jobs[uid])
        finally:
            conn.close()

    def _work(self):
        while True:
            uid, revision = self.queue.get()
            self.jobs[uid] = {'status': 'running', 'revision': revision}
            conn = None
            try:
                conn = self.connect()
                result = self.process(conn, uid, revision)
                self.jobs[uid] = {'status': 'complete', 'revision': revision, 'result': result}
            except Exception as exc:
                self.jobs[uid] = {'status': 'error', 'revision': revision,
                                 'error': 'A verificação automática não terminou. Os dados guardados foram preservados.'}
                print(f'[automatic-review] {uid}: {type(exc).__name__}: {exc}', flush=True)
            finally:
                if conn is not None:
                    conn.close()
                self.queue.task_done()
