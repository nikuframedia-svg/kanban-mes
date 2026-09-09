#!/usr/bin/env python3
"""Replay offline do CROSS sobre transcrições e rótulos independentes congelados.

Não abre bases de dados, não faz OCR e nunca usa sheet_data/cross_check como
verdade. O relatório identifica separadamente cobertura, escolha da OF,
escolha do modelo e todas as divergências. Use --report para guardar a saída.
"""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import importlib
import importlib.util
import json
import sys
import time
import tempfile
import statistics
from types import SimpleNamespace
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.matching.cross_check import check_sheet  # noqa: E402
from app.matching.angle_geometry import profile_key
from app.matching.geometry import canonical_code, parse_decimal  # noqa: E402
from app.matching.history import build_history_context, select_snapshot  # noqa: E402
from app.matching.loaders import CANTONEIRAS_SPEC  # noqa: E402
from app.matching.params import CrossParams  # noqa: E402
from app.matching.refs import PlanIndex  # noqa: E402
from app.matching.scorer import Scorer  # noqa: E402

from app.templates_spec import get_template  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "cross_v3"


def _read_json(path: Path) -> dict | list:
    payload = path.read_bytes()
    if path.suffix == ".gz":
        payload = gzip.decompress(payload)
    return json.loads(payload)


def load_corpus(directory: Path = FIXTURE_DIR) -> dict:
    """Verifica o conteúdo congelado antes de construir qualquer índice."""
    manifest = _read_json(directory / "manifest.json")
    for filename, expected in manifest["sha256"].items():
        path = directory / filename
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Fixture alterada: {filename}")
    corpus = {
        "manifest": manifest,
        "sheets": _read_json(directory / "sheets.json"),
        "labels": _read_json(directory / "labels.json"),
        "snapshots": [],
        "legacy_source": _read_json(directory / manifest["baseline_source_file"]),
    }
    for snapshot in manifest["snapshots"]:
        corpus["snapshots"].append({**snapshot, "entries": _read_json(directory / snapshot["file"])})
    return corpus


def source_data(sheet: dict, evidence_mode: str = "raw+human") -> dict:
    """Observações congeladas; nunca injeta rótulos nem propostas anteriores."""
    from app.matching.evidence import build_evidence
    events = sheet.get("independent_human_edits", []) if evidence_mode == "raw+human" else []
    floor = sheet.get("evidence_event_floor") or 0
    boundary = sheet.get("extracted_at") or ""
    events = [e for e in events if e["id"] > floor and (sheet.get("extraction_generation") or e.get("edited_at", "") >= boundary)]
    return build_evidence({**sheet, "sheet_data": {}}, events).data


def _legacy_runtime(source: dict):
    """Carrega o baseline congelado num namespace próprio, fora do projeto."""
    package = "_cant_frozen_legacy_" + hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()[:12]
    with tempfile.TemporaryDirectory(prefix="cross-v3-legacy-") as temporary:
        root = Path(temporary)
        for name, text in source.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        spec = importlib.util.spec_from_file_location(package, root / "app/__init__.py", submodule_search_locations=[str(root / "app")])
        module = importlib.util.module_from_spec(spec)
        sys.modules[package] = module
        spec.loader.exec_module(module)
        refs = importlib.import_module(package + ".matching.refs")
        scorer = importlib.import_module(package + ".matching.scorer")
        cross = importlib.import_module(package + ".matching.cross_check")
        loaders = importlib.import_module(package + ".matching.loaders")
    return SimpleNamespace(PlanIndex=refs.PlanIndex, Scorer=scorer.Scorer, check_sheet=cross.check_sheet, spec=loaders.CANTONEIRAS_SPEC)


