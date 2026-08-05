"""Kanban MES — web app (FastAPI + Jinja2 + HTMX).

Fluxo: /capture (foto ou folha manual) → staging local → /sheet/{uid} revisão
com células coloridas → /validate = única porta para o Postgres.
"""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import db, pg_store
from ..config import settings
from ..matching import loaders
from ..matching.cross_check import check_sheet
from ..matching.params import CrossParams
from ..matching.scorer import Scorer
from ..ocr.provider import get_provider
from ..templates_spec import TEMPLATES, get_template

app = FastAPI(title="Kanban MES")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_INDEX_TTL_SECONDS = 600
_index_cache: dict[str, tuple[float, object]] = {}
_index_lock = threading.Lock()


def _conn():
    return db.connect()


def get_index(loader_name: str):
    """Índice do plano com cache TTL — o plano muda no máximo a cada reload do Excel."""
    now = time.monotonic()
    with _index_lock:
        hit = _index_cache.get(loader_name)
        if hit and now - hit[0] < _INDEX_TTL_SECONDS:
            return hit[1]
    index = getattr(loaders, loader_name)()
    with _index_lock:
        _index_cache[loader_name] = (time.monotonic(), index)
    return index


def make_scorer(template_name: str) -> Scorer:
    template = get_template(template_name)
    index = get_index(template.index_loader)
    active = loaders.load_active_ofs() if template.family == "cantoneiras" else set()
    return Scorer(index, CrossParams.load(), active_primary=active)


def run_cross_check(conn, uid: str) -> None:
    sheet = db.get_sheet(conn, uid)
    if not sheet or not sheet["sheet_data"]:
        return
    scorer = make_scorer(sheet["template_name"])
    rows = sheet["sheet_data"].get("rows") or []
    cross = check_sheet(rows, scorer, db.human_fields_by_row(conn, uid))

    # aplicar escrita automática (política de perda esperada), auditada como 'system'
    changed = False
    for rc in cross["rows"]:
        for cell in rc["cells"]:
            if cell["auto_write"] and cell["proposal"] is not None:
                i, f = rc["row_index"], cell["field"]
                old = rows[i].get(f)
                if str(old or "").strip() != cell["proposal"]:
                    db.record_edit(conn, uid, f"rows[{i}].{f}", old, cell["proposal"], "system", "cross")
                    rows[i][f] = cell["proposal"]
                    changed = True
    if changed:
        fresh = db.get_sheet(conn, uid)
        data = fresh["sheet_data"]
        data["rows"] = rows
        db.save_sheet_data(conn, uid, data, fresh["revision"])
    db.save_cross_check(conn, uid, cross)


