"""Rollout rehearsed on temporary SQLite, including races during apply."""

from copy import deepcopy
import json
from pathlib import Path
import sqlite3

import pytest

from app import db
from app.matching import history, loaders
from app.matching.refs import PlanIndex
from app.web import main
from scripts import recalculate_cross_v3 as rollout


def snapshot(path):
    with sqlite3.connect(path) as conn:
        return {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in ("sheets", "edits")
        }


@pytest.fixture
def staged(tmp_path, monkeypatch):
    path = tmp_path / "staging.db"
    conn = db.connect(path)
    monkeypatch.setattr(main, "get_employees", lambda: {})
    monkeypatch.setattr(main, "_load_header_machines", lambda: [])
    monkeypatch.setattr(history, "load_history_context", lambda *args: None)
    index = PlanIndex([{
        "plan_key": "A", "of": "OF123", "ov": "OV111", "cliente_nome": "PLANO",
        "perfil": "L60X60X5", "modelo": "M78", "comp_mm": 78,
        "qtd_planeada": 20, "qtd_restante": 10, "falta_valida": True,
    }], loaders.CANTONEIRAS_SPEC, snapshot_id="rollout-plan")
    monkeypatch.setattr(loaders, "load_cantoneiras_index", lambda: index)
    monkeypatch.setattr(loaders, "plan_snapshot_info", lambda: {"snapshot_id": "rollout-plan"})

    def create(row):
        uid = db.create_sheet(conn, "cantoneiras_kanban")
        assert db.set_extraction(conn, uid, {
            "header": {"data": "14/08/2026", "operador": "TESTE"},
            "rows": [row], "footer": {"horas_trabalhadas": "7.5"},
        })
        return uid

    draft = create({"of": "123", "perfil": "60x5", "qtd": "3"})
    validated = create({"of": "123", "perfil": "60x5", "qtd": "2"})
    assert db.mark_validated(conn, validated, "teste")
    empty = create({})
    yield path, conn, draft, validated, empty
    conn.close()


def test_dry_run_is_read_only_and_apply_matches_review_with_backup_and_audit(staged, tmp_path):
    path, conn, draft, validated, empty = staged
    original = snapshot(path)
    report = rollout.prepare_report(path)
    assert snapshot(path) == original
    assert [target["uid"] for target in report["targets"]] == [draft]
    assert {item["uid"]: item["reason"] for item in report["skipped"]} == {
        validated: "validated_immutable", empty: "empty_original_rows",
    }
    # Exercise the actual JSON artifact consumed by --from-report.
    report = json.loads(json.dumps(report))
    result = rollout.apply_report(report, backup_path=tmp_path / "backup.db")
    assert result["applied"] == [draft]
    assert result["conflicts"] == []
    assert result["validated_unchanged"] is True
    assert snapshot(Path(result["backup"])) == original
    target = report["targets"][0]
    current = db.get_sheet(conn, draft)
    assert current["sheet_data"] == target["after_data"]
    assert current["cross_check"]["materialized_revision"] == current["revision"]
    assert rollout._hash(current["raw_extraction"]) == target["raw_sha256"]
    audit = [dict(row) for row in conn.execute(
        "SELECT field_path,old_value,new_value,source,actor FROM edits WHERE sheet_uid=? ORDER BY id", (draft,),
    )]
    assert audit == target["edits"]
    assert audit


