"""Async review writes and optimistic concurrency independent of OCR diagnostics."""
from functools import wraps
import hashlib
import inspect
import json
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from .. import db
from ..review_guard import ReviewConflict, revision_guard, cancel_pending


def token(sheet):
    """A stale revision is reusable only if the reviewed production is identical."""
    data = {key: value for key, value in (sheet.get("sheet_data") or {}).items()
            if key not in {"_ocr_coverage", "_coverage_recovery", "_header_recovery"}}
    snapshot = {key: sheet.get(key) for key in (
        "uid", "template_name", "image_sha256", "image_rotation", "extraction_generation")}
    snapshot["data"] = data
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def revision_for(sheet, revision, review_token=""):
    if (sheet and revision is not None and revision < sheet["revision"]
            and review_token and review_token == token(sheet)):
        return sheet["revision"]
    return revision


def asynchronous(connect, view):
    """Preserve existing HTML responses; return committed state to async editors."""
    def decorate(action):
        signature = inspect.signature(action, eval_str=True)

        @wraps(action)
        def write(*args, **kwargs):
            values = signature.bind(*args, **kwargs)
            request = values.arguments["request"]
            if "application/json" not in request.headers.get("accept", ""):
                return action(*args, **kwargs)
            uid, back = values.arguments["uid"], values.arguments.get("back", "")
            cancel_pending(uid)
            conn = connect()
            try:
                before = db.get_sheet(conn, uid)
            finally:
                conn.close()
            if not before:
                return JSONResponse({"ok": False, "saved": False, "detail": "Folha inexistente."}, status_code=404)
            revision = revision_for(before, values.arguments.get("revision"),
                                    values.arguments.get("review_token", ""))
            if before["status"] == "validated" or revision != before["revision"]:
                return JSONResponse({"ok": False, "saved": False,
                    "detail": "A folha foi alterada. Os valores em edição foram mantidos."}, status_code=409)
            values.arguments["revision"] = revision
            error, code = None, 200
            with revision_guard(uid, revision) as guard:
                try:
                    response = action(*values.args, **values.kwargs)
                    query = parse_qs(urlsplit(response.headers.get("location", "")).query)
                    error = query.get("erro", [None])[0]
                    if response.status_code >= 400:
                        code = response.status_code
                        error = (getattr(response, "context", {}).get("erro")
                                 or getattr(request.state, "form_error", None)
                                 or "Não foi possível guardar. Os valores em edição foram mantidos.")
                    elif error:
                        code = 503
                except (HTTPException, ReviewConflict) as exc:
                    code = exc.status_code if isinstance(exc, HTTPException) else 409
                    error = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
                # Draft overlays belong to the legacy HTML error response, not to
                # the committed state returned to the asynchronous editor.
                for name in ("header_draft", "field_draft", "form_error", "form_focus", "error_context"):
                    if hasattr(request.state, name):
                        delattr(request.state, name)
                page = view(request, uid, back=back)
                current = page.context["sheet"]
                if current["revision"] != guard["revision"] or current["status"] == "validated":
                    code, error = 409, "A folha foi alterada. Os valores em edição foram mantidos."
                result = {"ok": error is None, "saved": guard["revision"] != revision or error is None,
                          "detail": error, "html": page.body.decode() if error is None else None}
                if code != 409:
                    result.update(revision=current["revision"], review_token=token(current))
                return JSONResponse(result, status_code=code)

        # Resolve postponed annotations in the endpoint's original module.
        write.__signature__ = signature
        return write
    return decorate
