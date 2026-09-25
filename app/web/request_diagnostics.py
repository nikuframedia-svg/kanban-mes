"""Correlate sheet failures with the running build, without leaking internals."""
import logging
import re
import time
import uuid
from .. import pg
from ..health import STARTUP_HEALTH

log = logging.getLogger("uvicorn.error.sheet_requests")
timing_log = logging.getLogger("uvicorn.error.timing")

# Pedidos acima disto ficam no log com o tempo gasto no Postgres: é assim que
# se vê, no PC da fábrica, se a lentidão é do túnel ou da própria app.
SLOW_REQUEST_MS = 300.0


def install(app, templates, safe_back):
    @app.middleware("http")
    async def sheet_requests(request, call_next):
        request.state.request_id = uuid.uuid4().hex
        match = re.fullmatch(r"/sheet/([^/]+)/?", request.url.path)
        sheet_uid = request.url.path.split("/")[2] if request.url.path.startswith("/sheet/") else None
        started = time.perf_counter()
        with pg.measure() as stats:
            try:
                response = await call_next(request)
            except Exception:
                log.exception("[sheet-request] request=%s sheet=%s version=%s stage=render", request.state.request_id,
                              sheet_uid, STARTUP_HEALTH.get("commit"))
                raise
        total_ms = (time.perf_counter() - started) * 1000.0
        if sheet_uid and response.status_code >= 400:
            log.warning("[sheet-request] request=%s sheet=%s version=%s stage=%s status=%s",
                        request.state.request_id, sheet_uid, STARTUP_HEALTH.get("commit"),
                        "open" if match else request.url.path.rsplit("/", 1)[-1], response.status_code)
        if match and request.method == "GET" and response.status_code == 404:
            response = templates.TemplateResponse(request, "sheet_not_found.html", {
                "back_url": safe_back(request.query_params.get("back")) or "/",
                "request_id": request.state.request_id,
            }, status_code=404)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Server-Timing"] = (
            f"app;dur={total_ms:.0f}, "
            f'pg;dur={stats["ms"]:.0f};desc="{stats["queries"]} q {stats["connects"]} conn"'
        )
        if total_ms >= SLOW_REQUEST_MS:
            timing_log.info(
                "[timing] %s %s status=%s total_ms=%.0f pg_ms=%.0f pg_queries=%d "
                "pg_connects=%d request=%s",
                request.method, request.url.path, response.status_code, total_ms,
                stats["ms"], stats["queries"], stats["connects"], request.state.request_id,
            )
        return response
