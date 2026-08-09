"""Kanban MES — web app (FastAPI + Jinja2 + HTMX).

Fluxo: /capture (foto ou folha manual) → staging local → /sheet/{uid} revisão
com células coloridas → /validate = única porta para o Postgres.
"""

from __future__ import annotations

import csv
import hashlib
import io
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import db, pg_store
from ..config import settings
from ..matching import loaders
from ..matching.cross_check import check_sheet
from ..matching.params import CrossParams
from ..matching.scorer import Scorer
from ..ocr.provider import OcrError, empty_extraction, get_provider
from ..templates_spec import TEMPLATES, get_template
from . import estado as estado_data
from . import pdf as pdf_gen

class NoCacheStaticFiles(StaticFiles):
    """Estáticos com revalidação obrigatória — o link leva ?v=<hash>, e isto
    impede o browser/edge da Cloudflare de servir CSS velho a quem tem o URL antigo."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


_STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Kanban MES")
app.mount("/static", NoCacheStaticFiles(directory=str(_STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["css_version"] = hashlib.sha1(
    (_STATIC_DIR / "design.css").read_bytes()
).hexdigest()[:10]


@app.middleware("http")
async def _attach_watermark(request: Request, call_next):
    """Marca de água = folha mais recente no momento do pedido.

    A página do histórico guarda o valor do primeiro paint e compara-o com o
    header de cada poll HTMX; a diferença são as folhas que entraram entretanto,
    e é isso que alimenta o banner «N folhas novas».
    """
    watermark = 0
    if request.method == "GET":
        try:
            conn = _conn()
            try:
                row = conn.execute("SELECT MAX(rowid) AS m FROM sheets").fetchone()
                watermark = int(row["m"] or 0)
            finally:
                conn.close()
        except Exception:
            watermark = 0
    request.state.watermark = watermark
    response = await call_next(request)
    response.headers["X-Sheet-Watermark"] = str(watermark)
    return response


def tunnel_url() -> str | None:
    """URL público atual do túnel Cloudflare (escrito por scripts/tunnel.sh)."""
    try:
        text = (settings.data_dir / "tunnel_url.txt").read_text().strip()
        return text.splitlines()[-1] if text else None
    except OSError:
        return None

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
    if get_template(sheet["template_name"]).index_loader is None:
        return  # ex.: paragens — não há plano contra que cruzar
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
def home(request: Request, status: str = "", operador: str = "", setor: str = "",
         data: str = "", data_captura: str = "", of: str = "",
         created: str = "", deleted: str = ""):
    conn = _conn()
    try:
        sheets = db.list_sheets(conn, status=status or None, operador=operador or None,
                                setor=setor or None, data_folha=data or None,
                                data_captura=data_captura or None, of=of or None)
        options = db.filter_options(conn)
    finally:
        conn.close()
    # querystring dos filtros não-status, para os chips preservarem os filtros
    parts = [f"&{k}={v}" for k, v in (("operador", operador), ("setor", setor),
                                      ("data", data), ("data_captura", data_captura),
                                      ("of", of)) if v]
    return templates.TemplateResponse(request, "home.html", {
        "sheets": sheets, "options": options,
        "f": {"status": status, "operador": operador, "setor": setor,
              "data": data, "data_captura": data_captura, "of": of},
        "filter_qs": "".join(parts),
        "created": created, "deleted": deleted,
        "tunnel_url": tunnel_url(),
    })


@app.get("/captura", response_class=HTMLResponse)
def captura(request: Request):
    return templates.TemplateResponse(request, "captura.html", {
        "templates_list": list(TEMPLATES.values()),
    })


@app.get("/captura/camara", response_class=HTMLResponse)
def camara(request: Request):
    return templates.TemplateResponse(request, "camara.html", {
        "templates_list": list(TEMPLATES.values()),
    })


@app.get("/estado", response_class=HTMLResponse)
def estado_page(request: Request, q: str = "", familia: str = "", of: str = ""):
    conn = _conn()
    try:
        sheets = db.list_sheets(conn)
    finally:
        conn.close()
    by_status: dict[str, int] = {}
    for s in sheets:
        by_status[s["status"]] = by_status.get(s["status"], 0) + 1
    return templates.TemplateResponse(request, "estado.html", {
        "estado": estado_data.load_estado(q, familia, of),
        "q": q, "familia": familia, "of": of,
        "by_status": by_status, "n_sheets": len(sheets),
    })


@app.get("/estado/pdf")
def estado_pdf(q: str = "", familia: str = ""):
    data = estado_data.load_estado(q, familia)
    if not data["available"]:
        raise HTTPException(503, f"Postgres indisponível: {data['error']}")
    content = pdf_gen.estado_pdf(data["rows"], q, familia, data["kpis"])
    return Response(content, media_type="application/pdf", headers={
        "Content-Disposition": 'attachment; filename="estado_producao.pdf"',
    })


# rotas antigas — não partir bookmarks
@app.get("/dashboard")
def dashboard_redirect():
    return RedirectResponse("/estado", status_code=301)


@app.get("/capture")
def capture_redirect():
    return RedirectResponse("/captura", status_code=301)


def _save_image(content: bytes, filename: str) -> tuple[str, str]:
    sha = hashlib.sha256(content).hexdigest()
    settings.images_dir.mkdir(parents=True, exist_ok=True)
    dest = settings.images_dir / f"{sha[:16]}_{filename}"
    dest.write_bytes(content)
    return str(dest), sha


def _pdf_to_images(content: bytes, stem: str) -> list[tuple[bytes, str]]:
    """PDF de kanbans digitalizados: cada página vira uma imagem PNG."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(content)
    out: list[tuple[bytes, str]] = []
    try:
        for i in range(len(doc)):
            pil = doc[i].render(scale=2.0).to_pil()
            buf = io.BytesIO()
            pil.save(buf, format="PNG")
            out.append((buf.getvalue(), f"{stem}_p{i + 1}.png"))
    finally:
        doc.close()
    return out


