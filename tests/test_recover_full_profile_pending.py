"""Recuperação auditada de factos, sem validar nem alterar originais."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from app import db
from app.matching.loaders import CANTONEIRAS_SPEC
from app.matching.refs import PlanIndex


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/recover_full_profile_pending.py"
SPEC = importlib.util.spec_from_file_location("recover_full_profile_pending", SCRIPT)
assert SPEC and SPEC.loader
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)


def _index():
    entries = [{
        "snapshot_id": "snap", "plan_key": f"K{i}",
        "of": "OF42", "ov": "OV21", "cliente": "CLIENTE",
        "cliente_nome": "CLIENTE", "modelo": f"REF-{i}",
        "perfil": "L100X100X10", "comp_mm": length,
        "qtd_planeada": qty, "qtd_feita": 0, "qtd_restante": qty,
        "excesso": 0, "falta_valida": True,
        "regra_calculo": "calculated:qtd_minus_maq_blank_zero",
        "maquina": "FICEP", "semana": 39,
    } for i, (length, qty) in enumerate(((1000, 2), (1500, 3)))]
    return PlanIndex(entries, CANTONEIRAS_SPEC, snapshot_id="snap")


def _sheet(conn, *, validated=False):
    uid = db.create_sheet(conn, "cantoneiras_kanban")
    data = {
        "header": {"data": "16/09/2026"},
        "rows": [{
            "of": "42", "ov": "21", "cliente": "CLIENTE",
            "perfil": "L100X100X10", "modelo": None,
            "qtd": None, "perf_comp": "X",
        }],
        "footer": {},
    }
    db.set_extraction(conn, uid, data)
    db.record_edit(conn, uid, 'header.data', None, '16/09/2026', 'human', 'test')
    if validated:
        conn.execute("UPDATE sheets SET status='validated' WHERE uid=?", (uid,))
        conn.commit()
    return uid


def test_recovery_dry_run_apply_and_repeat_are_safe(tmp_path, monkeypatch):
    path = tmp_path / "app.db"
    conn = db.connect(path)
    try:
        pending = _sheet(conn)
        frozen = _sheet(conn, validated=True)
        pending_before = db.get_sheet(conn, pending)
        frozen_before = db.get_sheet(conn, frozen)
    finally:
        conn.close()

    index = _index()
    from tests.historical_fixtures import install
    install(monkeypatch, index.entries, "snap")
    from app.matching import loaders
    monkeypatch.setattr(loaders, "plan_snapshot_info", lambda: {"snapshot_id": "snap"})
    monkeypatch.setattr(loaders, "load_cantoneiras_index", lambda snapshot_id=None: index)
    monkeypatch.setattr(loaders, "load_active_ofs", lambda: set())
    monkeypatch.setattr(loaders, "load_machines", lambda: [])

    report = recovery.prepare_report(path)
    assert [target["uid"] for target in report["targets"]] == [pending]
    assert report["targets"][0]["after"][0]["quantity"] == 5
    assert report["targets"][0]["after"][0]["meters"] == 6.5
    assert [item["uid"] for item in report["validated_diagnostics"]] == [frozen]

    backup = tmp_path / "backup.db"
    applied = recovery.apply_report(
        report, database=path, backup_path=backup,
    )
    assert applied["applied"] == [pending]
    assert applied["validated_unchanged"]
    assert backup.exists()
    conn = db.connect(path)
    try:
        current = db.get_sheet(conn, pending)
        assert current["cross_check"]["full_profile_recovery_report"] == report["report_sha256"]
        assert current["sheet_data"] == pending_before["sheet_data"]
        assert current["raw_extraction"] == pending_before["raw_extraction"]
        assert current["sheet_no"] == pending_before["sheet_no"]
        assert current["status"] == pending_before["status"]
        assert db.get_sheet(conn, frozen) == frozen_before
        audit = conn.execute(
            "SELECT actor FROM edits WHERE sheet_uid=? AND field_path=?",
            (pending, "cross_check.full_profile_recovery"),
        ).fetchone()
        assert audit["actor"] == "recovery:full-profile"
    finally:
        conn.close()

    repeated = recovery.apply_report(
        report, database=path, backup_path=tmp_path / "unused.db",
    )
    assert repeated["applied"] == []
    assert repeated["already_applied"] == [pending]
    assert not (tmp_path / "unused.db").exists()