def legacy_replay(data: dict, index: PlanIndex, params: CrossParams, runtime=None) -> dict:
    """Reproduz as passagens de substituição legadas, sem cabeçalhos externos."""
    working = copy.deepcopy(data)
    rows = working.get("rows") or []
    scorer = (runtime.Scorer if runtime else Scorer)(index, params)
    states: set[str] = set()
    cross = {}
    for _ in range(32):
        state = json.dumps(rows, sort_keys=True, ensure_ascii=False)
        if state in states:
            break
        states.add(state)
        cross = (runtime.check_sheet if runtime else check_sheet)(rows, scorer, {}, footer=working.get("footer"))
        changed = False
        for result in cross.get("rows", []):
            row = rows[result["row_index"]]
            for cell in result.get("cells", []):
                proposed = cell.get("proposal")
                if not cell.get("auto_write") or proposed is None:
                    continue
                if str(row.get(cell["field"]) or "").strip() != str(proposed).strip():
                    row[cell["field"]] = str(proposed)
                    changed = True
        if not changed:
            break
    return cross


def _params(manifest: dict) -> CrossParams:
    from app.matching.params import ChannelParams, PolicyParams, PosteriorParams, ScoreParams
    values = manifest["params"]
    return CrossParams(
        channel=ChannelParams(**values["channel"]), score=ScoreParams(**values["score"]),
        posterior=PosteriorParams(**values["posterior"]), policy=PolicyParams(**values["policy"]),
        fitted_from=values.get("fitted_from", "frozen_fixture"),
    )


def _model(value: object) -> str:
    return canonical_code(value)


def _geometry(entry: dict) -> tuple | None:
    profile = profile_key(entry.get("perfil"))
    length = parse_decimal(entry.get("comp_mm", entry.get("length_mm")))
    if not profile or length is None or length <= 0:
        return None
    return profile, str(length.normalize())


def _confidence_summary(results: dict, applicable: set) -> dict:
    summary = {}
    for field in ("of", "profile", "model", "reference"):
        values = [
            result["confidence_by_field"][field]
            for key, result in results.items() if key in applicable
            and result.get("confidence_by_field", {}).get(field) is not None
        ]
        summary[field] = {
            "count": len(values),
            "min": round(min(values), 6) if values else None,
            "max": round(max(values), 6) if values else None,
            "mean": round(sum(values) / len(values), 6) if values else None,
            "at_least_0_95": sum(value >= 0.95 for value in values),
        }
    return summary


