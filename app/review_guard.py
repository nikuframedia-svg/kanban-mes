"""Track this job's own CAS commits without adopting concurrent revisions."""
from contextvars import ContextVar
from contextlib import contextmanager

_active = ContextVar("automatic_revision_guard", default=None)

class ReviewConflict(ValueError):
    pass

@contextmanager
def revision_guard(uid, revision, request_id=None):
    state = {"uid": uid, "revision": revision, "request_id": request_id}
    token = _active.set(state)
    try:
        yield state
    finally:
        _active.reset(token)

def check(uid, revision):
    state = _active.get()
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
