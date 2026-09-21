"""Explicit historical reference for isolated web tests, never live PG."""
from app import historical_quantities as h
from app.matching.full_profile import plan_identity

def install(monkeypatch, entries, snapshot="past"):
    monkeypatch.setattr(h, "load_snapshot", lambda day: {"snapshot_id": snapshot,
        "loaded_at": "2026-01-01T12:00:00+00:00", "cutoff": h.production_cutoff(day).isoformat()})
    def order(sid, of):
        source = entries() if callable(entries) else entries
        return [{**e, **plan_identity(e, sid),
                 "remaining_quantity": e.get("qtd_restante"), "remaining_valid": e.get("falta_valida"),
                 "remaining_rule": e.get("regra_calculo"), "quantity_planned": e.get("qtd_planeada"),
                 "quantity_made": e.get("qtd_feita")}
                for e in source if h.canonical_code(e.get("of"), prefix="OF") == of]
    monkeypatch.setattr(h, "load_order", order)
