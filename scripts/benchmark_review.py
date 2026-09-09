"""Read-only Postgres benchmark + ephemeral local HTTP staging.

Run from the repository root:
  .venv/bin/python scripts/benchmark_review.py --env-file .env --output docs/review-artifacts
Production tables are only read. The HTTP app uses a temporary SQLite database.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--output", type=Path, default=Path("docs/review-artifacts"))
    parser.add_argument("--repetitions", type=int, default=20)
    args = parser.parse_args()
    if args.env_file:
        for line in args.env_file.read_text().splitlines():
            parts = shlex.split(line, comments=True)
            if parts and "=" in parts[0]:
                key, value = parts[0].split("=", 1)
                os.environ[key] = value
    # All imports occur after env loading; credentials are never printed.
    from app import db
    from app.matching import loaders
    from app.matching.full_profile import expand_entries
    from app.web import main as web, export_source, export_routes, plan_review
    from tests.live_client import LiveTestClient

    args.output.mkdir(parents=True, exist_ok=True)
    result = {"source_app": plan_review.SOURCE_APP, "measured_at": datetime.now(timezone.utc).isoformat(),
              "repetitions": args.repetitions, "index_builds": 0, "timings_ms": {}}
    def measure(label, fn):
        values = []
        for _ in range(args.repetitions):
            before = time.perf_counter()
            fn()
            values.append((time.perf_counter() - before) * 1000)
        ordered = sorted(values)
        result["timings_ms"][label] = {"median": round(ordered[len(ordered) // 2], 2),
                                           "p95": round(ordered[math.ceil(len(ordered) * .95) - 1], 2),
                                           "max": round(max(ordered), 2)}

    info = loaders.plan_snapshot_info()
    sid = info["snapshot_id"]
    groups = loaders._fetch(
        "SELECT production_order_no, profile_type, count(*) AS n FROM analytics_mtg.kanban_plan_lines "
        "WHERE source_app=%s AND snapshot_id=%s GROUP BY production_order_no, profile_type ORDER BY n DESC LIMIT 3",
        (plan_review.SOURCE_APP, sid))
    result["snapshot_id"] = sid
    result["largest_groups"] = groups
    measure("snapshot", loaders.plan_snapshot_info)
    of = groups[0]["production_order_no"]
    measure("order_query", lambda: plan_review.fetch_order(sid, of))
    order = plan_review.fetch_order(sid, of)
    group = [r for r in order if plan_review.same_profile(r["profile_type"], groups[0]["profile_type"])]
    source = {"header": {}, "rows": [{"of": of, "perfil": groups[0]["profile_type"], "perf_comp": "X", "qtd": "1"}], "footer": {}}
    template = "tpl999_kanban" if plan_review.IS_MTG2 else "cantoneiras_kanban"
    real_connect = db.connect
    with tempfile.TemporaryDirectory(prefix="kanban-review-benchmark-") as temp:
        db.connect = lambda path=None: real_connect(Path(temp) / "staging.db")
        conn = db.connect()
        uids = []
        for g in groups:
            uid = db.create_sheet(conn, template)
            data = deepcopy(source)
            data["rows"][0].update(of=g["production_order_no"], perfil=g["profile_type"])
            db.set_extraction(conn, uid, data)
            uids.append(uid)
        frozen = db.create_sheet(conn, template)
        db.set_extraction(conn, frozen, source)
        cross = {"snapshot_id": sid, "rows": [{"row_index": 0, **expand_entries(group, sid)}]}
        db.save_cross_check(conn, frozen, cross, db.get_sheet(conn, frozen)["revision"])
        conn.execute("UPDATE sheets SET status='validated' WHERE uid=?", (frozen,))
        conn.commit()
        conn.close()
        def forbid_index(*_):
            result["index_builds"] += 1
            raise AssertionError("Popup attempted to load global index")
        web.get_index = forbid_index
        with LiveTestClient(web.app) as client:
            def request(uid):
                response = client.get(f"/sheet/{uid}/plano/0")
                response.raise_for_status()
                assert "tabela-plano" in response.text
            request(uids[0])  # compile templates before measuring warm requests
            measure("http_review_largest_group", lambda: request(uids[0]))
            cursor = [0]
            def alternating():
                cursor[0] += 1
                request(uids[cursor[0] % len(uids)])
            measure("http_alternating_groups", alternating)
            real_fetch = loaders._fetch
            result["frozen_plan_queries"] = 0
            def no_plan(*_):
                result["frozen_plan_queries"] += 1
                raise AssertionError("Frozen popup attempted a plan query")
            loaders._fetch = no_plan
            try:
                measure("http_validated_offline", lambda: request(frozen))
            finally:
                loaders._fetch = real_fetch
        db.connect = real_connect
    # Diagnose each archive sheet independently so the report includes all issues.
    archived = loaders._fetch(
        "SELECT sheet_uid AS uid,sheet_no,template_name,sheet_data,cross_check,plan_snapshot_id,"
        "'validated' AS status FROM mes_kanban.validated_sheets WHERE source_app=%s "
        "AND template_name NOT LIKE '%%paragens%%' ORDER BY sheet_no", (plan_review.SOURCE_APP,))
    diagnostic = {"source_app": plan_review.SOURCE_APP, "sheets": len(archived), "recoverable_sheets": 0,
                  "problems": [], "normal_metadata_warnings": []}
    for sheet in archived:
        try:
            prepared = export_source.prepare_sheets([sheet])[0]
            diagnostic["recoverable_sheets"] += 1
            if plan_review.IS_MTG2:
                for row_index, row, check in export_routes.facts_for(prepared):
                    if row.get("modelo") and not check.get("plan_identity"):
                        diagnostic["normal_metadata_warnings"].append({
                            "sheet_no": sheet["sheet_no"], "uid": sheet["uid"], "row": row_index + 1,
                            "reason": "Linha normal sem identidade do plano recuperável; Perfil e Designação completa ficam vazios.",
                        })
        except export_source.IncompleteExport as error:
            diagnostic["problems"].extend(error.problems)
    (args.output / "benchmark.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    (args.output / "legacy-diagnostic.json").write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2, default=str) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"Archive: {len(archived)} sheets, {diagnostic['recoverable_sheets']} recoverable, {len(diagnostic['problems'])} line problems.")


if __name__ == "__main__":
    main()