# ---------- páginas ----------

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    conn = _conn()
    try:
        sheets = db.list_sheets(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(request, "home.html", {
        "sheets": sheets, "templates_list": list(TEMPLATES.values()),
    })


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    conn = _conn()
    try:
        sheets = db.list_sheets(conn)
    finally:
        conn.close()
    by_status: dict[str, int] = {}
    for s in sheets:
        by_status[s["status"]] = by_status.get(s["status"], 0) + 1

    # Lado Postgres: contagens de mes_kanban se o schema já estiver aplicado.
    pg = {"available": False, "error": None, "sheets": 0, "records": 0,
          "by_family": [], "recent": []}
    try:
        import psycopg

        with psycopg.connect(pg_store._dsn(), connect_timeout=3) as pconn:
            pconn.read_only = True
            with pconn.cursor() as cur:
                cur.execute("SELECT count(*) FROM mes_kanban.validated_sheets")
                pg["sheets"] = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM mes_kanban.production_records")
                pg["records"] = cur.fetchone()[0]
                cur.execute(
                    "SELECT family, count(*), coalesce(sum(quantity), 0) "
                    "FROM mes_kanban.production_records GROUP BY family ORDER BY family")
                pg["by_family"] = cur.fetchall()
                cur.execute(
                    "SELECT sheet_date, family, operator_name, machine, count(*) "
                    "FROM mes_kanban.production_records "
                    "GROUP BY sheet_date, family, operator_name, machine "
                    "ORDER BY sheet_date DESC LIMIT 10")
                pg["recent"] = cur.fetchall()
        pg["available"] = True
    except Exception as exc:  # schema ausente, sem rede, sem password — mostrar em vez de rebentar
        pg["error"] = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__

    return templates.TemplateResponse(request, "dashboard.html", {
        "by_status": by_status, "n_sheets": len(sheets), "pg": pg,
    })


@app.get("/capture", response_class=HTMLResponse)
def capture(request: Request):
    return templates.TemplateResponse(request, "capture.html", {
        "templates_list": list(TEMPLATES.values()),
    })


@app.post("/upload")
async def upload(request: Request, template_name: str = Form(...),
                 photo: UploadFile | None = None):
    template = get_template(template_name)
    conn = _conn()
    try:
        image_path = image_sha = None
        if photo is not None and photo.filename:
            content = await photo.read()
            image_sha = hashlib.sha256(content).hexdigest()
            settings.images_dir.mkdir(parents=True, exist_ok=True)
            dest = settings.images_dir / f"{image_sha[:16]}_{photo.filename}"
            dest.write_bytes(content)
            image_path = str(dest)
        uid = db.create_sheet(conn, template_name, image_path, image_sha)
        extraction = get_provider().extract(
            Path(image_path) if image_path else Path("/dev/null"), template)
        db.set_extraction(conn, uid, extraction)
        run_cross_check(conn, uid)
    finally:
        conn.close()
    return RedirectResponse(f"/sheet/{uid}", status_code=303)


@app.get("/sheet/{uid}", response_class=HTMLResponse)
def sheet_view(request: Request, uid: str):
    conn = _conn()
    try:
        sheet = db.get_sheet(conn, uid)
    finally:
        conn.close()
    if not sheet:
        raise HTTPException(404)
    template = get_template(sheet["template_name"])
    cross_rows = {}
    if sheet["cross_check"]:
        cross_rows = {
            r["row_index"]: {**r, "cells_by_field": {c["field"]: c for c in r["cells"]}}
            for r in sheet["cross_check"]["rows"]
        }
    return templates.TemplateResponse(request, "sheet.html", {
        "sheet": sheet, "t": template, "cross_rows": cross_rows,
        "summary": (sheet["cross_check"] or {}).get("summary"),
        "review_order": (sheet["cross_check"] or {}).get("review_order", []),
        "stored": request.query_params.get("stored"),
    })


@app.post("/sheet/{uid}/edit")
def sheet_edit(uid: str, field_path: str = Form(...), value: str = Form(""),
               revision: int = Form(...), actor: str = Form("operador")):
    conn = _conn()
    try:
        sheet = db.get_sheet(conn, uid)
        if not sheet:
            raise HTTPException(404)
        if sheet["status"] == "validated":
            raise HTTPException(409, "Folha validada é imutável.")
        data = sheet["sheet_data"] or {"header": {}, "rows": [], "footer": {}}
        # field_path: 'header.data' | 'rows[3].of' | 'footer.horas_trabalhadas'
        old = None
        value_clean = value.strip() or None
        if field_path.startswith("rows["):
            idx_s, _, fname = field_path[5:].partition("].")
            i = int(idx_s)
            while len(data["rows"]) <= i:
                data["rows"].append({})
            old = data["rows"][i].get(fname)
            data["rows"][i][fname] = value_clean
        elif "." in field_path:
            section, _, fname = field_path.partition(".")
            if section not in ("header", "footer"):
                raise HTTPException(400)
            old = (data.get(section) or {}).get(fname)
            data.setdefault(section, {})[fname] = value_clean
        else:
            raise HTTPException(400)
        # controlo otimista: a revisão vem do formulário — se a folha mudou
        # desde que a página foi carregada, recusa em vez de sobrescrever
        if not db.save_sheet_data(conn, uid, data, revision):
            raise HTTPException(409, "A folha mudou entretanto — recarrega a página.")
        db.record_edit(conn, uid, field_path, old, value_clean, "human", actor)
        run_cross_check(conn, uid)
    finally:
        conn.close()
    return RedirectResponse(f"/sheet/{uid}", status_code=303)


@app.post("/sheet/{uid}/add-row")
def add_row(uid: str):
    conn = _conn()
    try:
        sheet = db.get_sheet(conn, uid)
        if not sheet:
            raise HTTPException(404)
        if sheet["status"] == "validated":
            raise HTTPException(409, "Folha validada é imutável.")
        data = sheet["sheet_data"]
        template = get_template(sheet["template_name"])
        data["rows"].append({f: None for f in template.row_fields})
        db.save_sheet_data(conn, uid, data, sheet["revision"])
    finally:
        conn.close()
    return RedirectResponse(f"/sheet/{uid}", status_code=303)


@app.post("/sheet/{uid}/recheck")
def recheck(uid: str):
    conn = _conn()
    try:
        sheet = db.get_sheet(conn, uid)
        if not sheet:
            raise HTTPException(404)
        if sheet["status"] == "validated":
            raise HTTPException(409, "Folha validada é imutável.")
        run_cross_check(conn, uid)
    finally:
        conn.close()
    return RedirectResponse(f"/sheet/{uid}", status_code=303)


@app.post("/sheet/{uid}/validate")
def validate(uid: str, actor: str = Form("operador")):
    """A única porta para o Postgres: valida → INSERT em mes_kanban → imutável."""
    conn = _conn()
    try:
        sheet = db.get_sheet(conn, uid)
        if not sheet:
            raise HTTPException(404)
        if sheet["status"] == "validated":
            raise HTTPException(409, "Folha já validada.")
        header = (sheet["sheet_data"] or {}).get("header") or {}
        if not str(header.get("operador") or "").strip():
            raise HTTPException(422, "Validação exige operador preenchido no cabeçalho.")
        if not str(header.get("data") or "").strip():
            raise HTTPException(422, "Validação exige data preenchida no cabeçalho.")
        template = get_template(sheet["template_name"])
        n = pg_store.store_validated_sheet(
            sheet, template, db.edit_count(conn, uid), actor)
        db.mark_validated(conn, uid, actor)
    finally:
        conn.close()
    return RedirectResponse(f"/sheet/{uid}?stored={n}", status_code=303)