@pytest.mark.parametrize("change", [
    "human_edit", "validation", "raw_without_revision", "data_without_revision", "audit_without_revision",
    "status", "template_name", "extraction_generation",
])
def test_preflight_rejects_changed_document_before_creating_backup(staged, tmp_path, change):
    path, conn, draft, *_ = staged
    report = rollout.prepare_report(path)
    current = db.get_sheet(conn, draft)
    if change == "human_edit":
        data = deepcopy(current["sheet_data"])
        data["rows"][0]["qtd"] = "99"
        assert db.save_sheet_data_with_edits(conn, draft, data, current["revision"], [
            ("rows[0].qtd", "3", "99", "human", "teste"),
        ])
    elif change == "validation":
        assert db.mark_validated(conn, draft, "teste")
    elif change == "raw_without_revision":
        # Also reject external OCR/data repairs that omitted the revision.
        conn.execute("UPDATE sheets SET raw_extraction=? WHERE uid=?", ('{"rows":[]}', draft))
        conn.commit()
    elif change == "data_without_revision":
        current["sheet_data"]["rows"][0]["qtd"] = "99"
        conn.execute("UPDATE sheets SET sheet_data=? WHERE uid=?", (json.dumps(current["sheet_data"]), draft))
        conn.commit()
    elif change in {"status", "template_name", "extraction_generation"}:
        value = {"status": "pending", "template_name": "chapa_kanban", "extraction_generation": 100}[change]
        conn.execute(f"UPDATE sheets SET {change}=? WHERE uid=?", (value, draft))
        conn.commit()
    else:
        db.record_edit(conn, draft, "rows[0].of", "123", "456", "human", "outro")
    before_apply = snapshot(path)
    backup = tmp_path / "backup.db"
    with pytest.raises(ValueError):
        rollout.apply_report(report, backup_path=backup)
    assert not backup.exists()
    assert snapshot(path) == before_apply


@pytest.mark.parametrize("change", ["report", "engine", "snapshot"])
def test_preflight_rejects_unreviewed_report_or_changed_dependencies(staged, tmp_path, monkeypatch, change):
    path, *_ = staged
    report = rollout.prepare_report(path)
    if change == "report":
        report["targets"][0]["after_data"]["rows"][0]["qtd"] = "99"
    elif change == "engine":
        monkeypatch.setattr(rollout, "engine_fingerprint", lambda: {"fingerprint": "changed"})
    else:
        monkeypatch.setattr(loaders, "plan_snapshot_info", lambda: {"snapshot_id": "changed"})
    before_apply = snapshot(path)
    with pytest.raises(ValueError):
        rollout.apply_report(report, backup_path=tmp_path / "backup.db")
    assert snapshot(path) == before_apply
    assert not (tmp_path / "backup.db").exists()


@pytest.mark.parametrize("change", ["human_edit", "validation", "audit_without_revision", "data_without_revision", "raw_without_revision"])
def test_cas_rejects_concurrent_change_after_preflight_and_keeps_backup(staged, tmp_path, monkeypatch, change):
    path, conn, draft, *_ = staged
    report = rollout.prepare_report(path)
    original = snapshot(path)
    real_save = db.save_sheet_data_with_edits
    after_race = None

    def save_after_race(*args, **kwargs):
        nonlocal after_race
        racing = db.connect(path)
        try:
            current = db.get_sheet(racing, draft)
            if change == "validation":
                assert db.mark_validated(racing, draft, "outro")
            elif change == "audit_without_revision":
                db.record_edit(racing, draft, "rows[0].of", "123", "456", "human", "outro")
            elif change == "data_without_revision":
                current["sheet_data"]["rows"][0]["qtd"] = "99"
                racing.execute("UPDATE sheets SET sheet_data=? WHERE uid=?", (json.dumps(current["sheet_data"]), draft))
                racing.commit()
            elif change == "raw_without_revision":
                racing.execute("UPDATE sheets SET raw_extraction=? WHERE uid=?", ('{"rows":[]}', draft))
                racing.commit()
            else:
                current["sheet_data"]["rows"][0]["qtd"] = "99"
                assert real_save(racing, draft, current["sheet_data"], current["revision"], [
                    ("rows[0].qtd", "3", "99", "human", "outro"),
                ])
            after_race = snapshot(path)
        finally:
            racing.close()
        return real_save(*args, **kwargs)

    monkeypatch.setattr(db, "save_sheet_data_with_edits", save_after_race)
    result = rollout.apply_report(report, backup_path=tmp_path / "backup.db")
    assert result["applied"] == []
    assert result["conflicts"] == [draft]
    assert snapshot(Path(result["backup"])) == original
    assert snapshot(path) == after_race


