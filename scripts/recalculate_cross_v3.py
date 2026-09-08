#!/usr/bin/env python3
"""Prepara e aplica uma revisão CROSS V3 de rascunhos, sem re-OCR.

O dry-run calcula numa cópia temporária do SQLite e guarda os resultados
concretos. --apply --from-report aplica esses resultados com CAS, depois de
verificar código/parâmetros, snapshot atual, revisão e transcrição original.
Folhas validadas e raw_extraction nunca são alteradas.

O ambiente deve ser o do serviço (incluindo MES_PG_* / MES_PG_DSN). O plano
atual vem sempre do PostgreSQL; --history-fixtures só fornece contexto.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import platform
import sqlite3
import sys
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode()).hexdigest()


def _read_only(path: Path):
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def engine_fingerprint() -> dict:
    from app.matching.params import CrossParams
    files = sorted(p for p in (ROOT / "app").rglob("*")
                   if p.is_file() and "__pycache__" not in p.parts)
    files += [ROOT / "scripts" / name for name in ("recalculate_cross_v3.py", "benchmark_cross_v3.py")]
    hashes = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
              for p in files}
    params_hash = _hash(asdict(CrossParams.load()))
    return {"engine": "v3", "source_hashes": hashes, "params_hash": params_hash, "fingerprint": _hash([hashes, params_hash])}


def installation_identity() -> dict:
    from app.health import STARTUP_HEALTH, _commit
    return {"source_app": STARTUP_HEALTH["app"], "platform": sys.platform,
            "host_sha256": _hash(platform.node()), "release_commit": _commit()}


def _preserved_fingerprint(conn) -> str:
    """All originals, public numbers and measured row/footer values."""
    rows = []
    for record in conn.execute("SELECT * FROM sheets ORDER BY uid"):
        record = dict(record)
        data = json.loads(record.get("sheet_data") or "{}")
        measured = [{k: v for k, v in row.items() if k not in
                     {"of", "ov", "cliente", "perfil", "modelo", "_plan_binding"}}
                    for row in data.get("rows") or []]
        rows.append({**{key: record.get(key) for key in
                     ("uid", "sheet_no", "template_name", "image_path", "image_sha256", "raw_extraction")},
                     "measured": measured, "footer": data.get("footer"), "layout": data.get("layout")})
    return _hash(rows)


def _diff(before: object, after: object, path: str = "") -> list[dict]:
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(before.keys() | after.keys()):
            result.extend(_diff(before.get(key), after.get(key), f"{path}.{key}" if path else key))
        return result
    if isinstance(before, list) and isinstance(after, list):
        result = []
        for i in range(max(len(before), len(after))):
            result.extend(_diff(before[i] if i < len(before) else None, after[i] if i < len(after) else None, f"{path}[{i}]"))
        return result
    return [] if before == after else [{"field_path": path, "before": before, "after": after}]


def _seal(report: dict) -> str:
    return _hash({key: value for key, value in report.items() if key != "report_sha256"})


def _validated_fingerprint(conn) -> str:
    rows = [dict(r) for r in conn.execute("SELECT * FROM sheets WHERE status='validated' ORDER BY uid")]
    # A migração 3 -> 5 acrescenta apenas estas fronteiras, sem alterar a
    # folha. Normalizar os defaults permite comparar a origem com a cópia
    # migrada sem ignorar valores não-zero ou qualquer outra coluna.
    for row in rows:
        row.setdefault("extraction_generation", 0)
        row.setdefault("evidence_event_floor", 0)
    return _hash(rows)


def _evidence_state(conn, sheet: dict):
    from app import db
    from app.matching.evidence import build_evidence

    events = db.evidence_edits(conn, sheet)
    evidence = build_evidence(sheet, events)
    digest = _hash({
        "data": evidence.data, "provenance": evidence.provenance,
        "explicit_bindings": evidence.explicit_bindings,
        "human_events": events,
    })
    return evidence, digest


def _target_conflict(conn, target: dict) -> str | None:
    from app import db

    sheet = db.get_sheet(conn, target["uid"])
    if sheet is None or sheet["status"] == "validated" or sheet["revision"] != target["expected_revision"]:
        return "Folha alterada/validada desde o dry-run"
    expected = target["expected_state"]
    if any(sheet.get(key, 0 if key in {"extraction_generation", "evidence_event_floor"} else None) != value
           for key, value in expected.items()):
        return "Estado, template ou geração mudou desde o dry-run"
    if sheet["status"] == "pending":
        return "OCR pendente"
    if _hash(sheet.get("raw_extraction")) != target["raw_sha256"]:
        return "Transcrição original mudou"
    if _hash(sheet.get("sheet_data")) != target["before_data_sha256"]:
        return "Dados atuais mudaram desde o dry-run"
    if _evidence_state(conn, sheet)[1] != target["before_evidence_sha256"]:
        return "Evidência humana mudou desde o dry-run"
    return None


def prepare_report(database: Path, *, uids: list[str] | None = None, history_fixtures: Path | None = None) -> dict:
    from app import db
    from app.matching import header_cross, loaders
    from app.matching.history import build_history_context, select_snapshot
    from app.matching.params import CrossParams
    from app.matching.scorer import Scorer
    from app.templates_spec import get_template
    from app.web.main import _source_document, _assumed_sheet_date, run_cross_check

    # Uniquement a ligação configurada; nunca usar um XLSM silenciosamente
    # como se fosse o snapshot ativo do serviço.
    index = loaders.load_cantoneiras_index()
    if not index.snapshot_id or not index.entries:
        raise RuntimeError("O PostgreSQL não tem um snapshot atual utilizável.")
    scorer = Scorer(index, CrossParams.load())
    frozen = None
    history_digest = None
    if history_fixtures is not None:
        from scripts.benchmark_cross_v3 import load_corpus
        frozen = load_corpus(history_fixtures)
        history_digest = hashlib.sha256((history_fixtures / "manifest.json").read_bytes()).hexdigest()

    report = {
        "format_version": 3,
        "installation": installation_identity(),
        "mode": "dry-run",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database": str(database.resolve()),
        "engine": engine_fingerprint(),
        "current_snapshot_id": index.snapshot_id,
        "current_plan_entries": len(index.entries),
        "history_fixtures": str(history_fixtures.resolve()) if history_fixtures else None,
        "history_manifest_sha256": history_digest,
        "targets": [], "skipped": [],
    }
    with tempfile.TemporaryDirectory(prefix="cross-v3-dry-run-") as temporary:
        clone_path = Path(temporary) / "staging.db"
        source = _read_only(database)
        destination = sqlite3.connect(clone_path)
        try:
            report["validated_before_sha256"] = _validated_fingerprint(source)
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        conn = db.connect(clone_path)
        try:
            metas = [dict(r) for r in conn.execute("SELECT uid,status,template_name FROM sheets ORDER BY created_at,uid")]
            known = {r["uid"] for r in metas}
            if uids and set(uids) - known:
                raise ValueError("UIDs desconhecidos: " + ", ".join(sorted(set(uids) - known)))
            for meta in metas:
                uid = meta["uid"]
                if uids and uid not in uids:
                    continue
                before = db.get_sheet(conn, uid)
                template = get_template(meta["template_name"])
                raw = before.get("raw_extraction") or {}
                evidence, evidence_digest = _evidence_state(conn, before)
                reason = None
                if meta["status"] == "validated":
                    reason = "validated_immutable"
                elif meta["status"] == "pending":
                    reason = "ocr_pending"
                elif template.name != "cantoneiras_kanban":
                    reason = "not_a_production_template"
                elif not before.get("raw_extraction"):
                    reason = "no_original_transcription"
                elif "não suportad" in str(before.get("error_message") or "").lower():
                    reason = "unsupported_document"
                elif not any(any(not str(k).startswith("_") and v is not None and str(v).strip() for k, v in row.items()) for row in evidence.data.get("rows", [])):
                    reason = "empty_original_rows"
                if reason is None:
                    from app.matching.v3 import classify_row
                    if not any(classify_row(row) == "production" for row in evidence.data.get("rows", [])):
                        reason = "no_eligible_production"
                if reason:
                    report["skipped"].append({"uid": uid, "reason": reason})
                    continue
                synthetic = Path(str(before.get("image_path") or "")).name.split("_", 1)[-1].startswith("t_")
                kwargs = {"scorer_override": scorer, "engine_override": "v3"}
                if frozen is not None:
                    snapshots = frozen["snapshots"]
                    observed_date = (evidence.data.get("header") or {}).get("data")
                    sheet_date = observed_date or _assumed_sheet_date(before)
                    choice = select_snapshot(snapshots, sheet_date, index.snapshot_id)
                    context = None
                    if choice:
                        historical = next(s for s in snapshots if s["snapshot_id"] == choice.snapshot_id)
                        context = build_history_context(index.entries, historical["entries"], choice)
                    kwargs["historical_context_override"] = context
                first_edit = conn.execute("SELECT coalesce(max(id),0) FROM edits").fetchone()[0]
                if not run_cross_check(conn, uid, **kwargs):
                    raise RuntimeError(f"CROSS não concluiu uma folha elegível: {uid}")
                after = db.get_sheet(conn, uid)
                if _hash(before.get("raw_extraction")) != _hash(after.get("raw_extraction")):
                    raise RuntimeError(f"CROSS alterou a transcrição original: {uid}")
                cross = after.get("cross_check") or {}
                if cross.get("snapshot_id") != index.snapshot_id:
                    raise RuntimeError(f"CROSS não confirmou o snapshot atual: {uid}")
                eligible = [i for i, row in enumerate(evidence.data.get("rows", []))
                            if classify_row(row) == "production"]
                associated = {row["row_index"] for row in cross.get("rows", [])
                              if row.get("row_kind") == "production" and row.get("matched_plan_key")}
                if set(eligible) != associated:
                    raise RuntimeError(f"CROSS não associou todas as linhas elegíveis: {uid}")
                edits = [dict(r) for r in conn.execute(
                    "SELECT field_path,old_value,new_value,source,actor FROM edits WHERE sheet_uid=? AND id>? ORDER BY id", (uid, first_edit),
                )]
                from app.production_facts import materialize_sheet
                target = {
                    "uid": uid, "synthetic_test_document": synthetic,
                    "expected_revision": before["revision"],
                    "expected_state": {key: before.get(key, 0 if key in {"extraction_generation", "evidence_event_floor"} else None)
                                       for key in ("status", "template_name", "extraction_generation", "evidence_event_floor", "sheet_no")},
                    "before_facts": materialize_sheet(before, template),
                    "after_facts": materialize_sheet(after, template),
                    "raw_sha256": _hash(before.get("raw_extraction")),
                    "before_data_sha256": _hash(before.get("sheet_data")),
                    "before_evidence_sha256": evidence_digest,
                    "before_evidence_provenance": evidence.provenance,
                    "changes": _diff(before.get("sheet_data"), after.get("sheet_data")),
                    "before_summary": (before.get("cross_check") or {}).get("summary"),
                    "after_summary": cross.get("summary"),
                    "after_data": after["sheet_data"], "after_cross": cross,
                    "edits": edits,
                }
                report["targets"].append(target)
            if _validated_fingerprint(conn) != report["validated_before_sha256"]:
                raise RuntimeError("O dry-run alterou uma folha validada na cópia.")
        finally:
            conn.close()
    report["report_sha256"] = _seal(report)
    return report


def apply_report(report: dict, *, backup_path: Path | None = None, expected_database: Path | None = None, check_only: bool = False) -> dict:
    from app import db
    from app.matching import loaders

    if report.get("format_version") != 3 or report.get("report_sha256") != _seal(report):
        raise ValueError("Relatório alterado/incompleto; repete o dry-run.")
    if report.get("installation") != installation_identity():
        raise ValueError("Relatório pertence a outra instalação, aplicação, plataforma ou versão Git.")
    if expected_database is not None and expected_database.resolve() != Path(report["database"]).resolve():
        raise ValueError("O relatório não pertence à base indicada.")
    if report["engine"]["fingerprint"] != engine_fingerprint()["fingerprint"]:
        raise ValueError("Código/parâmetros mudaram desde o dry-run.")
    if report.get("history_fixtures"):
        from scripts.benchmark_cross_v3 import load_corpus
        path = Path(report["history_fixtures"])
        load_corpus(path)
        if hashlib.sha256((path / "manifest.json").read_bytes()).hexdigest() != report["history_manifest_sha256"]:
            raise ValueError("Contexto congelado mudou desde o dry-run.")
    current = loaders.plan_snapshot_info()
    if current.get("snapshot_id") != report["current_snapshot_id"]:
        raise ValueError("O snapshot atual mudou; repete o dry-run.")
    database = Path(report["database"])
    source = _read_only(database)
    try:
        for target in report["targets"]:
            conflict = _target_conflict(source, target)
            if conflict:
                raise ValueError(f"{conflict}: {target['uid']}")
        validated_before = _validated_fingerprint(source)
        preserved_before = _preserved_fingerprint(source)
        if check_only:
            return {"mode": "check", "ok": True, "report_sha256": report["report_sha256"]}
        if backup_path is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup_path = database.parent / "backups" / f"app-before-cross-v3-{stamp}.db"
        if backup_path.exists():
            raise FileExistsError("O backup de destino já existe.")
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        backup = sqlite3.connect(backup_path)
        try:
            source.backup(backup)
            if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("Backup SQLite inválido.")
        finally:
            backup.close()
    finally:
        source.close()
    applied, conflicts = [], []
    conn = db.connect(database)
    try:
        for target in report["targets"]:
            cross = copy.deepcopy(target["after_cross"])
            cross["data_revision"] = cross["materialized_revision"] = target["expected_revision"] + 1
            edits = [(r["field_path"], r["old_value"], r["new_value"], r["source"], r["actor"]) for r in target["edits"]]
            ok = db.save_sheet_data_with_edits(
                conn, target["uid"], target["after_data"], target["expected_revision"],
                edits, cross_check=cross, write_cross=True,
                guard=lambda locked_conn: _target_conflict(locked_conn, target) is None,
            )
            (applied if ok else conflicts).append(target["uid"])
        # Outras validações podem acontecer no serviço entre commits. O CAS
        # protege cada alvo; o hash abaixo é diagnóstico do lote completo.
        validated_after = _validated_fingerprint(conn)
        preserved_after = _preserved_fingerprint(conn)
        data_matches_report = all(
            (db.get_sheet(conn, t["uid"]) or {}).get("sheet_data") == t["after_data"]
            for t in report["targets"] if t["uid"] in applied)
        snapshot_unchanged = loaders.plan_snapshot_info().get("snapshot_id") == report["current_snapshot_id"]
    finally:
        conn.close()
    return {
        "mode": "apply", "backup": str(backup_path), "report_sha256": report["report_sha256"],
        "current_snapshot_id": report["current_snapshot_id"], "applied": applied, "conflicts": conflicts,
        "validated_unchanged": validated_before == validated_after,
        "preserved_unchanged": preserved_before == preserved_after,
        "data_matches_report": data_matches_report, "snapshot_unchanged": snapshot_unchanged,
        "verified": not conflicts and validated_before == validated_after and preserved_before == preserved_after
                    and data_matches_report and snapshot_unchanged,
    }


def main() -> int:
    from app.config import settings
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=settings.sqlite_path)
    parser.add_argument("--uid", action="append", dest="uids")
    parser.add_argument("--history-fixtures", type=Path)
    parser.add_argument("--report", type=Path, help="Obrigatório no dry-run: resultado concreto para revisão")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true", help="Verificar o relatório sem escrever")
    parser.add_argument("--result", type=Path, help="Gravar resultado de check/apply")
    parser.add_argument("--from-report", type=Path)
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    os.environ.setdefault("PGCONNECT_TIMEOUT", "5")
    if args.apply or args.check:
        if args.from_report is None:
            parser.error("--apply exige --from-report de um dry-run")
        result = apply_report(json.loads(args.from_report.read_text(encoding="utf-8")), backup_path=args.backup,
                              expected_database=args.db, check_only=args.check)
    else:
        if args.report is None:
            parser.error("dry-run exige --report")
        result = prepare_report(args.db, uids=args.uids, history_fixtures=args.history_fixtures)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    if args.result:
        args.result.parent.mkdir(parents=True, exist_ok=True)
        args.result.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    summary = {k: v for k, v in result.items() if k in ("mode", "backup", "current_snapshot_id", "applied", "conflicts", "validated_unchanged", "preserved_unchanged", "data_matches_report", "snapshot_unchanged", "verified", "report_sha256")}
    if result["mode"] == "dry-run":
        summary.update({"targets": len(result["targets"]), "changed_cells": sum(len(t["changes"]) for t in result["targets"]), "skipped": result["skipped"], "report": str(args.report)})
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if result.get("conflicts") or result.get("verified") is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