# O OCR corre em segundo plano: o túnel Cloudflare corta pedidos aos ~100 s e um
# lote de PDF real (12 páginas × 2 chamadas Gemini) demora minutos. O upload
# responde já; o worker preenche as folhas e o Histórico (auto-refresh) mostra-as.
PROCESS_IN_BACKGROUND = True


def _process_sheet(uid: str) -> None:
    conn = db.connect()
    try:
        sheet = db.get_sheet(conn, uid)
        if not sheet or sheet["status"] != "pending" or not sheet.get("image_path"):
            return
        template_name = sheet["template_name"]
        template = get_template(template_name)
        provider = get_provider()
        image_path = Path(sheet["image_path"])
        # folha TPL102 tem frente (produção) e verso (paragens): detetar por página
        if template_name == "cantoneiras_kanban" and hasattr(provider, "classify_page"):
            try:
                if provider.classify_page(image_path) == "paragens":
                    template_name = "cantoneiras_paragens"
                    template = get_template(template_name)
                    db.set_template(conn, uid, template_name)
            except OcrError:
                pass  # em dúvida, segue como produção
        try:
            extraction = provider.extract(image_path, template)
        except OcrError as exc:
            # OCR falhou (rede, quota, chave): a folha abre vazia para
            # preenchimento manual; o erro fica no trilho de auditoria
            extraction = empty_extraction(template)
            extraction["_ocr_error"] = str(exc)
        db.set_extraction(conn, uid, extraction)
        run_cross_check(conn, uid)
    except Exception as exc:  # nunca matar o worker do lote por causa de uma folha
        print(f"[worker] folha {uid}: {exc}", flush=True)
    finally:
        conn.close()


def _process_batch(uids: list[str]) -> None:
    for uid in uids:
        _process_sheet(uid)
        time.sleep(6.0)  # 2 chamadas/folha: manter o lote abaixo do limite RPM


@app.post("/upload")
async def upload(template_name: str = Form(...), photos: list[UploadFile] = []):
    template = get_template(template_name)
    images: list[tuple[bytes, str]] = []
    for up in photos:
        if not up.filename:
            continue
        content = await up.read()
        name = Path(up.filename).name
        if name.lower().endswith(".pdf") or up.content_type == "application/pdf":
            images.extend(_pdf_to_images(content, Path(name).stem))
        else:
            images.append((content, name))

    conn = _conn()
    try:
        if not images:
            # sem ficheiros: folha vazia para preenchimento manual (imediato)
            uid = db.create_sheet(conn, template_name)
            db.set_extraction(conn, uid, empty_extraction(template))
            run_cross_check(conn, uid)
            uids = [uid]
        else:
            uids = []
            for content, name in images:
                image_path, sha = _save_image(content, name)
                uids.append(db.create_sheet(conn, template_name, image_path, sha))
    finally:
        conn.close()

    if images:
        if PROCESS_IN_BACKGROUND:
            threading.Thread(target=_process_batch, args=(uids,), daemon=True).start()
        else:  # testes: determinístico
            _process_batch(uids)

    if len(uids) == 1:
        return RedirectResponse(f"/sheet/{uids[0]}", status_code=303)
    return RedirectResponse(f"/?created={len(uids)}", status_code=303)


@app.get("/upload")
def upload_get_redirect():
    """Um refresh depois de um upload interrompido não deve dar 405."""
    return RedirectResponse("/captura", status_code=303)


@app.post("/sheet/{uid}/reocr")
def sheet_reocr(uid: str):
    """Re-ler a foto com OCR (ex.: depois de um 429 de quota)."""
    conn = _conn()
    try:
        sheet = db.get_sheet(conn, uid)
        if not sheet:
            raise HTTPException(404)
        if sheet["status"] == "validated":
            raise HTTPException(409, "Folha validada é imutável.")
        if not sheet.get("image_path"):
            raise HTTPException(422, "Folha sem foto — não há nada para reler.")
        db.mark_pending(conn, uid)
    finally:
        conn.close()
    if PROCESS_IN_BACKGROUND:
        threading.Thread(target=_process_sheet, args=(uid,), daemon=True).start()
    else:
        _process_sheet(uid)
    return RedirectResponse(f"/sheet/{uid}", status_code=303)