def test_missing_current_plan_cannot_prepare_or_mutate(staged, monkeypatch):
    path, *_ = staged
    monkeypatch.setattr(loaders, "load_cantoneiras_index", lambda: PlanIndex([], loaders.CANTONEIRAS_SPEC))
    original = snapshot(path)
    with pytest.raises(RuntimeError, match="snapshot atual"):
        rollout.prepare_report(path)
    assert snapshot(path) == original


def test_legacy_schema_dry_run_does_not_confuse_additive_defaults_with_validated_changes(staged):
    path, conn, draft, *_ = staged
    # Production databases predate these metadata columns. db.connect on
    # the dry-run clone adds them, without altering any validated decision.
    conn.execute("ALTER TABLE sheets DROP COLUMN extraction_generation")
    conn.execute("ALTER TABLE sheets DROP COLUMN evidence_event_floor")
    conn.commit()
    original = snapshot(path)
    report = rollout.prepare_report(path)
    assert [target["uid"] for target in report["targets"]] == [draft]
    assert snapshot(path) == original


def test_human_observation_on_empty_extraction_is_eligible_for_recalculation(staged):
    path, conn, _, _, empty = staged
    current = db.get_sheet(conn, empty)
    current["sheet_data"]["rows"][0]["of"] = "123"
    current["sheet_data"]["rows"][0]["qtd"] = "3"
    assert db.save_sheet_data_with_edits(conn, empty, current["sheet_data"], current["revision"], [
        ("rows[0].of", None, "123", "human", "teste"),
        ("rows[0].qtd", None, "3", "human", "teste"),
    ])
    original = snapshot(path)
    report = rollout.prepare_report(path, uids=[empty])
    assert [target["uid"] for target in report["targets"]] == [empty]
    assert report["targets"][0]["after_cross"]["rows"][0]["matched_plan_key"] == "A"
    assert report["targets"][0]["after_data"]["rows"][0]["qtd"] == "3"
    assert snapshot(path) == original


def test_frozen_history_uses_human_date_instead_of_original_ocr_date(staged, tmp_path, monkeypatch):
    from scripts import benchmark_cross_v3

    path, conn, draft, *_ = staged
    current = db.get_sheet(conn, draft)
    current["sheet_data"]["header"]["data"] = "20/08/2026"
    assert db.save_sheet_data_with_edits(conn, draft, current["sheet_data"], current["revision"], [
        ("header.data", "14/08/2026", "20/08/2026", "human", "teste"),
    ])
    index = loaders.load_cantoneiras_index()
    fixtures = tmp_path / "history"
    fixtures.mkdir()
    (fixtures / "manifest.json").write_text("{}")
    monkeypatch.setattr(benchmark_cross_v3, "load_corpus", lambda path: {"snapshots": [
        {"snapshot_id": "on-ocr-date", "loaded_at": "2026-08-14T10:00:00Z", "entries": index.entries},
        {"snapshot_id": "on-human-date", "loaded_at": "2026-08-20T10:00:00Z", "entries": index.entries},
    ]})
    original = snapshot(path)
    report = rollout.prepare_report(path, uids=[draft], history_fixtures=fixtures)
    cross = report["targets"][0]["after_cross"]
    assert cross["historical_context"]["snapshot_id"] == "on-human-date"
    assert snapshot(path) == original


def test_failed_cross_aborts_preparation_without_changing_original(staged, monkeypatch):
    path, *_ = staged
    before = snapshot(path)
    monkeypatch.setattr(main, "run_cross_check", lambda *args, **kwargs: False)
    with pytest.raises(RuntimeError, match="não concluiu"):
        rollout.prepare_report(path)
    assert snapshot(path) == before
