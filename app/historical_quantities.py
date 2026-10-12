"""Production balances frozen before the effective day (never nearest/future).

Independent of matching.history, whose observations only break matching ties.
The persisted basis is shared by review, validation, PG and exports.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import threading
import time as _clock
from collections import OrderedDict
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from .matching import loaders
from .matching.angle_geometry import profile_key
from .matching.geometry import canonical_code
from .matching.full_profile import expand_entries
from .templates_spec import field_value, is_marked

VERSION = 1

# Cada edição de uma folha com Perf. Comp. voltava ao Postgres: a lista de
# cargas e as linhas da OF em cada carga (2-4 idas pelo túnel por edição). Uma
# carga guardada nunca muda, por isso as linhas de (carga, OF) ficam em memória
# para sempre (até ao limite); a lista de cargas, que cresce, só 60 s.
_SNAPSHOTS_TTL_S = 60.0
_ORDERS_MAX = 512
_cache_lock = threading.Lock()
_snapshots_cache: tuple[float, list] | None = None
_orders_cache: "OrderedDict[tuple[str, str], list]" = OrderedDict()


def clear_caches() -> None:
    global _snapshots_cache
    with _cache_lock:
        _snapshots_cache = None
        _orders_cache.clear()


def production_cutoff(value):
    from .pg_store import normalize_sheet_date
    day = normalize_sheet_date(value)
    return datetime.combine(datetime.fromisoformat(day).date(), time.min,
                            ZoneInfo("Europe/Lisbon")).astimezone(timezone.utc)


def select_snapshot(snapshots, value):
    cutoff = production_cutoff(value)
    candidates = []
    for item in snapshots:
        loaded = item.get("loaded_at") or item.get("snapshot_loaded_at")
        if isinstance(loaded, str):
            loaded = datetime.fromisoformat(loaded.replace("Z", "+00:00"))
        if loaded is not None and loaded.tzinfo is not None and loaded < cutoff:
            candidates.append((loaded, str(item["snapshot_id"])))
    if not candidates:
        raise ValueError("Não existe plano guardado antes do dia de produção.")
    loaded, sid = max(candidates)
    return {"snapshot_id": sid, "loaded_at": loaded.isoformat(),
            "cutoff": cutoff.isoformat(), "timezone": "Europe/Lisbon"}


def _plan_snapshots():
    global _snapshots_cache
    with _cache_lock:
        hit = _snapshots_cache
    if hit is not None and _clock.monotonic() - hit[0] < _SNAPSHOTS_TTL_S:
        return hit[1]
    from .pg_store import SOURCE_APP
    condition, params = (("dataset_id = %s", ("ds-met2-perfis",))
                         if SOURCE_APP == "kanban-mes-mtg2" else
                         ("snapshot_id LIKE %s", (loaders._CANTONEIRAS_LIKE,)))
    rows = loaders._fetch("SELECT snapshot_id, loaded_at FROM audit_mtg.snapshots WHERE " + condition, params)
    with _cache_lock:
        _snapshots_cache = (_clock.monotonic(), rows)
    return rows


def load_snapshot(value):
    return select_snapshot(_plan_snapshots(), value)


def select_later_snapshots(snapshots, value):
    """Cargas a partir do dia de produção, da mais antiga para a mais recente."""
    cutoff = production_cutoff(value)
    later = []
    for item in snapshots:
        loaded = item.get("loaded_at") or item.get("snapshot_loaded_at")
        if isinstance(loaded, str):
            loaded = datetime.fromisoformat(loaded.replace("Z", "+00:00"))
        if loaded is not None and loaded.tzinfo is not None and loaded >= cutoff:
            later.append((loaded, str(item["snapshot_id"])))
    return [{"snapshot_id": sid, "loaded_at": loaded.isoformat(),
             "cutoff": cutoff.isoformat(), "timezone": "Europe/Lisbon"}
            for loaded, sid in sorted(later)]


def load_later_snapshots(value):
    return select_later_snapshots(_plan_snapshots(), value)


def load_order(snapshot_id, order):
    key = (str(snapshot_id), str(order))
    with _cache_lock:
        hit = _orders_cache.get(key)
        if hit is not None:
            _orders_cache.move_to_end(key)
            return hit
    rows = _load_order(snapshot_id, order)
    with _cache_lock:
        _orders_cache[key] = rows
        while len(_orders_cache) > _ORDERS_MAX:
            _orders_cache.popitem(last=False)
    return rows


def _load_order(snapshot_id, order):
    from .pg_store import SOURCE_APP
    extra, join = "", ""
    if SOURCE_APP == "kanban-mes-mtg2":
        extra = ", r.row_data ->> 'Ø Ext' AS profile_excel_o"
        join = " LEFT JOIN raw_mtg.plan_production_rows r ON r.snapshot_id=l.snapshot_id AND r.source_line_id=l.plan_key"
    return loaders._fetch(
        "SELECT l.*" + extra + " FROM analytics_mtg.kanban_plan_lines l" + join +
        " WHERE l.source_app=%s AND l.snapshot_id=%s AND l.production_order_no = ANY(%s) ORDER BY l.plan_key",
        (SOURCE_APP, snapshot_id, [order, "OF" + order]))


def context(data, row, decision_ids=()):
    from .pg_store import normalize_sheet_date
    return {"date": normalize_sheet_date((data.get("header") or {}).get("data")),
            "of": canonical_code(row.get("of"), prefix="OF"),
            "profile": profile_key(row.get("perfil")),
            "full": is_marked(field_value(row, "perf_comp")),
            "binding": row.get("_plan_binding"), "decisions": list(decision_ids),
            "unresolved": row.get("_identity_unresolved"), "version": VERSION}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def latest_plan_snapshot():
    """Id da carga mais recente do plano deste setor (cache curta em loaders)."""
    return (loaders.plan_snapshot_info() or {}).get("snapshot_id")


def apply(sheet, data, cross, *, decisions=(), snapshot_loader=None, order_loader=None,
          later_loader=None, latest_loader=None, complete_validated=False):
    """Saldo (quantidade em falta antes da produção) das linhas Perf. Comp.

    Regra única (25/09): a última carga do plano antes do dia de produção; se
    essa não tiver a OF + perfil — uma OF que entra no plano no próprio dia em
    que é cortada —, a primeira carga seguinte que a tenha, marcada como saldo
    aproximado. Se nenhuma tiver, a linha fica sem saldo e a validação avisa.
    """
    # Uma folha validada tem os factos congelados. A única exceção é o
    # sync_worker a completar um saldo que falhou por falta de ligação
    # (``transient``) antes de gravar no Postgres.
    if sheet.get("status") == "validated" and not complete_validated:
        return
    if later_loader is None:
        # Com um loader de teste explícito não se vai ao Postgres por omissão.
        later_loader = load_later_snapshots if snapshot_loader is None else (lambda value: [])
    if latest_loader is None:
        latest_loader = latest_plan_snapshot if snapshot_loader is None else (lambda: None)
    snapshot_loader = snapshot_loader or load_snapshot
    order_loader = order_loader or load_order
    previous = {r["row_index"]: r for r in (sheet.get("cross_check") or {}).get("rows", [])}
    loaded_snapshots: dict[str, object] = {}
    orders = {}

    def candidates(day):
        """(carga, aproximado) por ordem de preferência; cada lista uma vez.

        Uma falha de ligação fica lembrada: as linhas seguintes da mesma folha
        falham logo, em vez de esperarem cada uma pelo mesmo timeout."""
        if "error" in loaded_snapshots:
            raise loaded_snapshots["error"]
        try:
            if "before" not in loaded_snapshots:
                try:
                    loaded_snapshots["before"] = snapshot_loader(day)
                except ValueError:
                    loaded_snapshots["before"] = None
            if loaded_snapshots["before"] is not None:
                yield loaded_snapshots["before"], False
            if "later" not in loaded_snapshots:
                loaded_snapshots["later"] = later_loader(day)
        except ValueError:
            raise
        except Exception as exc:
            loaded_snapshots["error"] = exc
            raise
        for later in loaded_snapshots["later"]:
            yield later, True
    for rc in cross.get("rows", []):
        i = rc["row_index"]
        row = data["rows"][i]
        if row.get("_deleted") is True or not is_marked(field_value(row, "perf_comp")):
            continue
        # Only decisions affecting this basis invalidate it, not later plan loads.
        relevant = [e["id"] for e in decisions if e.get("source") == "human" and
                    (e.get("field_path") == "header.data" or
                     str(e.get("field_path", "")).startswith(f"rows[{i}]"))]
        basis = {"version": VERSION, "status": "unavailable"}
        try:
            ctx = context(data, row, relevant)
            signature = fingerprint(ctx)
            basis.update(context=ctx, fingerprint=signature, date=ctx["date"])
            old = previous.get(i, {})
            frozen = old.get("quantity_basis") or {}
            if frozen.get("fingerprint") == signature and frozen.get("status") == "ready":
                for key in ("quantity_basis", "plan_refs", "plan_refs_valid", "plan_refs_error",
                            "full_profile_quantity", "plan_length_mm", "plan_line_meters",
                            "line_meters", "plan_meters_error"):
                    rc[key] = copy.deepcopy(old.get(key))
                continue
            if not ctx["of"] or not ctx["profile"] or ctx["unresolved"]:
                raise ValueError("OF e perfil físico precisam de identificação inequívoca.")
            # O saldo é da OF + perfil escritos na linha, não da ligação ao
            # plano: uma correspondência fraca já não o impede (aviso à parte).
            chosen = None
            for snapshot, approximate in candidates(ctx["date"]):
                key = (snapshot["snapshot_id"], ctx["of"])
                if key not in orders:
                    try:
                        orders[key] = order_loader(*key)
                    except ValueError:
                        raise
                    except Exception as exc:
                        loaded_snapshots["error"] = exc
                        raise
                found = [e for e in orders[key] if profile_key(e.get("profile_type")) == ctx["profile"]]
                if found:
                    chosen = (snapshot, approximate, found)
                    break
            if chosen is None:
                raise ValueError("Sem referências para OF + perfil em nenhuma carga guardada do plano.")
            selected, approximate, entries = chosen
            basis.update(selected)
            identities = [(canonical_code(e.get("component_ref")), profile_key(e.get("profile_type")),
                           str(e.get("length_mm"))) for e in entries]
            for entry in entries:
                if not canonical_code(entry.get("component_ref")):
                    raise ValueError("Referência histórica sem identidade.")
                quantity = entry.get("remaining_quantity")
                if quantity is not None and not math.isfinite(float(quantity)):
                    raise ValueError("Quantidade histórica inválida.")
            if len(identities) != len(set(identities)):
                raise ValueError("Referências históricas repetidas: identidade ambígua.")
            expanded = expand_entries(entries, selected["snapshot_id"])
            if not expanded["plan_refs_valid"]:
                raise ValueError(expanded["plan_refs_error"])
            rc.update(expanded)
            basis.update(status="ready", approximate=approximate,
                         diagnostic=("first_snapshot_with_order_after_production_day"
                                     if approximate else "last_snapshot_before_production_day"))
        except Exception as exc:
            # Failure cannot silently reuse current-plan quantities or invent zero.
            basis["diagnostic"] = str(exc)[:300] if isinstance(exc, ValueError) else "Histórico do plano indisponível. Tenta verificar novamente."
            if not isinstance(exc, ValueError):
                # Falha de ligação, não dos dados: o sync_worker volta a tentar.
                basis["transient"] = True
            # Carga do plano em vigor quando falhou: quando chegar uma mais
            # recente (a OF ou o perfil podem ter entrado entretanto), abrir a
            # folha volta a calcular o saldo — ver needs_refresh (12/10).
            try:
                basis["plan_snapshot"] = latest_loader()
            except Exception:
                basis["plan_snapshot"] = None
            rc.update(plan_refs=[], plan_refs_valid=False, plan_refs_error=basis["diagnostic"],
                      full_profile_quantity=None, plan_length_mm=None,
                      plan_line_meters=None, line_meters=None)
        rc["quantity_basis"] = basis
    production = [r for r in cross.get("rows", []) if r.get("row_kind", r.get("mode"))
                  not in {"deleted", "empty", "activity"}]
    if any(r.get("quantity_basis") for r in production):
        meters = [r.get("plan_line_meters", r.get("line_meters")) for r in production]
        known = [m for m in meters if m is not None]
        summary = cross.setdefault("summary", {})
        total = round(sum(known), 3) if known else None
        summary.update(metros_teoricos=total, metros_parciais=any(m is None for m in meters))
        measured = summary.get("metros_produzidos")
        summary["desperdicio_m"] = (round(measured-total, 2) if measured is not None
            and total is not None and not summary["metros_parciais"] else None)


def has_transient_gap(cross) -> bool:
    return any((r.get("quantity_basis") or {}).get("transient")
               for r in (cross or {}).get("rows", []))


def needs_refresh(sheet, latest_loader=None):
    """Saldo por calcular de novo: versão antiga, ou falhou com uma carga do
    plano que entretanto deixou de ser a mais recente (sem isto, uma folha lida
    antes de a OF entrar no plano era validada sem o saldo)."""
    if sheet.get("status") not in {"extracted", "in_review"}:
        return False
    checks = {r["row_index"]: r for r in (sheet.get("cross_check") or {}).get("rows", [])}
    latest = None
    for i, row in enumerate((sheet.get("sheet_data") or {}).get("rows", [])):
        if row.get("_deleted") is not True and is_marked(field_value(row, "perf_comp")):
            basis = checks.get(i, {}).get("quantity_basis") or {}
            if basis.get("version") != VERSION:
                return True
            if basis.get("status") == "unavailable":
                if latest is None:
                    try:
                        latest = (latest_loader or latest_plan_snapshot)() or ""
                    except Exception:
                        return False
                if latest and basis.get("plan_snapshot") != latest:
                    return True
    return False
