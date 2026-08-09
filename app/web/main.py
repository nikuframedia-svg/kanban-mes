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
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import db, imaging, pg_store
from ..config import settings
from ..matching import carryover, loaders, operador
from ..matching.cross_check import check_sheet
from ..matching.params import CrossParams
from ..matching.scorer import Scorer
from ..ocr.provider import OcrError, empty_extraction, get_provider
from ..templates_spec import TEMPLATES, field_value, get_template, is_marked
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
# a folha decide o que é uma marca; o template não repete a regra
templates.env.globals["is_marked"] = is_marked
# lê a célula pelo nome atual e pelo antigo (folhas lidas antes do rename)
templates.env.globals["field_value"] = field_value


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


def get_employees():
    """Colaboradores com a mesma cache do índice do plano."""
    return get_index("load_employees")


def make_scorer(template_name: str) -> Scorer:
    template = get_template(template_name)
    index = get_index(template.index_loader)
    active = loaders.load_active_ofs() if template.family == "cantoneiras" else set()
    return Scorer(index, CrossParams.load(), active_primary=active)


def resolve_operator(conn, uid: str, sheet: dict) -> dict | None:
    """Resolve o operador da folha contra a lista de colaboradores.

    Corre para TODAS as folhas, incluindo o verso (paragens): é lá que estão
    metade dos casos, e a mesma pessoa aparecia com nomes diferentes na frente
    e no verso da mesma folha física.
    """
    header = (sheet["sheet_data"] or {}).get("header") or {}
    try:
        employees = get_employees()
    except Exception:
        return None
    if not employees:
        return None

    match = operador.resolve(header.get("operador"), header.get("n_operador"), employees)
    if match.cod is None and match.pernr is None:
        return asdict(match)

    protegidos = db.human_header_fields(conn, uid)
    data = sheet["sheet_data"]
    changed = False
    for field, value in (("operador", match.name), ("n_operador", str(match.cod or ""))):
        if not value or field in protegidos:
            continue
        if str(header.get(field) or "").strip() != value:
            db.record_edit(conn, uid, f"header.{field}", header.get(field), value,
                           "system", "colaboradores")
            data["header"][field] = value
            changed = True
    if changed:
        fresh = db.get_sheet(conn, uid)
        fresh_data = fresh["sheet_data"]
        fresh_data["header"] = data["header"]
        db.save_sheet_data(conn, uid, fresh_data, fresh["revision"])
    return asdict(match)


def run_cross_check(conn, uid: str) -> None:
    sheet = db.get_sheet(conn, uid)
    if not sheet or not sheet["sheet_data"]:
        return
    # O cabeçalho resolve-se sempre, antes de qualquer saída antecipada.
    operator_match = resolve_operator(conn, uid, sheet)
    if get_template(sheet["template_name"]).index_loader is None:
        # ex.: paragens — não há plano contra que cruzar, mas o operador já foi
        # resolvido e vale a pena guardar como.
        if operator_match:
            db.save_cross_check(conn, uid, {"summary": {}, "review_order": [],
                                            "rows": [], "operator": operator_match})
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
    if operator_match:
        cross["operator"] = operator_match
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
        # O OCR lê a folha na orientação de leitura, não como ela saiu do
        # scanner: com a folha deitada o modelo troca colunas.
        image_path = imaging.render_oriented(
            Path(sheet["image_path"]), int(sheet.get("image_rotation") or 0)
        )
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


def _diverged_map(sheet: dict) -> dict[str, str]:
    """Células onde o valor atual já não é o que a máquina leu.

    Devolve {field_path: valor_lido_pelo_ocr} — o valor original serve de
    tooltip, para o revisor saber de onde é que a célula veio sem ter de
    trocar de vista. Cobre cabeçalho, linhas e rodapé.
    """
    raw = sheet.get("raw_extraction") or {}
    cur = sheet.get("sheet_data") or {}
    if not raw:
        return {}
    out: dict[str, str] = {}

    def diff(path: str, a, b) -> None:
        sa, sb = str(a or "").strip(), str(b or "").strip()
        if sa != sb:
            out[path] = sa

    for section in ("header", "footer"):
        raw_sec, cur_sec = raw.get(section) or {}, cur.get(section) or {}
        for f in set(raw_sec) | set(cur_sec):
            diff(f"{section}.{f}", raw_sec.get(f), cur_sec.get(f))

    raw_rows, cur_rows = raw.get("rows") or [], cur.get("rows") or []
    for i in range(max(len(raw_rows), len(cur_rows))):
        r = raw_rows[i] if i < len(raw_rows) and isinstance(raw_rows[i], dict) else {}
        c = cur_rows[i] if i < len(cur_rows) and isinstance(cur_rows[i], dict) else {}
        for f in set(r) | set(c):
            diff(f"rows[{i}].{f}", r.get(f), c.get(f))
    return out


