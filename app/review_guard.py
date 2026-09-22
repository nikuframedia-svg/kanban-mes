"""Track this job's own CAS commits without adopting concurrent revisions."""
from contextvars import ContextVar
from contextlib import contextmanager
from threading import Lock

_active = ContextVar("automatic_revision_guard", default=None)

_generations = {}
_generation_lock = Lock()

def generation(uid):
    with _generation_lock:
        return _generations.get(uid, 0)

def cancel_pending(uid):
    """Revoke queued/in-flight automatic writes without waiting for OCR."""
    with _generation_lock:
        _generations[uid] = _generations.get(uid, 0) + 1

class ReviewConflict(ValueError):
    pass

@contextmanager
def revision_guard(uid, revision, request_id=None, *, automatic_generation=None):
    state = {"uid": uid, "revision": revision, "request_id": request_id, "automatic_generation": automatic_generation}
    token = _active.set(state)
    try:
        yield state
    finally:
        _active.reset(token)

def check(uid, revision):
    state = _active.get()
    if (state is not None and state["uid"] == uid
            and state["automatic_generation"] is not None
            and generation(uid) != state["automatic_generation"]):
        raise ReviewConflict("A revisão humana tem prioridade sobre a leitura automática.")
    if state is not None and state["uid"] == uid and revision != state["revision"]:
        raise ReviewConflict("A folha mudou durante a verificação; as alterações foram preservadas.")

def committed(uid, revision, changed):
    state = _active.get()
    if state is not None and state["uid"] == uid:
        if not changed:
            raise ReviewConflict("A folha mudou durante a verificação; as alterações foram preservadas.")
        state["revision"] = revision


def request_id():
    return (_active.get() or {}).get("request_id")