def run_benchmark(corpus: dict, *, snapshot_id: str | None = None, evidence_mode: str = "raw+human") -> dict:
    from app.matching.v3 import check_sheet_v3

    root = Path(__file__).resolve().parents[1]
    source_files = sorted((root / "app" / "matching").glob("*.py"))
    source_files += [root / "app" / name for name in ("templates_spec.py",)]
    source_files.append(Path(__file__).resolve())
    v3_source_hashes = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_files}
    manifest = corpus["manifest"]
    params = _params(manifest)
    snapshots = corpus["snapshots"]
    selected_id = snapshot_id or manifest["current_snapshot_id"]
    current = next((s for s in snapshots if s["snapshot_id"] == selected_id), None)
    if current is None:
        raise ValueError(f"Snapshot não congelado: {selected_id}")
    started = time.perf_counter()
    index = PlanIndex(
        current["entries"], CANTONEIRAS_SPEC,
        plan_age_days=manifest["plan_age_days"], snapshot_id=selected_id,
    )
    index_seconds = time.perf_counter() - started
    legacy = _legacy_runtime(corpus["legacy_source"])
    legacy_index = legacy.PlanIndex(current["entries"], legacy.spec, plan_age_days=manifest["plan_age_days"], snapshot_id=selected_id)
    entries_by_key = {str(e["plan_key"]): e for e in index.entries}
    current_ofs = {canonical_code(e.get("of"), "OF") for e in index.entries}
    current_groups = {(canonical_code(e.get("of"), "OF"), profile_key(e.get("perfil"))) for e in index.entries}
    current_references = {
        (canonical_code(e.get("of"), "OF"), _model(e.get("modelo"))) for e in index.entries
    }
    reference_geometries = {}
    for entry in index.entries:
        reference = (canonical_code(entry.get("of"), "OF"), _model(entry.get("modelo")))
        reference_geometries.setdefault(reference, set()).add(_geometry(entry))
    labels = {(r["uid"], r["row_index"]): r for r in corpus["labels"]["rows"]}
    applicable = {(r["uid"], i) for r in corpus["labels"]["applicable"] for i in r["row_indices"]}
    predictions: dict[str, dict] = {"legacy": {}, "v3": {}}
    seconds = {"legacy": 0.0, "v3": 0.0}
    contexts = {}
    for sheet in corpus["sheets"]:
        if not any(uid == sheet["uid"] for uid, _ in applicable):
            continue
        data = source_data(sheet, evidence_mode)
        choice = select_snapshot(snapshots, (data.get("header") or {}).get("data"), selected_id)
        context = None
        if choice is not None:
            historical = next(s for s in snapshots if s["snapshot_id"] == choice.snapshot_id)
            context = build_history_context(index.entries, historical["entries"], choice)
            contexts[sheet["uid"]] = context.to_dict()
        for name in ("legacy", "v3"):
            before = time.perf_counter()
            if name == "legacy":
                result = legacy_replay(data, legacy_index, params, legacy)
            else:
                result = check_sheet_v3(
                    data, params, index=index, historical_context=context,
                    provenance={"source": "frozen_raw_extraction", "sheet_uid": sheet["uid"]},
                    include_candidates=True,
                )
            seconds[name] += time.perf_counter() - before
            for row in result.get("rows", []):
                key = (sheet["uid"], row["row_index"])
                entry = entries_by_key.get(str(row.get("matched_plan_key"))) or {}
                label = labels.get(key, {})
                expected_of = canonical_code(label.get("of"), "OF")
                expected_model = _model(label.get("modelo"))
                candidate_entries = [entries_by_key[str(k)] for k in row.get("candidate_plan_keys", []) if str(k) in entries_by_key]
                predictions[name][key] = {
                    "plan_key": row.get("matched_plan_key"),
                    "of": canonical_code(entry.get("of"), "OF") or None,
                    "modelo": entry.get("modelo"), "perfil": entry.get("perfil"),
                    "length_mm": entry.get("comp_mm"),
                    "mode": row.get("mode"), "confidence": row.get("p_correct"),
                    "candidates_evaluated": row.get("candidates_evaluated"),
                    "candidate_of_recovered": any(
                        canonical_code(e.get("of"), "OF") == expected_of for e in candidate_entries
                    ) if name == "v3" and expected_of else None,
                    "candidate_model_recovered": any(
                        canonical_code(e.get("of"), "OF") == expected_of and _model(e.get("modelo")) == expected_model
                        for e in candidate_entries
                    ) if name == "v3" and expected_of and expected_model else None,
                    "confidence_by_field": {
                        field: row.get("confidence_" + field, row.get("p_correct") if field == "reference" else None)
                        for field in ("of", "profile", "model", "reference")
                    },
                }

    eligible_of = {key for key, label in labels.items() if label.get("of") and canonical_code(label["of"], "OF") in current_ofs and key in applicable}
    eligible_model = {
        key for key, label in labels.items()
        if label.get("modelo") and key in applicable
        and (canonical_code(label.get("of"), "OF"), _model(label["modelo"])) in current_references
    }
    expected_geometry = {}
    for key in eligible_model:
        reference = (canonical_code(labels[key].get("of"), "OF"), _model(labels[key]["modelo"]))
        geometries = reference_geometries[reference]
        if len(geometries) == 1 and None not in geometries:
            expected_geometry[key] = next(iter(geometries))
    metrics = {}
    for name, results in predictions.items():
        of_correct = sum(results.get(k, {}).get("of") == canonical_code(labels[k]["of"], "OF") for k in eligible_of)
        model_correct = sum(_model(results.get(k, {}).get("modelo")) == _model(labels[k]["modelo"]) for k in eligible_model)
        reference_correct = sum(
            _model(results.get(k, {}).get("modelo")) == _model(labels[k]["modelo"])
            and results.get(k, {}).get("of") == canonical_code(labels[k]["of"], "OF")
            for k in eligible_model
        )
        metrics[name] = {
            "applicable_rows": len(applicable),
            "matched_rows": sum(bool(results.get(k, {}).get("plan_key")) for k in applicable),
            "of_correct": of_correct, "of_evaluable": len(eligible_of),
            "model_correct": model_correct, "model_evaluable": len(eligible_model),
            "of_and_model_correct": reference_correct,
            "candidate_of_recovered": sum(results.get(k, {}).get("candidate_of_recovered") is True for k in eligible_of) if name == "v3" else None,
            "candidate_model_recovered": sum(results.get(k, {}).get("candidate_model_recovered") is True for k in eligible_model) if name == "v3" else None,
            "geometry_correct": sum(_geometry(results.get(k, {})) == geometry for k, geometry in expected_geometry.items()),
            "geometry_evaluable": len(expected_geometry),
            "confidence_summary": _confidence_summary(results, applicable),
            "seconds": round(seconds[name], 4),
            "mean_ms_per_applicable_row": round(seconds[name] * 1000 / max(len(applicable), 1), 3),
        }
    evaluation_splits = {}
    for split in sorted({sheet.get("evaluation_split", "unspecified") for sheet in corpus["sheets"]}):
        uids = {sheet["uid"] for sheet in corpus["sheets"] if sheet.get("evaluation_split", "unspecified") == split}
        keys = {key for key in applicable if key[0] in uids}
        evaluation_splits[split] = {name: {
            "documents": len(uids), "applicable_rows": len(keys),
            "matched_rows": sum(bool(results.get(k, {}).get("plan_key")) for k in keys),
            "of_correct": sum(results.get(k, {}).get("of") == canonical_code(labels[k]["of"], "OF") for k in keys & eligible_of),
            "of_evaluable": len(keys & eligible_of),
            "of_and_model_correct": sum(results.get(k, {}).get("of") == canonical_code(labels[k]["of"], "OF")
                 and _model(results.get(k, {}).get("modelo")) == _model(labels[k]["modelo"]) for k in keys & eligible_model),
            "model_evaluable": len(keys & eligible_model),
            "of_and_profile_correct": sum(results.get(k, {}).get("of") == canonical_code(labels[k]["of"], "OF")
                and profile_key(results.get(k, {}).get("perfil")) == profile_key(labels[k]["perfil"])
                for k in keys & eligible_of if labels[k].get("perfil")),
            "profile_correct": sum(profile_key(results.get(k, {}).get("perfil")) == profile_key(labels.get(k, {}).get("perfil"))
                                   for k in keys if labels.get(k, {}).get("perfil")),
            "profile_evaluable": sum(bool(labels.get(k, {}).get("perfil")) for k in keys),
        } for name, results in predictions.items()}
    comparisons = []
    for key in sorted(applicable):
        old = predictions["legacy"].get(key, {})
        new = predictions["v3"].get(key, {})
        label = labels.get(key, {})
        comparisons.append({
            "uid": key[0], "row_index": key[1],
            "observed_label": {f: label.get(f) for f in ("of", "modelo") if label.get(f)},
            "of_evaluable": key in eligible_of, "model_evaluable": key in eligible_model,
            "geometry_evaluable": key in expected_geometry,
            "changed_reference": old.get("plan_key") != new.get("plan_key"),
            "legacy": old, "v3": new,
        })
    return {
        "corpus_version": manifest["corpus_version"],
        "baseline": "legacy_fixed_point_rows_only",
        "evidence": evidence_mode,
        "baseline_commit": manifest["baseline_commit"],
        "baseline_source_sha256": manifest["sha256"][manifest["baseline_source_file"]],
        "v3_source_sha256": v3_source_hashes,
        "current_snapshot_id": selected_id, "plan_entries": len(index.entries),
        "plan_age_days": index.plan_age_days, "index_seconds": round(index_seconds, 4),
        "observable_of_labels": sum(bool(r.get("of")) for k, r in labels.items() if k in applicable),
        "observable_model_labels": sum(bool(r.get("modelo")) for k, r in labels.items() if k in applicable),
        "of_profile_groups_absent_from_snapshot": [
            {"uid": key[0], "row_index": key[1], "of": label["of"], "perfil": label["perfil"]}
            for key, label in labels.items() if key in applicable and label.get("of") and label.get("perfil")
            and (canonical_code(label["of"], "OF"), profile_key(label["perfil"])) not in current_groups
        ],
        "coverage_exclusions": [
            {"uid": k[0], "row_index": k[1], "of": r.get("of"), "modelo": r.get("modelo"), "reason": "label_not_in_current_universe"}
            for k, r in labels.items() if k in applicable and (
                r.get("of") and k not in eligible_of or r.get("modelo") and k not in eligible_model
            )
        ],
        "metrics": metrics, "evaluation_splits": evaluation_splits, "historical_contexts": contexts,
        "metric_notes": {
            "candidate_recovery": "V3 full candidate pool before selection; model requires the independently labeled OF and model in the same candidate. Legacy pool unavailable.",
            "geometry": "Canonical plan geometry for independently labeled OF/model with one unambiguous geometry. These are not independent visual geometry labels.",
            "confidence": "Informational estimates, not calibrated probabilities. Diagnostic and held-out document/date groups are reported separately.",
        },
        "reference_divergences": [r for r in comparisons if r["changed_reference"]],
        "rows": comparisons,
        "fixture_hashes": manifest["sha256"],
        "parameters": asdict(params),
    }



