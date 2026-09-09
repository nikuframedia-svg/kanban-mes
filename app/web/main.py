"""Kanban MES — web app (FastAPI + Jinja2 + HTMX).

Fluxo: /capture (foto ou folha manual) → staging local → /sheet/{uid} revisão
com células coloridas → /validate = única porta para o Postgres.
"""

from __future__ import annotations

import copy
import csv
import datetime
import hashlib
import html
import io
import json
import re
import threading
import time
import traceback
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from urllib.parse import unquote_plus, urlencode, urlsplit, urlunsplit

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import db, imaging, pg_store, production_facts
from ..config import settings
from ..health import STARTUP_HEALTH
from ..matching import carryover, header_cross, loaders, operador
from ..matching.cross_check import check_sheet
from ..matching.params import CrossParams
from ..matching.scorer import Scorer
from ..ocr.provider import OcrError, empty_extraction, get_provider, rescue_header
from ..templates_spec import TEMPLATES, field_value, get_template, is_marked
from . import estado as estado_data
from . import export as cpis_export
from . import pdf as pdf_gen

class NoCacheStaticFiles(StaticFiles):
    """Estáticos com revalidação obrigatória — o link leva ?v=<hash>, e isto
    impede o browser/edge da Cloudflare de servir CSS velho a quem tem o URL antigo."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


_STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """No arranque, re-enfileirar folhas que um restart deixou em `pending`:
    as threads do worker são daemon e morrem com o processo — sem isto, um
    deploy a meio de um lote deixava folhas em spinner eterno."""
    try:
        conn = db.connect()
        try:
            stuck = db.pending_with_image(conn)
        finally:
            conn.close()
        if stuck:
            print(f"[arranque] a retomar OCR de {len(stuck)} folha(s) pendente(s)", flush=True)
            threading.Thread(target=_process_batch, args=(stuck,), daemon=True).start()
    except Exception as exc:
        print(f"[arranque] retoma de pendentes falhou: {exc}", flush=True)
    yield


app = FastAPI(title="Kanban MES", lifespan=_lifespan)
app.mount("/static", NoCacheStaticFiles(directory=str(_STATIC_DIR)), name="static")
@app.get("/health")
def health():
    return dict(STARTUP_HEALTH)


templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["css_version"] = hashlib.sha1(
    (_STATIC_DIR / "design.css").read_bytes()
).hexdigest()[:10]
# a folha decide o que é uma marca; o template não repete a regra
templates.env.globals["is_marked"] = is_marked
# OF/OV apresentam-se como números puros (convenção do planeamento)
from ..matching import similarity as _sim  # noqa: E402
templates.env.globals["strip_ref"] = _sim.strip_ref_prefix
# lê a célula pelo nome atual e pelo antigo (folhas lidas antes do rename)
templates.env.globals["field_value"] = field_value


@app.exception_handler(HTTPException)
async def _html_post_errors(request: Request, exc: HTTPException):
    """Formulários do browser nunca aterram num documento JSON cru."""
    if request.method.upper() == "POST":
        detail = html.escape(str(exc.detail))
        return HTMLResponse(
            "<!doctype html><html lang='pt'><meta charset='utf-8'>"
            "<title>Kanban MES — erro</title><body>"
            f"<h1>Não foi possível concluir</h1><p>{detail}</p>"
            "<p><a href='javascript:history.back()'>Voltar</a></p></body></html>",
            status_code=exc.status_code,
        )
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                        headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def _html_post_validation_errors(request: Request,
                                       exc: RequestValidationError):
    """Os erros de parsing/fields obrigatórios dos forms também são HTML."""
    if request.method.upper() == "POST":
        return HTMLResponse(
            "<!doctype html><html lang='pt'><meta charset='utf-8'>"
            "<title>Kanban MES — erro</title><body>"
            "<h1>Não foi possível concluir</h1>"
            "<p>O formulário está incompleto ou contém um valor inválido.</p>"
            "<p><a href='javascript:history.back()'>Voltar</a></p></body></html>",
            status_code=422,
        )
    return JSONResponse({"detail": exc.errors()}, status_code=422)


@app.middleware("http")
async def _attach_watermark(request: Request, call_next):
    """Marca de água = folha mais recente no momento do pedido.

    A página do histórico guarda o valor do primeiro paint e compara-o com o
    header de cada poll HTMX; a diferença são as folhas que entraram entretanto,
    e é isso que alimenta o banner «N folhas novas».
    """
    # Estáticos e fotos não precisam de marca de água — e abrir uma ligação
    # SQLite (com executescript do schema) por cada imagem servida punha o
    # event loop a pagar até 5 s de busy_timeout em cada pedido.
    path = request.url.path
    if path.startswith("/static/") or path.endswith("/photo"):
        return await call_next(request)
    watermark = 0
    if request.method == "GET" and path in {"/", "/estado"}:
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

# De quanto em quanto tempo se pergunta ao Postgres se há plano novo. Baixo
# porque a pergunta é uma linha; o índice só se reconstrói se a resposta mudar.
_FRESHNESS_PROBE_SECONDS = 30
_index_cache: dict[str, tuple[float, object, str | None]] = {}
_index_lock = threading.Lock()


def _conn():
    return db.connect()


def get_index(loader_name: str):
    """Índice do plano em cache, revalidado contra o snapshot mais recente.

    O TTL sozinho era a forma errada de o fazer: reconstruía 64 mil linhas de
    dez em dez minutos mesmo sem nada ter mudado, e ainda assim demorava até
    dez minutos a ver um plano novo. A sonda é uma linha de SQL — reconstrói
    quando (e só quando) o snapshot muda.
    """
    now = time.monotonic()
    with _index_lock:
        hit = _index_cache.get(loader_name)
    if hit:
        checked_at, index, snapshot = hit
        if now - checked_at < _FRESHNESS_PROBE_SECONDS:
            return index
        current = _current_index_snapshot(loader_name)
        if current is None or current == snapshot:
            # sonda falhou (Postgres em baixo) ou nada mudou: continuar com o
            # que temos, e voltar a sondar daqui a pouco
            with _index_lock:
                _index_cache[loader_name] = (time.monotonic(), index, snapshot)
            return index
    index = getattr(loaders, loader_name)()
    # A fotografia que etiqueta o cache é a que foi efetivamente carregada.
    # Sondar o latest aqui abria uma corrida: carregar A, publicar B, etiquetar
    # o índice A como B e mantê-lo em cache sem nova invalidação.
    loaded_snapshot = getattr(index, "snapshot_id", None)
    with _index_lock:
        _index_cache[loader_name] = (
            time.monotonic(), index,
            (str(loaded_snapshot) if loaded_snapshot is not None
             else _current_index_snapshot(loader_name)),
        )
    return index


def _current_snapshot_id() -> str | None:
    try:
        return (loaders.plan_snapshot_info() or {}).get("snapshot_id")
    except Exception:
        return None


def _current_index_snapshot(loader_name: str) -> str | None:
    """Cada índice invalida-se pela SUA carga: os colaboradores chegam num
    snapshot próprio e ficavam presos ao snapshot do plano."""
    if loader_name == "load_employees":
        try:
            return loaders.employees_snapshot_id()
        except Exception:
            return None
    if loader_name == "load_nesting_index":
        try:
            return loaders.nesting_snapshot_id()
        except Exception:
            return None
    return _current_snapshot_id()


def get_employees():
    """Colaboradores em cache, invalidada pela sua própria carga."""
    return get_index("load_employees")


def make_scorer(template_name: str) -> Scorer:
    template = get_template(template_name)
    index = get_index(template.index_loader)
    active = loaders.load_active_ofs() if template.family == "cantoneiras" else set()
    return Scorer(index, CrossParams.load(), active_primary=active)


def make_fresh_scorer(template_name: str) -> Scorer:
    template = get_template(template_name)
    index = getattr(loaders, template.index_loader)()
    return Scorer(index, CrossParams.load())


_header_machine_cache: tuple[float, list] | None = None


def _load_header_machines() -> list:
    """Catálogo pequeno de máquinas, isolado do índice pesado do plano.

    Uma fonte em baixo devolve vazio: o checker marca ``no_reference`` e nunca
    inventa.
    """
    global _header_machine_cache
    now = time.monotonic()
    if _header_machine_cache and now - _header_machine_cache[0] < 60:
        return _header_machine_cache[1]
    try:
        values = list(loaders.load_machines())
    except Exception:
        values = []
    _header_machine_cache = (now, values)
    return values


def _source_document(sheet: dict) -> dict:
    """Proveniência do ficheiro, nunca uma referência para a data da folha."""
    filename, page = pg_store.source_from_image_path(sheet.get("image_path"))
    if not filename:
        return {}
    return {
        "filename": filename,
        "page": page,
        # derivado do nome do render, não guardado à parte — por isso marcado
        "inferred": True,
        "date_is_provenance_only": True,
    }


# Prefixo dd-mm-aaaa que o scanner da fábrica põe no nome dos PDFs
# («06-08-2026 - Rapid20T 2.pdf»).
_SOURCE_DATE_RE = re.compile(r"^(\d{2})-(\d{2})-(\d{4})")


def _assumed_sheet_date(sheet: dict) -> str | None:
    """Data assumida da folha (dd/mm/aaaa): dia útil anterior à data-base.

    Regra da fábrica (26/08): as folhas entregues à digitalização são sempre
    do dia útil anterior. A data-base é o prefixo dd-mm-aaaa do nome do PDF de
    origem; sem PDF (foto, folha manual), vale o `created_at` da folha.
    """
    filename, _page = pg_store.source_from_image_path(sheet.get("image_path"))
    base: datetime.date | None = None
    if filename:
        m = _SOURCE_DATE_RE.match(filename)
        if m:
            try:
                base = datetime.date(int(m.group(3)), int(m.group(2)),
                                     int(m.group(1)))
            except ValueError:
                base = None
    if base is None:
        try:
            base = datetime.datetime.fromisoformat(
                str(sheet.get("created_at") or "")).date()
        except ValueError:
            return None
    return header_cross.previous_business_day(base).strftime("%d/%m/%Y")


def _plan_header_machines(cross: dict, scorer: Scorer | None) -> list[str]:
    """Máquinas não vazias das linhas que ligaram fortemente ao plano."""
    if scorer is None:
        return []
    key_name = scorer.index.spec.key_field
    by_key = {
        str(entry.get(key_name)): entry
        for entry in scorer.index.entries
        if entry.get(key_name) is not None
    }
    out: list[str] = []
    for row in cross.get("rows", []):
        if row.get("mode") != "strong" or not row.get("matched_plan_key"):
            continue
        entry = by_key.get(str(row["matched_plan_key"])) or {}
        value = str(entry.get("maquina") or "").strip()
        if value and value not in out:
            out.append(value)
    return out


def resolve_operator(conn, uid: str, sheet: dict) -> dict | None:
    """Compara o operador da folha com a lista de colaboradores.

    Corre para TODAS as folhas, incluindo o verso (paragens): é lá que estão
    metade dos casos, e a mesma pessoa aparecia com nomes diferentes na frente
    e no verso da mesma folha física. É apenas diagnóstico: o formulário
    explícito é a única porta de escrita do cabeçalho.
    """
    del conn, uid
    header = (sheet["sheet_data"] or {}).get("header") or {}
    try:
        employees = get_employees()
    except Exception:
        return None
    if not employees:
        return None

    return asdict(operador.resolve(
        header.get("operador"), header.get("n_operador"), employees
    ))


_HISTORY_AUTO = object()


def run_cross_check(conn, uid: str, *, force_plan: bool = False, scorer_override: Scorer | None = None,
                    engine_override: str | None = None,
                    historical_context_override=_HISTORY_AUTO) -> bool:
    engine = engine_override or settings.cross_engine
    sheet = db.get_sheet(conn, uid)
    if not sheet or not sheet.get("sheet_data") or sheet["status"] in {"pending", "validated"}:
        return False
    if engine == "v3" and sheet["template_name"] == "cantoneiras_kanban":
        if force_plan and scorer_override is None:
            scorer_override = make_fresh_scorer(sheet["template_name"])

        return _run_cross_check_v3(
            conn, uid, scorer_override=scorer_override,
            historical_context_override=historical_context_override,
        )
    if engine not in {"legacy", "v3"}:
        raise ValueError(f"Motor cross desconhecido: {engine}")
    return _run_cross_check_legacy(conn, uid, force_plan=force_plan, scorer_override=scorer_override)


def _run_cross_check_v3(conn, uid: str, *, scorer_override=None,
                         historical_context_override=_HISTORY_AUTO) -> bool:
    from ..matching.evidence import IDENTITY_FIELDS, build_evidence
    from ..matching.history import load_history_context
    from ..matching.v3 import check_sheet_v3

    base = db.get_sheet(conn, uid)
    if not base or not base.get("sheet_data") or base["status"] == "validated":
        return False
    template = get_template(base["template_name"])
    evidence = build_evidence(base, db.evidence_edits(conn, base))
    data = copy.deepcopy(base["sheet_data"])
    rows = data.get("rows") or []
    observed_header = evidence.data.get("header") or {}
    source_document = _source_document(base)
    assumed_date = _assumed_sheet_date(base)
    scorer = None
    plan_reference = {"status": "not_applicable"}
    if template.index_loader:
        try:
            scorer = scorer_override or Scorer(get_index(template.index_loader), CrossParams.load())
            if not scorer.index.entries:
                scorer = None
        except Exception:
            scorer = None
        plan_reference = (
            {"status": "available", "snapshot_id": scorer.index.snapshot_id}
            if scorer else {"status": "no_reference", "message": "Plano indisponível; dados conservados."}
        )
    history = None
    if scorer is not None:
        if historical_context_override is _HISTORY_AUTO:
            sheet_date = observed_header.get("data") or assumed_date
            history = load_history_context(scorer.index, sheet_date)
        else:
            history = historical_context_override
        cross = check_sheet_v3(
            evidence.data, scorer.params, index=scorer.index,
            historical_context=history, explicit_bindings=evidence.explicit_bindings,
            provenance=evidence.provenance,
        )
    else:
        if template.index_loader:
            from ..matching.refs import PlanIndex
            cross = check_sheet_v3(evidence.data, index=PlanIndex([], loaders.CANTONEIRAS_SPEC))
        else:
            cross = {"rows": [], "summary": {}, "review_order": []}
    cross.update({
        "version": "cross-v3", "engine_version": "cross-v3",
        "evidence_fingerprint": evidence.provenance["fingerprint"],
        "provenance": evidence.provenance,
        "snapshot_id": scorer.index.snapshot_id if scorer else None,
        "plan_reference": plan_reference,
        "historical_context": history.to_dict() if history is not None else {"status": "unavailable"},
    })
    edits = []

    def apply_value(path, container, key, new, actor):
        old = container.get(key)
        # Identity projection uses the exact canonical candidate, including
        # empty values. Production values are never routed through here.
        if old != new:
            container[key] = new
            edits.append((path, old, new, actor))

    sources = evidence.provenance["field_sources"]
    for rc in cross.get("rows", []):
        i = rc["row_index"]
        if i >= len(rows):
            continue
        rc["selected_snapshot_id"] = cross["snapshot_id"]
        rc["replaced_values"] = {}
        if scorer is not None and rc.get("mode") in {"activity", "empty"}:
            # Undo an old engine's identity fill on non-production rows.
            # These observations have no eligible plan identity to project.
            for key in IDENTITY_FIELDS:
                observed = evidence.data["rows"][i].get(key)
                apply_value(f"rows[{i}].{key}", rows[i], key, observed, "cross:v3:observation")
        if is_marked(field_value(rows[i], "perf_comp")) or rc.get("binding_status") in {"stale", "reselected"}:
            if "_plan_binding" in rows[i]:
                old = rows[i].pop("_plan_binding")
                edits.append((f"rows[{i}]._plan_binding", old, None, "cross:v3"))
        for cell in rc.get("cells", []):
            key = cell["field"]
            if key not in IDENTITY_FIELDS or not cell.get("auto_write"):
                continue
            path = f"rows[{i}].{key}"
            new = cell.get("proposal")
            before = (evidence.data.get("rows") or [])[i].get(key)
            apply_value(path, rows[i], key, new, "cross:v3")
            if before != new:
                cell["applied"] = True
                cell["message"] = "Substituído automaticamente. " + cell.get("message", "")
                rc["replaced_values"][key] = {
                    "before": before, "after": new, **sources.get(path, {"source": "raw_extraction"}),
                }
            # Result metadata describes the observation and the applied
            # choice; it is not recomputed from the materialized values.
            cell["auto_write"] = False

    employees = None
    if any(str(observed_header.get(key) or "").strip() for key in ("operador", "n_operador")):
        try:
            employees = get_employees()
        except Exception:
            pass
    plan_machines = _plan_header_machines(cross, scorer)
    machines = _load_header_machines() if (
        observed_header.get("setor_maquina") or plan_machines or header_cross.template_machine(template)
    ) else []
    human_header = {
        path.split(".", 1)[1] for path, source in sources.items()
        if path.startswith("header.") and source["source"] == "human"
    }
    header_result = header_cross.check_header(
        observed_header, template, human_fields=human_header, employees=employees,
        machines=machines, plan_machines=plan_machines, source_document=source_document,
        assumed_date=assumed_date,
    )
    header = data.setdefault("header", {})
    for key, cell in header_result["cells"].items():
        if cell.get("auto_write") and cell.get("proposal") is not None:
            new = str(cell["proposal"])
            apply_value(f"header.{key}", header, key, new, cell.get("actor") or "cross:header")
            cell["applied"] = observed_header.get(key) != new
            cell["auto_write"] = False
    # Header dependency is downstream only; it never reopens row inference.
    final_header = header_cross.check_header(
        header, template, human_fields=human_header, employees=employees,
        machines=machines, plan_machines=plan_machines, source_document=source_document,
        assumed_date=assumed_date,
    )
    for key, cell in final_header["cells"].items():
        observed_cell = header_result["cells"].get(key) or {}
        if observed_cell.get("applied"):
            cell["applied"] = True
            cell["message"] = "Substituído automaticamente. " + cell.get("message", "")
        cell["observed_written"] = observed_header.get(key)
    cross["header"] = {"cells": final_header["cells"], "source_document": final_header["source_document"]}
    cross["operator"] = final_header["operator"]
    expected = base["revision"]
    cross["data_revision"] = cross["materialized_revision"] = expected + bool(edits)
    if edits:
        return db.apply_cross_corrections(conn, uid, data, cross, expected, edits)
    return db.save_cross_check(conn, uid, cross, expected_revision=expected)


def _run_cross_check_legacy(conn, uid: str, *, force_plan: bool = False, scorer_override: Scorer | None = None) -> bool:
    sheet = db.get_sheet(conn, uid)
    if not sheet or not sheet["sheet_data"]:
        return False
    # A identidade do operador é calculada como proveniência/diagnóstico; não
    # altera o cabeçalho confirmado no formulário.
    operator_match = resolve_operator(conn, uid, sheet)
    # Reler antes de calcular: o resultado só será gravado por CAS sobre esta
    # revisão conhecida.
    base = db.get_sheet(conn, uid)
    if not base or not base["sheet_data"]:
        return False
    template = get_template(base["template_name"])
    data = base["sheet_data"]
    rows = data.get("rows") or []
    header = data.get("header") or {}
    human_header = db.human_header_fields(conn, uid)
    expected = base["revision"]

    scorer: Scorer | None = None
    if template.index_loader is None:
        # ex.: paragens — não há plano contra que cruzar, mas o cabeçalho
        # (operador, máquina, data, turno) verifica-se na mesma.
        plan_reference = {"status": "not_applicable"}
    else:
        try:
            if force_plan:
                with _index_lock:
                    _index_cache.pop(template.index_loader, None)
            scorer = scorer_override or make_scorer(base["template_name"])
            plan_reference = {"status": "available"}
        except Exception:
            # O plano é uma fonte independente. Uma indisponibilidade não pode
            # impedir data/turno, colaboradores ou máquina de serem cruzados
            # e persistidos.
            plan_reference = {
                "status": "no_reference",
                "message": "Plano indisponível; linhas mantidas sem cruzamento.",
            }
    if scorer is not None:
        # Edições anteriores de identidade são evidência de auditoria, não um
        # veto sobre campos que pertencem ao planeamento. Também não desligam
        # a herança que o cross precisa de resolver.
        cross = check_sheet(rows, scorer, {}, footer=data.get("footer"))
        plan_reference["snapshot_id"] = scorer.index.snapshot_id
    else:
        cross = {"summary": {}, "review_order": [], "rows": []}

    # Cabeçalho determinístico: cada fonte só entra se puder ser precisa —
    # falhas viram ``no_reference``, nunca palpites.
    employees = None
    if any(str(header.get(f) or "").strip() for f in ("operador", "n_operador")):
        try:
            employees = get_employees()
        except Exception:
            employees = None
    plan_machines = _plan_header_machines(cross, scorer)
    machine_catalog: list = []
    if (str(header.get("setor_maquina") or "").strip() or plan_machines
            or header_cross.template_machine(template)):
        machine_catalog = _load_header_machines()
    source_document = _source_document(base)
    # A data assumida também vale para o verso (paragens): é a mesma folha
    # física, digitalizada no mesmo dia.
    assumed_date = _assumed_sheet_date(base)

    def check_current_header() -> dict:
        return header_cross.check_header(
            header, template,
            human_fields=human_header,
            employees=employees,
            machines=machine_catalog,
            plan_machines=plan_machines,
            source_document=source_document,
            assumed_date=assumed_date,
        )

    edits: list[tuple[str, object, object, str]] = []

    # Materializar propostas únicas do cabeçalho até estabilizar. A última
    # passagem descreve o valor final, por isso a UI mostra a célula verde; o
    # primeiro valor OCR continua preservado no trilho old→new.
    header_originals: dict[str, object] = {}
    header_actors: dict[str, str] = {}
    applied_header_fields: set[str] = set()
    for _header_iteration in range(8):
        header_result = check_current_header()
        header_changed = False
        for field_name, cell in header_result["cells"].items():
            proposal = cell.get("proposal")
            if not cell.get("auto_write") or proposal is None:
                continue
            old = header.get(field_name)
            new = str(proposal).strip() or None
            if (str(old or "").strip() or None) == new:
                continue
            header_originals.setdefault(field_name, old)
            header_actors[field_name] = cell.get("actor") or "cross:header"
            header[field_name] = new
            applied_header_fields.add(field_name)
            header_changed = True
        if not header_changed:
            break
    else:
        raise RuntimeError("Cross do cabeçalho não estabilizou")

    for field_name in applied_header_fields:
        edits.append((
            f"header.{field_name}", header_originals[field_name],
            header.get(field_name), header_actors[field_name],
        ))
        cell = header_result["cells"][field_name]
        cell["applied"] = True
        cell["message"] = (
            "Substituído automaticamente. " + cell.get("message", "")
        ).strip()

    # Fixed point: materializar pode mudar o melhor candidato. Repetimos o
    # cálculo sobre os valores finais até estabilizar, mantendo um único old→new
    # por célula no trilho de auditoria e um guarda de ciclo/iterações.
    applied_cells: set[tuple[int, str]] = set()
    original_values: dict[tuple[int, str], object] = {}
    fixed_point = True
    seen_states: set[str] = set()
    if scorer is not None:
        for _iteration in range(8):
            signature = json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str)
            if signature in seen_states:
                fixed_point = False
                break
            seen_states.add(signature)
            changed = False
            for rc in cross.get("rows", []):
                for cell in rc.get("cells", []):
                    if not cell.get("auto_write") or cell.get("proposal") is None:
                        continue
                    i, f = rc["row_index"], cell["field"]
                    if i >= len(rows):
                        continue
                    proposal = cell["proposal"]
                    if str(rows[i].get(f) or "").strip() == str(proposal).strip():
                        continue
                    key = (i, f)
                    original_values.setdefault(key, rows[i].get(f))
                    rows[i][f] = proposal if str(proposal).strip() else None
                    applied_cells.add(key)
                    changed = True
            if not changed:
                break
            cross = check_sheet(rows, scorer, {}, footer=data.get("footer"))
        else:
            fixed_point = False

        final_by_row = {r["row_index"]: r for r in cross.get("rows", [])}
        for i, f in applied_cells:
            edits.append((f"rows[{i}].{f}", original_values[(i, f)],
                          rows[i].get(f), "cross"))
            final_cell = next(
                (cell for cell in final_by_row.get(i, {}).get("cells", [])
                 if cell["field"] == f), None
            )
            if final_cell is not None:
                final_cell["applied"] = True
                final_cell["message"] = (
                    "Substituído automaticamente. "
                    "O valor lido pelo OCR fica no histórico de auditoria."
                )
        cross["fixed_point"] = fixed_point

    cross["header"] = {
        "cells": header_result["cells"],
        "source_document": header_result["source_document"],
    }
    cross["plan_reference"] = plan_reference
    final_operator = header_result.get("operator") or operator_match
    if final_operator:
        cross["operator"] = final_operator

    # Não apagar a proveniência numa revalidação sobre valores já canónicos.
    # O payload final conserva o primeiro valor substituído e o último valor
    # materializado durante toda a revisão da folha.
    replaced_by_row: dict[int, dict[str, dict[str, object]]] = {
        int(row.get("row_index")): {
            field: dict(change)
            for field, change in (row.get("replaced_values") or {}).items()
            if isinstance(change, dict)
        }
        for row in ((base.get("cross_check") or {}).get("rows") or [])
        if row.get("row_index") is not None
    }
    for path, old, new, _actor in edits:
        match = re.match(r"^rows\[(\d+)]\.([A-Za-z_][A-Za-z0-9_]*)$", path)
        if match:
            row_changes = replaced_by_row.setdefault(int(match.group(1)), {})
            previous = row_changes.get(match.group(2)) or {}
            row_changes[match.group(2)] = {
                "old": previous.get("old", old), "new": new,
            }
    for row_cross in cross.get("rows", []):
        row_cross["replaced_values"] = replaced_by_row.get(
            row_cross.get("row_index"), {}
        )
    # A revisão que estes metadados descrevem. Validação recusa um cross velho
    # se uma edição entrar entre o cálculo e a reserva SQLite.
    cross["data_revision"] = expected + (1 if edits else 0)

    if edits:
        # Se o humano editou entre o cálculo e a gravação, o CAS recusa e
        # desiste-se — a edição dele dispara um run_cross_check novo.
        return db.apply_cross_corrections(
            conn, uid, data, cross, expected, edits
        )
    return db.save_cross_check(conn, uid, cross, expected_revision=expected)


# ---------- páginas ----------

@app.get("/", response_class=HTMLResponse)
def home(request: Request, status: str = "", operador: str = "", setor: str = "",
         data: str = "", data_captura: str = "", of: str = "",
         created: str = "", deleted: str = "", validated: str = "",
         stored: str = "", page: int = 1):
    status = status if status in {"", "pending", "validated", "error"} else ""
    page = max(1, page)
    conn = _conn()
    try:
        all_sheets = db.list_sheets(
            conn, status=status or None, operador=operador or None,
            setor=setor or None, data_folha=data or None,
            data_captura=data_captura or None, of=of or None,
        )
        options = db.filter_options(conn)
    finally:
        conn.close()
    page_size = 100
    total = len(all_sheets)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    sheets = all_sheets[(page - 1) * page_size:page * page_size]
    filters = {
        "status": status, "operador": operador, "setor": setor,
        "data": data, "data_captura": data_captura, "of": of,
    }
    history_url = _history_location(page=page, **filters)
    return templates.TemplateResponse(request, "home.html", {
        "sheets": sheets, "options": options,
        "f": filters,
        "history_url": history_url,
        "status_urls": {
            value: _history_location(page=1, **(filters | {"status": value}))
            for value in ("", "pending", "validated", "error")
        },
        "clear_url": _history_location(status=status),
        "pagination": {
            "page": page, "pages": pages, "total": total,
            "prev": _history_location(page=page - 1, **filters) if page > 1 else None,
            "next": _history_location(page=page + 1, **filters) if page < pages else None,
        },
        "created": created, "deleted": deleted,
        "validated": validated, "stored": stored,
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
def estado_page(request: Request, q: str = "", familia: str = "", of: str = "",
                validated: str = "", stored: str = "", page: int = 1):
    conn = _conn()
    try:
        sheets = db.list_sheets(conn)
    finally:
        conn.close()
    by_status: dict[str, int] = {}
    for s in sheets:
        by_status[s["status"]] = by_status.get(s["status"], 0) + 1
    data_estado = estado_data.load_estado(q, familia, of)
    page_size = 100
    page = max(1, page)
    total = len(data_estado["rows"])
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    data_estado["rows"] = data_estado["rows"][(page - 1) * page_size:page * page_size]
    estado_back = _estado_location(q=q, familia=familia, of=of, page=page)
    return templates.TemplateResponse(request, "estado.html", {
        "estado": data_estado,
        "q": q, "familia": familia, "of": of,
        "validated": validated, "stored": stored,
        "by_status": by_status, "n_sheets": len(sheets),
        "estado_back": estado_back,
        "estado_close_url": _estado_location(q=q, familia=familia, page=page),
        "estado_pdf_url": _query_location("/estado/pdf", q=q, familia=familia),
        "pagination": {
            "page": page, "pages": pages, "total": total,
            "prev": _estado_location(q=q, familia=familia, page=page - 1) if page > 1 else None,
            "next": _estado_location(q=q, familia=familia, page=page + 1) if page < pages else None,
        },
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


def _process_sheet(uid: str, force_ocr: bool = False) -> None:
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
        # Verso em branco do scanner: sem tinta não há nada para ler, e mandar
        # uma página vazia ao modelo produzia folhas inventadas inteiras.
        # `force_ocr` é o revisor a discordar da deteção — respeita-se.
        if not force_ocr and imaging.is_blank_page(image_path, settings.blank_ink_threshold):
            extraction = empty_extraction(template)
            extraction["_blank_page"] = True
            if db.set_extraction(conn, uid, extraction):
                run_cross_check(conn, uid)
            return
        # Folha TPL102 tem frente (produção) e verso (paragens). O provider
        # classifica E transcreve na MESMA chamada — eram duas por página, e a
        # classificação sozinha gastava metade da quota do free tier.
        try:
            if template.family == "cantoneiras" and hasattr(provider, "extract_auto"):
                kinds = {"producao": get_template("cantoneiras_kanban"),
                         "paragens": get_template("cantoneiras_paragens")}
                kind, extraction = provider.extract_auto(image_path, kinds)
                if kinds[kind].name != template_name:
                    # a reclassificação grava-se junto com a transcrição, na
                    # mesma escrita atómica (ver db.set_extraction)
                    template_name = kinds[kind].name
                    template = kinds[kind]
            else:
                extraction = provider.extract(image_path, template)
            # Ponto comum da cadeia (Qwen/Gemini/Claude): se a leitura veio
            # sem identificação no cabeçalho, uma segunda chamada focada na
            # faixa superior tenta recuperá-la. Nunca pisa o que foi lido.
            extraction = rescue_header(provider, image_path, template, extraction)
        except OcrError as exc:
            # OCR falhou (rede, quota, chave): a folha abre vazia para
            # preenchimento manual; o erro fica no trilho de auditoria
            extraction = empty_extraction(template)
            extraction["_ocr_error"] = str(exc)
        if not db.set_extraction(conn, uid, extraction, template_name=template_name):
            return  # o revisor começou a editar entretanto: o trabalho dele manda
        run_cross_check(conn, uid)
    except Exception as exc:  # nunca matar o worker do lote por causa de uma folha
        print(f"[worker] folha {uid}: {exc}", flush=True)
        try:
            err_conn = db.connect()
            try:
                # sem isto a folha ficava `pending` para sempre: spinner
                # eterno na página e nenhum botão para a recuperar
                db.set_error(err_conn, uid, str(exc))
            finally:
                err_conn.close()
        except Exception:
            pass
    finally:
        conn.close()


# Entre folhas: 1 chamada/folha, ~10 RPM — o limite do free tier do primário.
_BATCH_SLEEP_S = 6.0
# Antes da segunda passagem: tempo para um pico de 503 («high demand») passar.
_RETRY_DELAY_S = 60.0
# Só se re-tenta o que recupera sozinho: 5xx e rede. Quota (429) não — essa
# volta pelo botão «Tentar OCR outra vez» ou pelo fallback pago, se existir.
_TRANSIENT_MARKERS = ("HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504", "indisponível")


def _transient_failures(uids: list[str]) -> list[str]:
    """Folhas do lote cujo OCR falhou por causa passageira e ninguém tocou."""
    out: list[str] = []
    conn = db.connect()
    try:
        for uid in uids:
            sheet = db.get_sheet(conn, uid)
            # `in_review` também conta: as escritas do próprio motor (ex.: a
            # data assumida) mudam o estado sem nenhum humano ter tocado — o
            # guarda contra pisar trabalho humano é o edit_count, logo abaixo.
            if not sheet or sheet["status"] not in ("extracted", "in_review"):
                continue                      # pendente/validada/erro: não mexer
            if db.edit_count(conn, uid):
                continue                      # já há trabalho humano em cima
            err = (sheet.get("raw_extraction") or {}).get("_ocr_error") or ""
            if any(m in err for m in _TRANSIENT_MARKERS):
                out.append(uid)
    finally:
        conn.close()
    return out


def _process_batch(uids: list[str]) -> None:
    for i, uid in enumerate(uids):
        _process_sheet(uid)
        if i < len(uids) - 1:
            time.sleep(_BATCH_SLEEP_S)
    # Segunda passagem única pelas falhas passageiras: um pico de 503 a meio de
    # um lote de 26 páginas não deve deixar folhas mortas à espera de cliques.
    retry = _transient_failures(uids)
    if not retry:
        return
    time.sleep(_RETRY_DELAY_S)
    conn = db.connect()
    try:
        for uid in retry:
            db.mark_pending(conn, uid)
    finally:
        conn.close()
    for i, uid in enumerate(retry):
        _process_sheet(uid)
        if i < len(retry) - 1:
            time.sleep(_BATCH_SLEEP_S)


@app.post("/upload")
def upload(template_name: str = Form(...), photos: list[UploadFile] = []):
    # Rota síncrona de propósito: corre no threadpool. Como `async def`, o
    # render do PDF (26 páginas × pypdfium) bloqueava o event loop dezenas de
    # segundos e a app inteira deixava de responder durante um upload.
    try:
        template = get_template(template_name)
    except KeyError:
        raise HTTPException(422, f"Template desconhecido: {template_name}")
    images: list[tuple[bytes, str]] = []
    for up in photos:
        if not up.filename:
            continue
        content = up.file.read()
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


# PDFs de kanban que o scanner da fábrica põe no Drive: «06-08-2026 - Rapid20T 1.pdf»,
# «18-08-2026.PDF»… O padrão vem da config (MES_KANBAN_PDF_RE): o scanner
# mudou a convenção de nomes a 14-08 e o padrão antigo, preso a «rapid»,
# deixava os lotes novos por ingerir.
_KANBAN_PDF_RE = re.compile(settings.kanban_pdf_re, re.IGNORECASE)

# caminhos de célula aceites no /edit; nomes de campo só minúsculas/underscore
_FIELD_PATH_RE = re.compile(
    r"^(?:rows\[(?P<idx>\d{1,4})\]\.(?P<rfield>[a-z_]{1,40})"
    r"|(?P<section>header|footer)\.(?P<sfield>[a-z_]{1,40}))$"
)
_MAX_ROWS = 200   # nenhuma folha física tem 200 linhas


@app.post("/ingest/drive")
def ingest_drive(request: Request):
    """Ingestão dos PDFs de kanban que o sync do Drive deixou no servidor.

    Fecha o ciclo scanner → Drive → app: até aqui o sync trazia os PDFs para a
    máquina e ficavam à espera de um upload manual (4 lotes chegaram a
    acumular-se). Idempotente a dois níveis: sha256 do PDF (versão nova do
    mesmo ficheiro reprocessa) e sha256 de cada página (o que já entrou — por
    upload manual, por exemplo — nunca duplica). O OCR segue em background,
    como no upload manual.
    """
    if settings.admin_token and request.headers.get("X-Admin-Token") != settings.admin_token:
        raise HTTPException(403, "X-Admin-Token inválido.")
    drive = settings.drive_dir
    if not drive.is_dir():
        raise HTTPException(503, f"Pasta do Drive não encontrada: {drive}")

    pdfs = sorted(p for p in drive.iterdir()
                  if p.is_file() and _KANBAN_PDF_RE.match(p.name))
    report = {
        "pdfs_novos": 0,
        "folhas_criadas": 0,
        "paginas_repetidas": 0,
        "pdfs_vistos": len(pdfs),
        "pdfs_falhados": 0,
        "falhas": [],
    }
    uids: list[str] = []
    conn = _conn()
    try:
        done = db.ingested_shas(conn)
        for pdf in pdfs:
            try:
                content = pdf.read_bytes()
                pdf_sha = hashlib.sha256(content).hexdigest()
                if pdf_sha in done:
                    continue
                pages = _pdf_to_images(content, pdf.stem)
                if not pages:
                    raise ValueError("PDF sem páginas")
            except Exception as exc:
                # PDF estragado não pode encravar o ciclo diário inteiro
                print(f"[ingest] {pdf.name}: PDF ilegível ({exc})", flush=True)
                report["pdfs_falhados"] += 1
                report["falhas"].append({
                    "ficheiro": pdf.name,
                    "tipo": "pdf_ilegivel",
                    "erro": type(exc).__name__,
                    "detalhe": str(exc)[:500],
                })
                continue
            report["pdfs_novos"] += 1
            for page_bytes, page_name in pages:
                page_sha = hashlib.sha256(page_bytes).hexdigest()
                if db.image_sha_exists(conn, page_sha):
                    report["paginas_repetidas"] += 1
                    continue
                image_path, sha = _save_image(page_bytes, page_name)
                uids.append(db.create_sheet(conn, "cantoneiras_kanban", image_path, sha))
                report["folhas_criadas"] += 1
            db.record_ingested(conn, pdf.name, pdf_sha, len(pages))
    finally:
        conn.close()

    if uids:
        if PROCESS_IN_BACKGROUND:
            threading.Thread(target=_process_batch, args=(uids,), daemon=True).start()
        else:  # testes: determinístico
            _process_batch(uids)
    if report["falhas"]:
        # Os PDFs válidos foram processados, mas o chamador tem de receber um
        # estado de falha para não dar o ciclo diário como concluído.
        return JSONResponse(status_code=422, content=report)
    return report


@app.post("/sheet/{uid}/reocr")
def sheet_reocr(uid: str, force: int = 0, back: str = Form("")):
    """Re-ler a foto com OCR (ex.: depois de um 429 de quota).

    `?force=1` salta a deteção de página em branco — é o revisor a dizer que a
    página tem mesmo conteúdo, e a palavra dele vale mais que a heurística."""
    conn = _conn()
    try:
        sheet = db.get_sheet(conn, uid)
        if not sheet:
            raise HTTPException(404)
        if sheet["status"] == "validated":
            raise HTTPException(409, "Folha validada é imutável.")
        if not sheet.get("image_path"):
            raise HTTPException(422, "Folha sem foto — não há nada para reler.")
        if not force and db.edit_count(conn, uid):
            # já há trabalho humano em cima: re-ler substituía-o todo pela
            # transcrição nova; exige-se a intenção explícita (?force=1)
            raise HTTPException(
                409, "Esta folha já tem correções manuais — re-ler o OCR "
                     "substituía-as. Usa «Forçar OCR» se for mesmo isso que queres.")
        db.mark_pending(conn, uid)
    finally:
        conn.close()
    if PROCESS_IN_BACKGROUND:
        threading.Thread(target=_process_sheet, args=(uid, bool(force)), daemon=True).start()
    else:
        _process_sheet(uid, bool(force))
    return RedirectResponse(_sheet_location(uid, back), status_code=303)


@app.post("/sheet/{uid}/delete")
def sheet_delete(uid: str, back: str = Form("")):
    conn = _conn()
    try:
        try:
            image_path = db.delete_sheet(conn, uid)
        except KeyError:
            raise HTTPException(404)
        except PermissionError:
            raise HTTPException(409, "Folha validada é imutável — não se apaga.")
        # O ficheiro pode ser partilhado (frente/verso, uploads repetidos):
        # só se apaga quando NENHUMA outra folha ainda aponta para ele.
        shared = bool(image_path) and db.image_path_in_use(conn, image_path)
    finally:
        conn.close()
    if image_path and not shared:
        p = Path(image_path).resolve()
        if p.is_relative_to(settings.images_dir.resolve()) and p.is_file():
            imaging.clear_renders(p)   # os .rotN.png derivados vão junto
            p.unlink()
    return RedirectResponse(_with_query(_safe_back(back) or "/", deleted=1), status_code=303)


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
    visible_no = 0
    for row in data.get("rows") or []:
        if row.get("_deleted") is True:
            continue
        if not any(not str(k).startswith("_")
                   and v is not None and str(v).strip()
                   for k, v in row.items()):
            continue
        visible_no += 1
        w.writerow([
            sheet.get("sheet_no") or sheet["uid"][:8], sheet["status"], header.get("operador"),
            header.get("data"), header.get("setor_maquina"), visible_no,
        ] + [row.get(f) for f in template.row_fields])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition":
            f'attachment; filename="kanban_{sheet.get("sheet_no") or uid[:8]}.csv"',
    })


def _export_response(request: Request, kind: str, de="", ate="", operador="", validadas=0):
    from . import export_routes, export_source
    try:
        sheets = export_routes.export_sheets(_conn, de, ate, operador,
                                             drafts=kind == "cpis" and not validadas)
        if kind == "technical":
            content = export_routes.technical_workbook(sheets)
            filename = "producao_mes.xlsx"
        else:
            content = export_routes.workbook(kind, sheets)
            filename = (cpis_export.basedados_filename_for(de or None, ate or None)
                        if kind == "basedados" else cpis_export.cpis_filename_for(de or None, ate or None, bool(validadas)))
    except export_source.IncompleteExport as exc:
        return templates.TemplateResponse(request, "export_error.html",
            {"message": str(exc), "problems": exc.problems}, status_code=422)
    except pg_store.InvalidSheetDate:
        return templates.TemplateResponse(request, "export_error.html",
            {"message": "O período indicado não tem datas válidas.", "problems": []}, status_code=422)
    except Exception:
        traceback.print_exc()
        return templates.TemplateResponse(request, "export_error.html",
            {"message": "Não foi possível ler o histórico validado. Tenta exportar novamente.", "problems": []}, status_code=503)
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/export/cpis")
def export_cpis(request: Request, de: str = "", ate: str = "", operador: str = "", validadas: int = 0):
    return _export_response(request, "cpis", de, ate, operador, validadas)


@app.get("/export/basedados")
def export_basedados(request: Request, de: str = "", ate: str = "", operador: str = "", validadas: int = 0):
    return _export_response(request, "basedados", de, ate, operador, 1)


@app.get("/export.xlsx")
def export_xlsx(request: Request):
    return _export_response(request, "technical", validadas=1)


def _safe_back(back: str | None) -> str | None:
    """Só aceita caminhos internos — impede que um ?back= leve para fora do site.
    O `\\` conta como `//`: os browsers normalizam `/\\evil.com` para
    `//evil.com` e o filtro de prefixo deixava-o passar."""
    if (not back or not back.startswith("/") or back.startswith("//")
            or "\\" in back or any(ord(ch) < 32 for ch in back)):
        return None
    return back


def _safe_history_back(back: str | None) -> str | None:
    safe = _safe_back(back)
    if not safe or urlsplit(safe).path != "/":
        return None
    return safe


def _sheet_location(uid: str, back: str | None = None, **query: object) -> str:
    params = [(key, str(value)) for key, value in query.items() if value is not None]
    safe_back = _safe_back(back)
    if safe_back:
        params.append(("back", safe_back))
    return f"/sheet/{uid}" + (f"?{urlencode(params)}" if params else "")


def _query_location(path: str, **values: object) -> str:
    params = [(key, str(value)) for key, value in values.items()
              if value not in (None, "")]
    return path + (f"?{urlencode(params)}" if params else "")


def _history_location(*, status: str = "", operador: str = "", setor: str = "",
                      data: str = "", data_captura: str = "", of: str = "",
                      page: int = 1) -> str:
    return _query_location(
        "/", status=status, operador=operador, setor=setor, data=data,
        data_captura=data_captura, of=of, page=page if page > 1 else None,
    )


def _estado_location(*, q: str = "", familia: str = "", of: str = "",
                     page: int = 1) -> str:
    return _query_location(
        "/estado", q=q, familia=familia, of=of,
        page=page if page > 1 else None,
    )


def _with_query(location: str, **values: object) -> str:
    parts = urlsplit(location)
    additions = {key: str(value) for key, value in values.items()
                 if value is not None}
    # Preserva byte a byte filtros, ordem e encoding do `back` original. Só os
    # parâmetros transitórios que estamos a acrescentar são substituídos.
    untouched = []
    for raw_pair in parts.query.split("&") if parts.query else []:
        raw_key = raw_pair.partition("=")[0]
        if unquote_plus(raw_key) not in additions:
            untouched.append(raw_pair)
    suffix = urlencode(additions)
    query = "&".join([*untouched, *([suffix] if suffix else [])])
    return urlunsplit(("", "", parts.path, query, parts.fragment))


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
            if str(f).startswith("_"):
                continue
            diff(f"rows[{i}].{f}", r.get(f), c.get(f))
    return out


def _with_field_draft(sheet: dict, field_path: str,
                      value: object) -> dict:
    """Sobrepõe só a célula submetida numa cópia usada para renderizar.

    Num conflito de revisão, a folha mais recente continua a ser a fonte de
    todos os restantes campos. Assim o operador vê o valor que escreveu e
    pode confirmá-lo novamente sem esmagar alterações concorrentes.
    """
    draft = copy.deepcopy(sheet)
    data = draft.get("sheet_data") or {"header": {}, "rows": [], "footer": {}}
    draft["sheet_data"] = data
    match = _FIELD_PATH_RE.match(field_path)
    if not match:  # o chamador já validou; guarda defensiva para uso futuro
        return draft
    if match.group("idx") is not None:
        row_index = int(match.group("idx"))
        rows = data.setdefault("rows", [])
        while len(rows) <= row_index:
            rows.append({})
        rows[row_index][match.group("rfield")] = value
    else:
        data.setdefault(match.group("section"), {})[match.group("sfield")] = value
    return draft


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
    return _render_sheet(request, sheet, back=back, view=view)


def _render_sheet(request: Request, sheet: dict, *, back: str | None = None,
                  view: str | None = None, status_code: int = 200,
                  header_draft: dict | None = None,
                  field_draft: tuple[str, object] | None = None,
                  erro: str | None = None, focus: str | None = None,
                  error_context: str | None = None):
    """Render único da folha, incluindo conflitos que preservam o formulário."""
    if header_draft is not None:
        current = sheet.get("sheet_data") or {}
        sheet = {
            **sheet,
            "sheet_data": {**current, "header": dict(header_draft)},
        }
    if field_draft is not None:
        sheet = _with_field_draft(sheet, *field_draft)
    template = get_template(sheet["template_name"])
    stored_cross = sheet.get("cross_check") or {}
    if (sheet["status"] != "validated" and stored_cross.get("engine") == "cross-v3"
            and stored_cross.get("data_revision") != sheet["revision"]):
        sheet = {**sheet, "cross_check": None}
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
            r["row_index"]: {**r, "cells_by_field": {c["field"]: c for c in r.get("cells", [])}}
            for r in sheet["cross_check"]["rows"]
        }
    diverged = _diverged_map(sheet) if view_mode == "final" else {}
    cross = sheet["cross_check"] or {}
    header_cross_data = cross.get("header") or {}
    header_cells = header_cross_data.get("cells") or {}
    source_document = (
        header_cross_data.get("source_document") or _source_document(sheet)
    )
    return templates.TemplateResponse(request, "sheet.html", {
        "sheet": sheet, "t": template, "cross_rows": cross_rows,
        "summary": cross.get("summary"),
        "review_order": cross.get("review_order", []),
        "plan_reference": cross.get("plan_reference") or {},
        "stored": request.query_params.get("stored"),
        "erro": erro if erro is not None else request.query_params.get("erro"),
        "header_conflict": header_draft is not None,
        "error_context": (error_context
                          or request.query_params.get("erro_context")),
        "focus": focus if focus is not None else request.query_params.get("focus"),
        "has_ocr": has_ocr, "view_mode": view_mode,
        "operator": cross.get("operator"),
        "header_cells": header_cells,
        "header_labels": header_cross.HEADER_LABELS,
        "source_document": source_document,
        "diverged": diverged, "n_diverged": len(diverged),
        "back_url": _safe_back(back) or "/",
    }, status_code=status_code)


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


def _totais_plano(linhas: list) -> dict:
    """Somas de planeada/feita/falta das linhas do pop-up.

    Cada soma só existe se houver pelo menos um valor na coluna — somar zeros
    de colunas vazias mostraria «0» onde a verdade é «não se sabe».
    """
    totais: dict = {"planeada": None, "feita": None, "falta": None,
                    "excesso": None,
                    "parcial": False}
    colunas = {"planeada": "quantity_planned", "feita": "quantity_made",
               "falta": "remaining_quantity", "excesso": "excesso"}
    for chave, col in colunas.items():
        valores = [l[col] for l in linhas if l.get(col) is not None]
        if valores:
            totais[chave] = float(sum(valores))
    return totais


@app.get("/sheet/{uid}/plano/{row_index}", response_class=HTMLResponse)
def sheet_plano_perfil(request: Request, uid: str, row_index: int, origem: str = ""):
    from . import plan_review
    conn = _conn()
    try:
        sheet = db.get_sheet(conn, uid)
    finally:
        conn.close()
    if not sheet:
        raise HTTPException(404)
    rows = (sheet.get("sheet_data") or {}).get("rows") or []
    if not 0 <= row_index < len(rows) or rows[row_index].get("_deleted") is True:
        raise HTTPException(404)
    try:
        ctx = plan_review.context(sheet, row_index, get_template(sheet["template_name"]),
                                  _safe_back(request.query_params.get("back")) or "/")
    except Exception:
        return templates.TemplateResponse(request, "_plan_error.html",
            {"message": "Não foi possível consultar estas referências. Confirma o planeamento e tenta novamente."}, status_code=503)
    return templates.TemplateResponse(request, "_plano_perfil.html", ctx)


@app.post("/sheet/{uid}/rows/{row_index}/reference")
def sheet_reference(
    uid: str,
    row_index: int,
    snapshot_id: str = Form(...),
    plan_key: str = Form(...),
    revision: int = Form(...),
    actor: str = Form("operador"),
    back: str = Form(""),
):
    """Escolha explícita e autenticada de uma referência do snapshot atual."""
    conn = _conn()
    saved = False
    try:
        sheet = db.get_sheet(conn, uid)
        if not sheet:
            raise HTTPException(404)
        if sheet["status"] == "validated":
            raise HTTPException(409, "Folha validada é imutável.")
        if sheet["template_name"] != "cantoneiras_kanban":
            raise HTTPException(422, "Esta folha não permite escolher referências.")
        rows = (sheet.get("sheet_data") or {}).get("rows") or []
        if row_index < 0 or row_index >= len(rows):
            raise HTTPException(404)
        if rows[row_index].get("_deleted") is True:
            raise HTTPException(404)
        if is_marked(field_value(rows[row_index], "perf_comp")):
            raise HTTPException(
                422, "Perfil completo representa todas as referências; não permite escolher uma."
            )
        if sheet["revision"] != revision:
            raise HTTPException(409, "A folha foi alterada; reabre Referências.")
        try:
            current_info = loaders.plan_snapshot_info() or {}
            current_snapshot = current_info.get("snapshot_id")
            with _index_lock:
                _index_cache.pop("load_cantoneiras_index", None)
            index = get_index("load_cantoneiras_index")
        except Exception as exc:
            raise HTTPException(
                422, "Planeamento indisponível; tenta novamente mais tarde."
            ) from exc
        if (not current_snapshot or not index.snapshot_id
                or str(index.snapshot_id) != str(current_snapshot)):
            raise HTTPException(
                409, "O planeamento mudou; reabre Referências e confirma novamente."
            )
        if not current_snapshot or snapshot_id != str(current_snapshot):
            raise HTTPException(
                409, "O planeamento mudou; reabre Referências e confirma novamente."
            )
        matches = [
            entry for entry in index.entries
            if str(entry.get(index.spec.key_field)) == plan_key
        ]
        if len(matches) != 1:
            raise HTTPException(422, "Referência inexistente ou adulterada.")
        entry = matches[0]
        content = tuple(
            f.name for f in index.spec.identity_fields
            if f.name not in carryover.CARRY_FIELDS
        )
        identities = carryover.resolve(rows, content, {})
        effective = carryover.effective_row(rows[row_index], identities[row_index])
        written_of = str(effective.get("of") or "").strip()
        if not written_of or not (
            index.variants_for("of", written_of)
            & index.variants_for("of", str(entry.get("of") or ""))
        ):
            raise HTTPException(422, "A referência escolhida não pertence à OF desta linha.")
        model = str(entry.get("modelo") or "").strip()
        if not model:
            raise HTTPException(422, "A referência escolhida não tem Modelo/Referência.")
        try:
            latest_info = loaders.plan_snapshot_info() or {}
        except Exception as exc:
            raise HTTPException(
                422, "Planeamento indisponível; tenta novamente mais tarde."
            ) from exc
        if (not latest_info.get("snapshot_id")
                or str(latest_info["snapshot_id"]) != str(current_snapshot)):
            raise HTTPException(
                409, "O planeamento mudou; reabre Referências e confirma novamente."
            )
        data_doc = sheet["sheet_data"]
        row = data_doc["rows"][row_index]
        old_model = row.get("modelo")
        old_binding = row.get("_plan_binding")
        binding = {
            "snapshot_id": str(current_snapshot),
            "plan_key": plan_key,
            "selected_explicitly": True,
        }
        row["modelo"] = model
        row["_plan_binding"] = binding
        audit = []
        if old_model != model:
            audit.append((f"rows[{row_index}].modelo", old_model, model,
                          "human", actor))
        if old_binding != binding:
            audit.append((f"rows[{row_index}]._plan_binding", old_binding, binding,
                          "human", actor))
        if audit and not db.save_sheet_data_with_edits(
            conn, uid, data_doc, revision, audit
        ):
            raise HTTPException(409, "A folha foi alterada; reabre Referências.")
        if audit:
            saved = True
            if not run_cross_check(conn, uid, force_plan=True,
                                   scorer_override=Scorer(index, CrossParams.load()) if settings.cross_engine == "v3" else None):
                raise HTTPException(
                    409,
                    "A referência foi guardada, mas a folha mudou durante o cross.",
                )
    except HTTPException as exc:
        if exc.status_code == 404 or exc.status_code >= 500:
            raise
        return RedirectResponse(
            _sheet_location(
                uid, back, erro=exc.detail, focus="problem",
                erro_context="edit",
            ),
            status_code=303,
        )
    except Exception as exc:
        print(
            f"[reference] folha {uid}, linha {row_index}: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        traceback.print_exc()
        message = (
            "A referência foi guardada, mas não foi possível atualizar o cross. "
            "A escolha ficou preservada."
            if saved else
            "Não foi possível guardar a referência; reabre o pop-up e tenta novamente."
        )
        return RedirectResponse(
            _sheet_location(
                uid, back, erro=message, focus="problem", erro_context="edit",
            ),
            status_code=303,
        )
    finally:
        conn.close()
    return RedirectResponse(_sheet_location(uid, back), status_code=303)


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
        "Content-Disposition":
            f'attachment; filename="kanban_{sheet.get("sheet_no") or uid[:8]}.pdf"',
    })


@app.post("/sheet/{uid}/header")
def sheet_header(
    request: Request,
    uid: str,
    operador: str = Form(""),
    n_operador: str = Form(""),
    setor_maquina: str = Form(""),
    data: str = Form(""),
    turno: str = Form(""),
    revision: int = Form(...),
    actor: str = Form("operador"),
    back: str = Form(""),
):
    """Guarda todo o cabeçalho numa única transação CAS auditada."""
    posted = {
        "operador": operador.strip() or None,
        "n_operador": n_operador.strip() or None,
        "setor_maquina": setor_maquina.strip() or None,
        "data": data.strip() or None,
        "turno": turno.strip() or None,
    }
    conn = _conn()
    try:
        sheet = db.get_sheet(conn, uid)
        if not sheet:
            raise HTTPException(404)
        if sheet["status"] == "validated":
            raise HTTPException(409, "Folha validada é imutável.")
        template = get_template(sheet["template_name"])
        posted = {field: posted.get(field) for field in template.header_fields}
        data_doc = sheet.get("sheet_data") or {"header": {}, "rows": [], "footer": {}}
        old_header = data_doc.get("header") or {}
        edits = [
            (f"header.{field}", old_header.get(field), value, "human", actor)
            for field, value in posted.items()
            if (str(old_header.get(field) or "").strip() or None)
               != (str(value or "").strip() or None)
        ]
        if sheet["revision"] != revision:
            latest = db.get_sheet(conn, uid) or sheet
            return _render_sheet(
                request, latest, back=back, status_code=409,
                header_draft=posted,
                erro="A folha foi alterada; confirma novamente os valores",
            )
        if edits:
            data_doc["header"] = {**old_header, **posted}
            if not db.save_sheet_data_with_edits(
                conn, uid, data_doc, revision, edits
            ):
                latest = db.get_sheet(conn, uid) or sheet
                return _render_sheet(
                    request, latest, back=back, status_code=409,
                    header_draft=posted,
                    erro="A folha foi alterada; confirma novamente os valores",
                )
        # Um único ciclo do cross depois do commit integral do formulário.
        try:
            cross_saved = run_cross_check(conn, uid)
        except Exception as exc:
            print(
                f"[header] folha {uid}: {type(exc).__name__}: {exc}",
                flush=True,
            )
            traceback.print_exc()
            return RedirectResponse(
                _sheet_location(
                    uid, back,
                    erro=("O cabeçalho foi guardado, mas não foi possível "
                          "atualizar o cross. Os valores ficaram preservados."),
                    focus="header-form", erro_context="edit",
                ),
                status_code=303,
            )
        if not cross_saved:
            return RedirectResponse(
                _sheet_location(
                    uid, back,
                    erro=("O cabeçalho foi guardado, mas a folha mudou durante "
                          "o cross. Os valores ficaram preservados."),
                    focus="header-form", erro_context="edit",
                ),
                status_code=303,
            )
    finally:
        conn.close()
    return RedirectResponse(_sheet_location(uid, back), status_code=303)


@app.post("/sheet/{uid}/edit")
def sheet_edit(request: Request, uid: str, field_path: str = Form(...),
               value: str = Form(""),
               revision: int = Form(...), actor: str = Form("operador"),
               back: str = Form("")):
    conn = _conn()
    saved = False
    focus = field_path
    try:
        sheet = db.get_sheet(conn, uid)
        if not sheet:
            raise HTTPException(404)
        if sheet["status"] == "validated":
            raise HTTPException(409, "Folha validada é imutável.")
        data = sheet["sheet_data"] or {"header": {}, "rows": [], "footer": {}}
        # field_path: 'header.data' | 'rows[3].of' | 'footer.horas_trabalhadas'.
        # Validação estrita: o endpoint é público (túnel sem auth) e um
        # rows[50000000] criava 50 M de dicts — OOM e queda do processo; um
        # índice negativo corrompia a proteção de células humanas.
        m = _FIELD_PATH_RE.match(field_path)
        if not m:
            raise HTTPException(400, "field_path inválido.")
        old = None
        value_clean = value.strip() or None
        audit_edits: list[tuple[str, object, object, str, str]] = []
        if m.group("idx") is not None:
            i = int(m.group("idx"))
            focus = f"row-{i}"
            if i > _MAX_ROWS:
                raise HTTPException(422, f"Linha {i} fora do limite ({_MAX_ROWS}).")
            if i < len(data["rows"]) and data["rows"][i].get("_deleted") is True:
                raise HTTPException(409, "A linha foi apagada e já não pode ser editada.")
            fname = m.group("rfield")
            if fname.startswith("_"):
                raise HTTPException(400, "Campo interno não é editável.")
            while len(data["rows"]) <= i:
                data["rows"].append({})
            old = data["rows"][i].get(fname)
            data["rows"][i][fname] = value_clean
            planning_fields = (
                {"of", "ov", "cliente", "perfil", "modelo", "perf_comp"}
                if sheet["template_name"] == "cantoneiras_kanban"
                else {"nesting", "maquina", "esp", "comp_mm", "larg_mm"}
            )
            if fname in planning_fields:
                old_binding = data["rows"][i].pop("_plan_binding", None)
                if old_binding is not None:
                    audit_edits.append((
                        f"rows[{i}]._plan_binding", old_binding, None,
                        "system", "binding:identity-edited",
                    ))
        else:
            section, fname = m.group("section"), m.group("sfield")
            old = (data.get(section) or {}).get(fname)
            data.setdefault(section, {})[fname] = value_clean
        # Edição que não muda nada (ex.: focar a célula e sair) não é uma
        # decisão humana: gravá-la marcava o campo como inviolável e
        # desligava a herança sem o revisor querer.
        old_clean = str(old).strip() or None if old is not None else None
        if old_clean == value_clean:
            return RedirectResponse(_sheet_location(uid, back), status_code=303)
        if sheet["revision"] != revision:
            return _render_sheet(
                request, sheet, back=back, status_code=409,
                field_draft=(field_path, value_clean),
                erro="A folha foi alterada; confirma novamente este valor.",
                focus=focus, error_context="edit",
            )
        # controlo otimista: a revisão vem do formulário — se a folha mudou
        # desde que a página foi carregada, recusa em vez de sobrescrever
        audit_edits.insert(0, (field_path, old, value_clean, "human", actor))
        if not db.save_sheet_data_with_edits(
            conn, uid, data, revision, audit_edits
        ):
            latest = db.get_sheet(conn, uid) or sheet
            return _render_sheet(
                request, latest, back=back, status_code=409,
                field_draft=(field_path, value_clean),
                erro="A folha foi alterada; confirma novamente este valor.",
                focus=focus, error_context="edit",
            )
        saved = True
        if not run_cross_check(conn, uid):
            raise HTTPException(
                409, "O valor foi guardado, mas a folha mudou durante o cross; "
                "recarrega e confirma novamente."
            )
    except HTTPException as exc:
        if not saved:
            raise
        return RedirectResponse(
            _sheet_location(
                uid, back, erro=exc.detail, focus=focus, erro_context="edit"
            ),
            status_code=303,
        )
    except Exception as exc:
        print(
            f"[edit] folha {uid}, campo {field_path}, guardado={saved}: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        traceback.print_exc()
        message = (
            "O valor foi guardado, mas não foi possível atualizar o cross. "
            "A edição ficou preservada; recarrega a folha."
            if saved else
            "Não foi possível guardar o valor. A folha foi preservada; "
            "recarrega e tenta novamente."
        )
        return RedirectResponse(
            _sheet_location(
                uid, back, erro=message, focus=focus, erro_context="edit"
            ),
            status_code=303,
        )
    finally:
        conn.close()
    return RedirectResponse(_sheet_location(uid, back), status_code=303)


@app.post("/sheet/{uid}/add-row")
def add_row(uid: str, back: str = Form("")):
    conn = _conn()
    try:
        sheet = db.get_sheet(conn, uid)
        if not sheet:
            raise HTTPException(404)
        if sheet["status"] == "validated":
            raise HTTPException(409, "Folha validada é imutável.")
        data = sheet["sheet_data"]
        if data is None:
            raise HTTPException(409, "A folha ainda está a ser lida pelo OCR.")
        template = get_template(sheet["template_name"])
        data["rows"].append({f: None for f in template.row_fields})
        if not db.save_sheet_data(conn, uid, data, sheet["revision"]):
            raise HTTPException(409, "A folha mudou entretanto — recarrega a página.")
    finally:
        conn.close()
    return RedirectResponse(_sheet_location(uid, back), status_code=303)


@app.post("/sheet/{uid}/rows/{row_index}/delete")
def delete_row(uid: str, row_index: int, revision: int = Form(...),
               actor: str = Form("operador"), back: str = Form("")):
    """Retira uma linha sem destruir a transcrição/auditoria que lhe deu origem."""
    conn = _conn()
    deleted = False
    try:
        try:
            sheet = db.get_sheet(conn, uid)
            if not sheet:
                raise HTTPException(404)
            if sheet["status"] == "validated":
                raise HTTPException(409, "Folha validada é imutável.")
            data = sheet.get("sheet_data")
            if data is None:
                raise HTTPException(409, "A folha ainda está a ser lida pelo OCR.")
            rows = data.get("rows") or []
            if row_index < 0 or row_index >= len(rows):
                raise HTTPException(422, "Linha inexistente.")
            row = rows[row_index]
            if not isinstance(row, dict) or row.get("_deleted") is True:
                raise HTTPException(409, "Esta linha já foi apagada.")
            old_row = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
            row["_deleted"] = True
            if not db.save_sheet_data_with_edits(
                conn, uid, data, revision,
                [(f"rows[{row_index}]", old_row, "<apagada>", "human", actor)],
            ):
                raise HTTPException(
                    409, "A folha foi alterada; confirma novamente os valores"
                )
            deleted = True
            if not run_cross_check(conn, uid):
                raise HTTPException(
                    409, "A linha foi apagada, mas a folha mudou durante o cross."
                )
        except HTTPException as exc:
            if exc.status_code == 404:
                raise
            return RedirectResponse(
                _sheet_location(
                    uid, back, erro=exc.detail, focus=f"row-{row_index}",
                    erro_context="edit",
                ),
                status_code=303,
            )
        except Exception as exc:
            print(
                f"[delete-row] folha {uid}, linha {row_index}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            traceback.print_exc()
            message = (
                "A linha foi apagada, mas não foi possível atualizar o cross."
                if deleted else
                "Não foi possível apagar a linha; a folha ficou preservada."
            )
            return RedirectResponse(
                _sheet_location(
                    uid, back, erro=message, focus=f"row-{row_index}",
                    erro_context="edit",
                ),
                status_code=303,
            )
    finally:
        conn.close()
    return RedirectResponse(_sheet_location(uid, back), status_code=303)


@app.post("/sheet/{uid}/recheck")
def recheck(uid: str, back: str = Form("")):
    conn = _conn()
    cross_attempted = False
    try:
        sheet = db.get_sheet(conn, uid)
        if not sheet:
            raise HTTPException(404)
        if sheet["status"] == "validated":
            raise HTTPException(409, "Folha validada é imutável.")
        cross_attempted = True
        if not run_cross_check(conn, uid):
            raise HTTPException(
                409, "A folha mudou durante o cross; tenta novamente."
            )
    except HTTPException as exc:
        if exc.status_code == 404 or not cross_attempted:
            raise
        return RedirectResponse(
            _sheet_location(
                uid, back, erro=exc.detail, erro_context="edit"
            ),
            status_code=303,
        )
    except Exception as exc:
        print(
            f"[recheck] folha {uid}: {type(exc).__name__}: {exc}",
            flush=True,
        )
        traceback.print_exc()
        return RedirectResponse(
            _sheet_location(
                uid, back,
                erro=("Não foi possível atualizar o cross. A folha e as "
                      "edições ficaram preservadas."),
                erro_context="edit",
            ),
            status_code=303,
        )
    finally:
        conn.close()
    return RedirectResponse(_sheet_location(uid, back), status_code=303)


@app.post("/sheet/{uid}/validate")
def validate(uid: str, actor: str = Form("operador"), back: str = Form(""),
             history_back: str = Form(""), revision: int | None = Form(None),
             header_operador: str | None = Form(None),
             header_n_operador: str | None = Form(None),
             header_setor_maquina: str | None = Form(None),
             header_data: str | None = Form(None),
             header_turno: str | None = Form(None)):
    """Re-cross atual + reserva CAS + INSERT PG + imutabilidade local."""
    # «Quem valida» deixou de existir no form: valida-se sem entidade e o
    # registo interno fica «operador».
    actor = actor.strip() or "operador"
    conn = _conn()
    focus: str | None = None
    try:
        before = db.get_sheet(conn, uid)
        if not before:
            raise HTTPException(404)
        if before["status"] == "validated":
            raise HTTPException(409, "Folha já validada.")
        if revision is not None and revision != before["revision"]:
            raise HTTPException(
                409, "A folha foi alterada; confirma novamente os valores."
            )
        # O botão Validar também confirma o que estiver atualmente escrito no
        # formulário do cabeçalho. Assim o utilizador nunca é obrigado a
        # carregar primeiro em «Guardar cabeçalho», nem perde o rascunho que
        # está visível no browser. POSTs antigos/administrativos que não enviam
        # estes campos continuam a validar os valores já persistidos.
        submitted_header = {
            "operador": header_operador,
            "n_operador": header_n_operador,
            "setor_maquina": header_setor_maquina,
            "data": header_data,
            "turno": header_turno,
        }
        if any(value is not None for value in submitted_header.values()):
            template = get_template(before["template_name"])
            data_doc = before.get("sheet_data") or {
                "header": {}, "rows": [], "footer": {},
            }
            old_header = dict(data_doc.get("header") or {})
            new_header = dict(old_header)
            edits = []
            for field in template.header_fields:
                raw = submitted_header.get(field)
                if raw is None:
                    continue
                value = raw.strip() or None
                new_header[field] = value
                if (str(old_header.get(field) or "").strip() or None) != value:
                    edits.append((
                        f"header.{field}", old_header.get(field), value,
                        "human", actor,
                    ))
            if edits:
                data_doc["header"] = new_header
                if not db.save_sheet_data_with_edits(
                    conn, uid, data_doc, before["revision"], edits
                ):
                    raise HTTPException(
                        409, "A folha foi alterada; confirma novamente os valores."
                    )
                before = db.get_sheet(conn, uid)
                if not before:
                    raise HTTPException(404)
        # O índice é recarregado: a validação congela uma única fotografia
        # atual, não a que por acaso ficou no cache durante a revisão.
        run_cross_check(conn, uid, force_plan=True)
        conn.execute("BEGIN IMMEDIATE")
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
        cross = sheet.get("cross_check") or {}
        if template.index_loader:
            plan_ref = cross.get("plan_reference") or {}
            if plan_ref.get("status") != "available" or not cross.get("snapshot_id"):
                raise HTTPException(
                    422, "Validação bloqueada: o planeamento está indisponível."
                )
            if cross.get("data_revision") != sheet["revision"]:
                raise HTTPException(
                    409, "A folha mudou durante o cross-check; tenta novamente."
                )
            if cross.get("engine") != "cross-v3" and cross.get("fixed_point") is False:
                raise HTTPException(
                    422, "O cross-check não estabilizou; revê a identidade das linhas."
                )
            rows = (sheet.get("sheet_data") or {}).get("rows") or []
            cross_rows = {
                row.get("row_index"): row for row in cross.get("rows", [])
            }
            visible_no = 0
            for row_index, row in enumerate(rows):
                if row.get("_deleted") is True:
                    continue
                visible_no += 1
                if not any(
                    value is not None and str(value).strip()
                    for key, value in row.items() if not str(key).startswith("_")
                ):
                    continue
                row_cross = cross_rows.get(row_index) or {}
                if cross.get("engine") == "cross-v3" and row_cross.get("row_kind") in {"empty", "activity"}:
                    continue
                if row_cross.get("binding_stale"):
                    raise HTTPException(
                        422,
                        f"Linha {visible_no}: o planeamento mudou; "
                        "reabre Referências.",
                    )
                if not row_cross.get("matched_plan_key"):
                    raise HTTPException(
                        422,
                        f"Linha {visible_no}: não existe candidato no planeamento.",
                    )
                if is_marked(field_value(row, "perf_comp")):
                    if not row_cross.get("plan_refs_valid"):
                        raise HTTPException(
                            422,
                            f"Linha {visible_no}: "
                            + (row_cross.get("plan_refs_error")
                               or "as referências de perfil completo são inválidas."),
                        )
            # Última sonda imediatamente antes de materializar no Postgres.
            # Se uma carga foi publicada depois do cross, esse snapshot já
            # deixou de ser o atual e a seleção explícita tem de ser reaberta.
            current_snapshot = _current_index_snapshot(template.index_loader)
            if current_snapshot is None:
                raise HTTPException(
                    422, "Validação bloqueada: o planeamento está indisponível."
                )
            if str(current_snapshot) != str(cross.get("snapshot_id")):
                raise HTTPException(
                    409, "O planeamento mudou; reabre Referências e confirma novamente."
                )
        try:
            n = pg_store.store_validated_sheet(
                sheet, template, db.edit_count(conn, uid), actor)
        except pg_store.InvalidSheetDate as exc:
            raise HTTPException(
                422, f"Data «{exc}» não é interpretável — escreve dd/mm/aaaa.")
        except pg_store.SheetNumberConflict as exc:
            raise HTTPException(
                409,
                f"O número público {exc.sheet_no} está associado a outra folha "
                "no histórico. A validação não foi gravada; atualiza a lista "
                "e tenta novamente.",
            )
        except Exception:
            traceback.print_exc()
            raise HTTPException(
                503,
                "Não foi possível gravar a validação no histórico. Nenhuma "
                "linha parcial foi aceite; tenta novamente.",
            )
        if not db.mark_validated(
            conn, uid, actor, expected_revision=sheet["revision"]
        ):
            raise HTTPException(
                409, "A folha mudou durante a validação — tenta novamente."
            )
    except HTTPException as exc:
        # Os portões da validação (422/409) voltam à folha como banner: o
        # form navega para o POST, e a resposta JSON crua lê-se como crash.
        # Só 404 sobe: todos os restantes portões voltam à folha. Os 5xx de
        # escrita já foram registados no log antes de serem convertidos.
        if exc.status_code == 404:
            raise
        focus = (
            "header.operador" if "operador" in str(exc.detail).lower()
            else ("header.data" if "data" in str(exc.detail).lower() else "problem")
        )
        return RedirectResponse(
            _sheet_location(uid, back, erro=exc.detail, focus=focus), status_code=303,
        )
    except Exception as exc:
        print(
            f"[validate] folha {uid}: {type(exc).__name__}: {exc}",
            flush=True,
        )
        traceback.print_exc()
        return RedirectResponse(
            _sheet_location(
                uid, back,
                erro=("Não foi possível concluir a validação. Nenhuma linha "
                      "parcial foi aceite; tenta novamente."),
                focus=focus,
            ),
            status_code=303,
        )
    finally:
        conn.close()
    destination = _safe_history_back(back) or _safe_history_back(history_back) or "/"
    return RedirectResponse(
        _with_query(
            destination,
            validated=sheet.get("sheet_no") or uid[:8],
            stored=n,
        ),
        status_code=303,
    )


from . import plan_picker  # noqa: E402
plan_picker.register(app, _conn, lambda loader: get_index(loader),
                     lambda *args, **kwargs: run_cross_check(*args, **kwargs), _sheet_location)
