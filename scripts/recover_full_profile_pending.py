#!/usr/bin/env python3
"""Recalcula apenas factos de Perfil Completo em folhas MTG3 pendentes.

O modo por omissão é uma simulação sobre uma cópia SQLite consistente. A
aplicação exige o relatório selado, o mesmo código, o mesmo snapshot e a mesma
revisão/evidência. Folhas validadas são apenas diagnosticadas e nunca escritas.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPT_PATH = Path(__file__).resolve()


def _hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _seal(report: dict) -> str:
    return _hash({key: value for key, value in report.items()
                  if key != "report_sha256"})


def _script_sha256() -> str:
    return hashlib.sha256(SCRIPT_PATH.read_bytes()).hexdigest()


def _read_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _has_full_profile(sheet: dict) -> bool:
    from app.templates_spec import field_value, is_marked

    return any(
        isinstance(row, dict)
        and row.get("_deleted") is not True
        and is_marked(field_value(row, "perf_comp"))
        for row in ((sheet.get("sheet_data") or {}).get("rows") or [])
    )


def _full_profile_summary(sheet: dict) -> list[dict]:
    from app.templates_spec import field_value, is_marked

    rows = ((sheet.get("sheet_data") or {}).get("rows") or [])
    checks = {row.get("row_index"): row for row in
              ((sheet.get("cross_check") or {}).get("rows") or [])}
    result = []
    for row_index, row in enumerate(rows):
        if (not isinstance(row, dict) or row.get("_deleted") is True
                or not is_marked(field_value(row, "perf_comp"))):
            continue
        check = checks.get(row_index) or {}
        result.append({
            "row_index": row_index,
            "of": row.get("of"),
            "perfil": row.get("perfil"),
            "matched_plan_key": check.get("matched_plan_key"),
            "plan_refs_valid": check.get("plan_refs_valid"),
            "plan_refs_error": check.get("plan_refs_error"),
            "references": len(check.get("plan_refs") or []),
            "quantity": check.get("full_profile_quantity"),
            "meters": check.get("plan_line_meters", check.get("line_meters")),
        })
    return result


def _human_evidence_hash(conn: sqlite3.Connection, uid: str) -> str:
    rows = [dict(row) for row in conn.execute(
        "SELECT id,field_path,old_value,new_value,source,actor,edited_at "
        "FROM edits WHERE sheet_uid=? AND source='human' ORDER BY id", (uid,),
    )]
    return _hash(rows)


def _sheet_state(conn: sqlite3.Connection, sheet: dict) -> dict:
    return {
        "uid": sheet["uid"], "sheet_no": sheet.get("sheet_no"),
        "status": sheet.get("status"), "template_name": sheet.get("template_name"),
        "revision": sheet.get("revision"),
        "raw_sha256": _hash(sheet.get("raw_extraction")),
        "data_sha256": _hash(sheet.get("sheet_data")),
        "human_evidence_sha256": _human_evidence_hash(conn, sheet["uid"]),
    }


def _validated_hash(conn: sqlite3.Connection) -> str:
    rows = [dict(row) for row in conn.execute(
        "SELECT * FROM sheets WHERE status='validated' ORDER BY uid"
    )]
    return _hash(rows)


def _identity_changes(before: dict, after: dict) -> list[str]:
    """Deteta mudanças de associação sem confundir factos agora preenchidos."""
    old_rows = {row.get("row_index"): row for row in
                ((before.get("cross_check") or {}).get("rows") or [])}
    new_rows = {row.get("row_index"): row for row in
                ((after.get("cross_check") or {}).get("rows") or [])}
    changes = []
    for row_index, old in old_rows.items():
        new = new_rows.get(row_index) or {}
        old_key = old.get("matched_plan_key")
        if old_key and old_key != new.get("matched_plan_key"):
            changes.append(
                f"linha {row_index + 1}: referência {old_key!r} passou a "
                f"{new.get('matched_plan_key')!r}"
            )
        old_identity = old.get("plan_identity") or {}
        new_identity = new.get("plan_identity") or {}
        for field in ("production_order_no", "profile_type", "component_ref"):
            if old_identity.get(field) is not None and (
                    old_identity.get(field) != new_identity.get(field)):
                changes.append(
                    f"linha {row_index + 1}: {field} mudou de "
                    f"{old_identity.get(field)!r} para {new_identity.get(field)!r}"
                )
        if new.get("binding_stale"):
            changes.append(f"linha {row_index + 1}: associação desatualizada")
    return changes


def _conflict(conn: sqlite3.Connection, target: dict) -> str | None:
    from app import db

    sheet = db.get_sheet(conn, target["uid"])
    if not sheet:
        return "Folha deixou de existir"
    marker = (sheet.get("cross_check") or {}).get("full_profile_recovery_report")
    if marker == target.get("report_sha256"):
        return "already_applied"
    current = _sheet_state(conn, sheet)
    if current != target["expected_state"]:
        return "Folha, revisão ou evidência mudou desde a simulação"
    if sheet["status"] not in {"extracted", "in_review"}:
        return "Folha já não está pendente de validação"
    return None


def prepare_report(database: Path, uids: list[str] | None = None) -> dict:
    from app import db
    from app.health import code_fingerprint
    from app.matching import loaders
    from app.matching.params import CrossParams
    from app.matching.scorer import Scorer
    from app.web.main import run_cross_check

    info = loaders.plan_snapshot_info()
    snapshot = str(info.get("snapshot_id") or "")
    if not snapshot:
        raise RuntimeError("O snapshot atual do planeamento não foi identificado.")
    index = loaders.load_cantoneiras_index(snapshot_id=snapshot)
    if not index.entries or str(index.snapshot_id) != snapshot:
        raise RuntimeError("Não foi possível carregar o snapshot atual completo.")
    scorer = Scorer(
        index, CrossParams.load(), active_primary=loaders.load_active_ofs(),
    )
    report = {
        "format_version": 1,
        "mode": "dry-run",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database": str(database.resolve()),
        "code_fingerprint": code_fingerprint(),
        "recovery_code_sha256": _script_sha256(),
        "snapshot_id": snapshot,
        "plan_entries": len(index.entries),
        "targets": [], "unchanged": [], "manual_review": [],
        "validated_diagnostics": [],
    }

    with tempfile.TemporaryDirectory(prefix="full-profile-recovery-") as temp:
        clone_path = Path(temp) / "app.db"
        source = _read_only(database)
        clone = sqlite3.connect(clone_path)
        try:
            source.backup(clone)
        finally:
            clone.close()
            source.close()
        conn = db.connect(clone_path)
        try:
            validated_before = _validated_hash(conn)
            sheets = [db.get_sheet(conn, row["uid"]) for row in conn.execute(
                "SELECT uid FROM sheets ORDER BY sheet_no,uid"
            )]
            known = {sheet["uid"] for sheet in sheets if sheet}
            if uids and set(uids) - known:
                raise ValueError("UIDs desconhecidos: " + ", ".join(
                    sorted(set(uids) - known)))
            for before in sheets:
                if not before or (uids and before["uid"] not in uids):
                    continue
                if before["template_name"] != "cantoneiras_kanban" or not _has_full_profile(before):
                    continue
                if before["status"] == "validated":
                    report["validated_diagnostics"].append({
                        "uid": before["uid"], "sheet_no": before.get("sheet_no"),
                        "rows": _full_profile_summary(before),
                    })
                    continue
                if before["status"] not in {"extracted", "in_review"}:
                    report["manual_review"].append({
                        "uid": before["uid"], "sheet_no": before.get("sheet_no"),
                        "reason": f"Estado não elegível: {before['status']}",
                    })
                    continue
                expected = _sheet_state(conn, before)
                if not run_cross_check(
                    conn, before["uid"], scorer_override=scorer,
                    engine_override="legacy",
                ):
                    report["manual_review"].append({
                        "uid": before["uid"], "sheet_no": before.get("sheet_no"),
                        "reason": "O cruzamento não concluiu.",
                    })
                    continue
                after = db.get_sheet(conn, before["uid"])
                if (_hash(after.get("raw_extraction")) != expected["raw_sha256"]
                        or _hash(after.get("sheet_data")) != expected["data_sha256"]
                        or after.get("sheet_no") != before.get("sheet_no")):
                    report["manual_review"].append({
                        "uid": before["uid"], "sheet_no": before.get("sheet_no"),
                        "reason": "O cruzamento propôs alterar dados revistos; exige revisão manual.",
                        "before": _full_profile_summary(before),
                        "after": _full_profile_summary(after),
                    })
                    continue
                identity_changes = _identity_changes(before, after)
                if identity_changes:
                    report["manual_review"].append({
                        "uid": before["uid"], "sheet_no": before.get("sheet_no"),
                        "reason": "O cruzamento propôs outra associação.",
                        "identity_changes": identity_changes,
                        "before": _full_profile_summary(before),
                        "after": _full_profile_summary(after),
                    })
                    continue
                after_rows = _full_profile_summary(after)
                invalid = [row for row in after_rows if row["plan_refs_valid"] is not True]
                if invalid:
                    report["manual_review"].append({
                        "uid": before["uid"], "sheet_no": before.get("sheet_no"),
                        "reason": "Perfil completo ainda sem factos válidos.",
                        "rows": invalid,
                    })
                    continue
                item = {
                    "uid": before["uid"], "sheet_no": before.get("sheet_no"),
                    "expected_state": expected,
                    "before": _full_profile_summary(before), "after": after_rows,
                    "after_cross": after.get("cross_check"),
                }
                if before.get("cross_check") == after.get("cross_check"):
                    report["unchanged"].append({key: item[key] for key in
                                                ("uid", "sheet_no", "before")})
                else:
                    report["targets"].append(item)
            if _validated_hash(conn) != validated_before:
                raise RuntimeError("A simulação alterou uma folha validada.")
        finally:
            conn.close()
    report["report_sha256"] = _seal(report)
    for target in report["targets"]:
        target["report_sha256"] = report["report_sha256"]
    # O selo não inclui a cópia colocada em cada alvo; voltar a selar produziria
    # uma autorreferência. A validação remove essa cópia antes de confirmar.
    return report


def _verify_report(report: dict) -> None:
    copy_for_seal = copy.deepcopy(report)
    for target in copy_for_seal.get("targets", []):
        target.pop("report_sha256", None)
    if (report.get("format_version") != 1
            or report.get("report_sha256") != _seal(copy_for_seal)):
        raise ValueError("Relatório alterado ou incompleto; repete a simulação.")


def apply_report(report: dict, *, database: Path, backup_path: Path | None,
                 check_only: bool = False) -> dict:
    from app import db
    from app.health import code_fingerprint
    from app.matching import loaders

    _verify_report(report)
    if database.resolve() != Path(report["database"]).resolve():
        raise ValueError("O relatório pertence a outra base SQLite.")
    if code_fingerprint() != report["code_fingerprint"]:
        raise ValueError("O código mudou desde a simulação.")
    if _script_sha256() != report.get("recovery_code_sha256"):
        raise ValueError("O utilitário de recuperação mudou desde a simulação.")
    if str((loaders.plan_snapshot_info() or {}).get("snapshot_id") or "") != report["snapshot_id"]:
        raise ValueError("O snapshot mudou; repete a simulação.")

    source = _read_only(database)
    conflicts, already = [], []
    try:
        validated_before = _validated_hash(source)
        for target in report["targets"]:
            reason = _conflict(source, target)
            if reason == "already_applied":
                already.append(target["uid"])
            elif reason:
                conflicts.append({"uid": target["uid"], "reason": reason})
        if conflicts:
            raise ValueError("Conflitos: " + json.dumps(conflicts, ensure_ascii=False))
        if check_only:
            return {"mode": "check", "ok": True, "already_applied": already}
        pending = [target for target in report["targets"]
                   if target["uid"] not in already]
        if pending:
            if backup_path is None:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                backup_path = database.parent / "backups" / f"before-full-profile-{stamp}.db"
            if backup_path.exists():
                raise FileExistsError("O backup de destino já existe.")
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            backup = sqlite3.connect(backup_path)
            try:
                source.backup(backup)
                if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("O backup SQLite falhou o quick_check.")
            finally:
                backup.close()
    finally:
        source.close()

    applied = []
    conn = db.connect(database)
    try:
        for target in report["targets"]:
            if target["uid"] in already:
                continue
            current = db.get_sheet(conn, target["uid"])
            cross = copy.deepcopy(target["after_cross"])
            cross["full_profile_recovery_report"] = report["report_sha256"]
            cross["data_revision"] = cross["materialized_revision"] = current["revision"] + 1
            edit = [(
                "cross_check.full_profile_recovery",
                _full_profile_summary(current), target["after"],
                "system", "recovery:full-profile",
            )]
            ok = db.save_sheet_data_with_edits(
                conn, target["uid"], current["sheet_data"], current["revision"],
                edit, cross_check=cross, write_cross=True,
                keep_status=True,
                guard=lambda locked, item=target: _conflict(locked, item) is None,
            )
            if not ok:
                raise RuntimeError(f"Conflito durante a aplicação: {target['uid']}")
            applied.append(target["uid"])
        validated_after = _validated_hash(conn)
    finally:
        conn.close()
    return {
        "mode": "apply", "applied": applied, "already_applied": already,
        "backup": str(backup_path) if backup_path else None,
        "validated_unchanged": validated_before == validated_after,
        "verified": validated_before == validated_after,
    }


def main(argv: list[str] | None = None) -> int:
    from app.config import settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=settings.sqlite_path)
    parser.add_argument("--uid", action="append", dest="uids")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--from-report", type=Path)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.check or args.apply:
        if not args.from_report:
            parser.error("--check/--apply exige --from-report")
        report = json.loads(args.from_report.read_text(encoding="utf-8"))
        result = apply_report(
            report, database=args.db, backup_path=args.backup,
            check_only=args.check,
        )
    else:
        if not args.report:
            parser.error("A simulação exige --report")
        result = prepare_report(args.db, args.uids)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