@app.post("/sheet/{uid}/delete")
def sheet_delete(uid: str):
    conn = _conn()
    try:
        try:
            image_path = db.delete_sheet(conn, uid)
        except KeyError:
            raise HTTPException(404)
        except PermissionError:
            raise HTTPException(409, "Folha validada é imutável — não se apaga.")
    finally:
        conn.close()
    if image_path:
        p = Path(image_path).resolve()
        if p.is_relative_to(settings.images_dir.resolve()) and p.is_file():
            p.unlink()
    return RedirectResponse("/?deleted=1", status_code=303)


@app.get("/sheet/{uid}/csv")
def sheet_csv(uid: str):
    conn = _conn()
    try:
        sheet = db.get_sheet(conn, uid)
    finally:
        conn.close()
    if not sheet:
        raise HTTPException(404)
    template = get_template(sheet["template_name"])
    data = sheet["sheet_data"] or {}
    header = data.get("header") or {}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["folha", "estado", "operador", "data", "setor_maquina", "linha"]
               + list(template.row_fields))
    for i, row in enumerate(data.get("rows") or []):
        if not any(v is not None and str(v).strip() for v in row.values()):
            continue
        w.writerow([
            sheet["uid"][:8], sheet["status"], header.get("operador"),
            header.get("data"), header.get("setor_maquina"), i + 1,
        ] + [row.get(f) for f in template.row_fields])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="kanban_{uid[:8]}.csv"',
    })


@app.get("/export.xlsx")
def export_xlsx():
    """Todas as linhas de produção validadas (Postgres) num Excel."""
    import openpyxl
    import psycopg

    try:
        with psycopg.connect(pg_store._dsn(), connect_timeout=5) as pconn:
            pconn.read_only = True
            with pconn.cursor() as cur:
                cur.execute(
                    "SELECT sheet_uid, row_index, sheet_date, family, operator_name, "
                    "machine, production_order, sales_order, customer_name, model_ref, "
                    "matched_plan_key, match_confidence, quantity, length_mm, width_mm, "
                    "thickness_mm, hours_worked, validated_at "
                    "FROM mes_kanban.production_records "
                    "ORDER BY sheet_date DESC, sheet_uid, row_index")
                cols = [d.name for d in cur.description]
                rows = cur.fetchall()
    except Exception as exc:
        raise HTTPException(503, f"Postgres indisponível: {exc}")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "producao"
    ws.append(cols)
    for r in rows:
        ws.append([str(v) if v is not None and not isinstance(v, (int, float)) else v
                   for v in r])
    buf = io.BytesIO()
    wb.save(buf)
    return Response(buf.getvalue(),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="producao_mes.xlsx"'})


def _safe_back(back: str | None) -> str | None:
    """Só aceita caminhos internos — impede que um ?back= leve para fora do site."""
    if not back or not back.startswith("/") or back.startswith("//"):
        return None
    return back


@app.get("/sheet/{uid}", response_class=HTMLResponse)
def sheet_view(request: Request, uid: str, back: str | None = None):
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
    raw = sheet.get("raw_extraction") or {}
    raw_rows = [r for r in (raw.get("rows") or []) if isinstance(r, dict)]
    has_ocr = bool(sheet.get("image_path")) and any(
        v is not None and str(v).strip() for r in raw_rows for v in r.values()
    )
    # células onde o estado atual já difere do que a máquina leu (edições motor+humanas)
    cur_rows = (sheet["sheet_data"] or {}).get("rows") or []
    raw_diverged = sum(
        1
        for i, r in enumerate(raw_rows) if i < len(cur_rows)
        for f in template.row_fields
        if str(r.get(f) or "").strip() != str(cur_rows[i].get(f) or "").strip()
    ) if has_ocr else 0
    return templates.TemplateResponse(request, "sheet.html", {
        "sheet": sheet, "t": template, "cross_rows": cross_rows,
        "summary": (sheet["cross_check"] or {}).get("summary"),
        "review_order": (sheet["cross_check"] or {}).get("review_order", []),
        "stored": request.query_params.get("stored"),
        "has_ocr": has_ocr, "raw_diverged": raw_diverged,
        "back_url": _safe_back(back),
    })


@app.get("/sheet/{uid}/photo")
def sheet_photo(uid: str):
    conn = _conn()
    try:
        sheet = db.get_sheet(conn, uid)
    finally:
        conn.close()
    if not sheet or not sheet.get("image_path"):
        raise HTTPException(404)
    path = Path(sheet["image_path"]).resolve()
    # a foto tem de viver dentro da pasta de imagens da app (anti path-traversal)
    if not path.is_relative_to(settings.images_dir.resolve()) or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


@app.get("/sheet/{uid}/pdf")
def sheet_pdf(uid: str):
    conn = _conn()
    try:
        sheet = db.get_sheet(conn, uid)
        edits = db.edit_count(conn, uid)
    finally:
        conn.close()
    if not sheet:
        raise HTTPException(404)
    template = get_template(sheet["template_name"])
    content = pdf_gen.sheet_pdf(sheet, template, edits)
    return Response(content, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="kanban_{uid[:8]}.pdf"',
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