def run_performance(corpus: dict, uid: str = "8bfd9466f652", warm_runs: int = 3) -> dict:
    from app.matching.v3 import check_sheet_v3
    from app.health import code_fingerprint
    import platform
    params = _params(corpus["manifest"])
    snapshot = next(s for s in corpus["snapshots"] if s["snapshot_id"] == corpus["manifest"]["current_snapshot_id"])
    data = source_data(next(s for s in corpus["sheets"] if s["uid"] == uid))
    index = PlanIndex(snapshot["entries"], CANTONEIRAS_SPEC, snapshot_id=snapshot["snapshot_id"])
    legacy = _legacy_runtime(corpus["legacy_source"])
    old_index = legacy.PlanIndex(snapshot["entries"], legacy.spec, snapshot_id=snapshot["snapshot_id"])
    old_scorer = legacy.Scorer(old_index, params)
    results = {}
    for name, fn in (
        ("legacy", lambda: legacy.check_sheet(data["rows"], old_scorer, {}, footer=data.get("footer"))),
        ("v3", lambda: check_sheet_v3(data, params, index=index)),
    ):
        times = []
        for _ in range(warm_runs + 1):
            started = time.perf_counter()
            fn()
            times.append(time.perf_counter() - started)
        results[name] = {"cold_seconds": times[0], "warm_seconds": times[1:], "warm_median_seconds": statistics.median(times[1:])}
    ratio = results["v3"]["warm_median_seconds"] / results["legacy"]["warm_median_seconds"]
    return {"uid": uid, "source": "frozen_original_transcription", "plan_entries": index.n,
            "snapshot_id": index.snapshot_id, "platform": platform.platform(), "python": platform.python_version(),
            "code_fingerprint": code_fingerprint(), "measurements": results, "warm_ratio": ratio,
            "maximum_ratio": 1.5, "passed": ratio <= 1.5,
            "scope": "single cross pass for each engine, same original rows/parameters/machine; excludes PostgreSQL I/O and history loading"}

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--performance", action="store_true")
    parser.add_argument("--fixtures", type=Path, default=FIXTURE_DIR)
    parser.add_argument("--snapshot", help="Snapshot atual do cenário; por omissão o atual congelado")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--evidence", choices=("raw+human", "raw"), default="raw+human")
    args = parser.parse_args()
    corpus = load_corpus(args.fixtures)
    report = run_performance(corpus) if args.performance else run_benchmark(corpus, snapshot_id=args.snapshot, evidence_mode=args.evidence)
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    selected = ("warm_ratio", "passed", "measurements") if args.performance else ("corpus_version", "current_snapshot_id", "metrics", "evaluation_splits", "coverage_exclusions")
    print(json.dumps({k: report[k] for k in selected}, ensure_ascii=False, indent=2))
    return 1 if report.get("passed") is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