@app.get("/sheet/{uid}", response_class=HTMLResponse)
def sheet_view(request: Request, uid: str, back: str | None = None,
               view: str | None = None):
    conn = _conn()
    try:
        sheet = db.get_sheet(conn, uid)
    finally:
        conn.close()
    if not sheet:
        raise HTTPException(404)
    template = get_template(sheet["template_name"])
    raw = sheet.get("raw_extraction") or {}
    raw_rows = [r for r in (raw.get("rows") or []) if isinstance(r, dict)]
    has_ocr = bool(sheet.get("image_path")) and any(
        v is not None and str(v).strip() for r in raw_rows for v in r.values()
    )
    # Vista crua: mostra a transcrição original e DESLIGA as cores. As cores do
    # cross-check validam o valor final contra o plano — pintá-las por cima de
    # valores crus seria dizer que o motor aprovou o que ele nunca viu.
    view_mode = "raw" if (view == "raw" and raw) else "final"
    cross_rows = {}
    if view_mode == "final" and sheet["cross_check"]:
        cross_rows = {
            r["row_index"]: {**r, "cells_by_field": {c["field"]: c for c in r["cells"]}}
            for r in sheet["cross_check"]["rows"]
        }
    diverged = _diverged_map(sheet) if view_mode == "final" else {}
    return templates.TemplateResponse(request, "sheet.html", {
        "sheet": sheet, "t": template, "cross_rows": cross_rows,
        "summary": (sheet["cross_check"] or {}).get("summary"),
        "review_order": (sheet["cross_check"] or {}).get("review_order", []),
        "stored": request.query_params.get("stored"),
        "has_ocr": has_ocr, "view_mode": view_mode,
        "operator": (sheet["cross_check"] or {}).get("operator"),
        "diverged": diverged, "n_diverged": len(diverged),
        "back_url": _safe_back(back),
    })


@app.get("/sheet/{uid}/photo")
def sheet_photo(uid: str, original: int = 0):
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
    if not original:
        path = imaging.render_oriented(path, int(sheet.get("image_rotation") or 0))
    return FileResponse(path)


@app.post("/sheet/{uid}/rotate")
def sheet_rotate(uid: str):
    """Roda mais 90° no sentido horário, por cima da correcção automática.

    A automática acerta em todas as digitalizações que vimos, mas é um palpite
    sobre o conteúdo a partir da forma da imagem — se sair ao contrário, o
    revisor resolve com cliques.
    """
    conn = _conn()
    try:
        sheet = db.get_sheet(conn, uid)
        if not sheet:
            raise HTTPException(404)
        rotation = db.set_image_rotation(conn, uid, int(sheet.get("image_rotation") or 0) + 90)
    finally:
        conn.close()
    return {"ok": True, "rotation": rotation}


@app.get("/sheet/{uid}/plano/{row_index}", response_class=HTMLResponse)
def sheet_plano_perfil(request: Request, uid: str, row_index: int):
    """As referências do plano para a chave OF + Perfil de uma linha.

    Recebe a linha e não a chave: é o servidor que resolve a OF (incluindo a
    herdada da linha de cima) e a forma canónica do perfil. Se fosse o template
    a montar `?of=&perfil=`, teria de conhecer as convenções do plano e podia
    perguntar por uma chave diferente daquela com que o motor cruzou.
    """
    conn = _conn()
    try:
        sheet = db.get_sheet(conn, uid)
        human = db.human_fields_by_row(conn, uid)
    finally:
        conn.close()
    if not sheet:
        raise HTTPException(404)
    template = get_template(sheet["template_name"])
    rows = (sheet["sheet_data"] or {}).get("rows") or []
    if row_index < 0 or row_index >= len(rows):
        raise HTTPException(404)

    ctx: dict = {"row_index": row_index, "of": None, "perfil": None,
                 "linhas": [], "perfis": [], "erro": None, "plano": {}}
    try:
        index = get_index(template.index_loader) if template.index_loader else None
        if index is None:
            ctx["erro"] = "Esta folha não cruza com o plano."
            return templates.TemplateResponse(request, "_plano_perfil.html", ctx)

        content = tuple(f.name for f in index.spec.identity_fields
                        if f.name not in carryover.CARRY_FIELDS)
        identities = carryover.resolve(rows, content, human)
        eff = carryover.effective_row(rows[row_index], identities[row_index])
        escrito = str(eff.get("perfil") or "").strip()
        of_escrita = str(eff.get("of") or "").strip()
        # A OF no plano leva prefixo; procurar pela forma que lá existe.
        of = next(
            (index.entries[i]["of"] for i in index.exact_matches("of", of_escrita)),
            of_escrita,
        ) if of_escrita else ""
        perfil = index.normalize_written("perfil", escrito) if escrito else ""
        ctx.update({"of": of, "perfil": perfil, "perfil_escrito": escrito,
                    "herdou_of": identities[row_index].is_inherited("of")})
        if not of:
            ctx["erro"] = "Esta linha não tem OF — escreve-a (ou herda-a da linha de cima)."
        else:
            ctx["plano"] = loaders.plan_snapshot_info()
            if perfil:
                ctx["linhas"] = loaders.fetch_profile_lines(of, perfil)
            if not ctx["linhas"]:
                # Sem correspondência mostra-se o que a obra tem mesmo: o caso
                # comum é o perfil estar escrito com uma medida trocada.
                ctx["perfis"] = loaders.fetch_profiles_in_of(of)
    except Exception as exc:  # Postgres em baixo não pode rebentar a revisão
        ctx["erro"] = f"Não foi possível ler o plano: {exc}"
    return templates.TemplateResponse(request, "_plano_perfil.html", ctx)


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
