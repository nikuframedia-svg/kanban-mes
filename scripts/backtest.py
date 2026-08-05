"""Backtest do motor de matching com dados reais do Postgres.

Fonte: raw_mtg.chapa_kanban_daily_rows — 20.618 registos reais de operadores
(o que foi efetivamente cortado, dia a dia) — cruzados contra o índice de
nestings (11.684 programas). O ground truth é o próprio nesting_code do registo.

Cenários:
  A (limpo)  — as linhas tal como estão: mede acerto top-1 e cobertura;
  B (ruído)  — perturba a escrita com confusões de glifos/omissões, simulando
               caligrafia/OCR: mede taxa de recuperação;
  C (cantoneiras) — amostra do plano de perfis com ruído, mesmo teste.

Medições que ficam em params/cross_params.json (nada copiado de fora):
  - m por campo  = P(campo concorda | linha certa), medido nos pares corretos;
  - temperatura T calibrada por Brier score sobre o cenário com ruído.

Uso:
  MES_PG_DSN="host=... user=postgres password=..." .venv/bin/python scripts/backtest.py [--sample 3000]
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.matching import loaders, similarity as sim  # noqa: E402
from app.matching.params import GLYPH_CONFUSABLE, CrossParams, PARAMS_PATH  # noqa: E402
from app.matching.scorer import Scorer  # noqa: E402

RNG = random.Random(42)

_CONFUSION: dict[str, list[str]] = defaultdict(list)
for a, b in GLYPH_CONFUSABLE:
    _CONFUSION[a].append(b)
    _CONFUSION[b].append(a)


def add_noise_code(code: str) -> str:
    """Simula caligrafia/OCR: troca de glifo confundível e/ou omissão."""
    chars = list(code)
    swappable = [i for i, c in enumerate(chars) if c.upper() in _CONFUSION]
    if swappable and RNG.random() < 0.7:
        i = RNG.choice(swappable)
        chars[i] = RNG.choice(_CONFUSION[chars[i].upper()])
    if len(chars) > 4 and RNG.random() < 0.15:
        del chars[RNG.randrange(len(chars))]
    return "".join(chars)


def add_noise_text(text: str) -> str:
    """Operadores abreviam nomes: corta a partir do 1º token às vezes."""
    tokens = text.split()
    if len(tokens) > 1 and RNG.random() < 0.5:
        return " ".join(tokens[: RNG.randint(1, len(tokens) - 1)])
    return text


def posterior_for_t(pool_bits: list[float], n_plan: int, age_days: float,
                    t: float, params: CrossParams) -> float:
    """Recalcula o posterior para uma temperatura t (calibração offline)."""
    p = params.posterior
    b_max = max(pool_bits)
    z = sum(math.pow(2.0, (b - b_max) / t) for b in pool_bits)
    pi = min(p.pi_h0_max, p.pi_h0_base + p.pi_h0_per_day * max(age_days, 0.0))
    pi = max(pi, 1e-4)
    b_h0 = p.b_h0_raw_bits + math.log2(max(n_plan, 2))
    w_h0 = (pi / (1.0 - pi)) * math.pow(2.0, (b_h0 - b_max) / t)
    return 1.0 / (z + w_h0)


def run_scenario(name: str, rows: list[dict], truths: list[str], scorer: Scorer,
                 collect_calibration: list | None = None) -> dict:
    top1 = 0
    strong = 0
    strong_correct = 0
    weak = 0
    no_match = 0
    for k, (row, truth) in enumerate(zip(rows, truths)):
        if k and k % 250 == 0:
            print(f"  [{name}] {k}/{len(rows)}…", flush=True)
        m = scorer.match_row(row)
        correct = False
        if m.winner is not None:
            winner_primary = scorer.index.normalized(scorer._primary, m.winner.idx)
            correct = winner_primary == sim.normalize_code(truth)
        top1 += correct
        if m.winner is None:
            no_match += 1
        elif m.mode == "strong":
            strong += 1
            strong_correct += correct
        else:
            weak += 1
        if collect_calibration is not None and m.winner is not None:
            memo: dict = {}
            pool = [scorer.score_entry(row, i, memo) for i in scorer.candidates(row)]
            collect_calibration.append(([s.bits for s in pool], correct))
    n = len(rows)
    out = {
        "scenario": name,
        "rows": n,
        "top1_accuracy": round(top1 / n, 4) if n else None,
        "strong": strong,
        "strong_precision": round(strong_correct / strong, 4) if strong else None,
        "weak_guess": weak,
        "no_match": no_match,
    }
    print(f"[{name}] {json.dumps(out, ensure_ascii=False)}")
    return out


def measure_m(rows: list[dict], truths: list[str], index) -> dict[str, float]:
    """m por campo = P(concordância | par correto), medido nos dados reais."""
    agree = defaultdict(int)
    total = defaultdict(int)
    truth_idx: dict[str, int] = {}
    primary = index.spec.identity_fields[0].name
    for i in range(index.n):
        truth_idx.setdefault(index.normalized(primary, i), i)
    for row, truth in zip(rows, truths):
        idx = truth_idx.get(sim.normalize_code(truth))
        if idx is None:
            continue
        entry = index.entries[idx]
        for f in index.spec.identity_fields:
            written = row.get(f.name)
            if written is None or str(written).strip() == "":
                continue
            total[f.name] += 1
            truth_v = str(entry.get(f.entry_key) or "")
            s = (sim.code_similarity(str(written), truth_v) if f.kind == "code"
                 else sim.text_similarity(str(written), truth_v))
            agree[f.name] += s >= 1.0
        for f in index.spec.numeric_fields:
            w = sim.parse_number(row.get(f.name))
            t = sim.parse_number(entry.get(f.entry_key))
            if w is None or t is None:
                continue
            total[f.name] += 1
            agree[f.name] += sim.numeric_similarity(w, t, f.tolerance) >= 1.0
    return {
        f: round(agree[f] / total[f], 4)
        for f in total if total[f] >= 30
    }


def calibrate_t(samples: list[tuple[list[float], bool]], n_plan: int,
                age_days: float, params: CrossParams) -> tuple[float, dict[str, float]]:
    briers = {}
    for t in (1.0, 1.5, 2.0, 2.5, 3.0, 4.0):
        se = [
            (posterior_for_t(bits, n_plan, age_days, t, params) - correct) ** 2
            for bits, correct in samples
        ]
        briers[str(t)] = round(sum(se) / len(se), 4)
    best = min(briers, key=briers.get)
    return float(best), briers


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=3000)
    ap.add_argument("--save-params", action="store_true",
                    help="gravar m/T medidos em params/cross_params.json")
    args = ap.parse_args()

    params = CrossParams()
    report: dict = {"sample": args.sample}

    # ---------- chapa: kanban diário real vs nestings ----------
    print("A carregar índice de nestings e kanban diário do Postgres…")
    nesting_index = loaders.load_nesting_index()
    kanban = loaders._fetch(
        """
        SELECT nesting_code, machine_name, thickness_mm, sheet_length_mm,
               sheet_width_mm, event_date
        FROM raw_mtg.chapa_kanban_daily_rows
        WHERE nesting_code IS NOT NULL AND nesting_code <> ''
        """
    )
    known = {sim.normalize_code(e["nesting"]) for e in nesting_index.entries}
    in_plan = [r for r in kanban if sim.normalize_code(r["nesting_code"]) in known]
    coverage = len(in_plan) / len(kanban) if kanban else 0.0
    print(f"Kanban: {len(kanban)} registos; {len(in_plan)} com nesting no índice "
          f"(cobertura {coverage:.1%})")
    report["kanban_rows"] = len(kanban)
    report["kanban_coverage"] = round(coverage, 4)

    if len(in_plan) > args.sample:
        in_plan = RNG.sample(in_plan, args.sample)

    def kanban_to_row(r: dict) -> dict:
        return {
            "nesting": r["nesting_code"],
            "maquina": r["machine_name"],
            "esp": r["thickness_mm"],
            "comp_mm": r["sheet_length_mm"],
            "larg_mm": r["sheet_width_mm"],
        }

    clean_rows = [kanban_to_row(r) for r in in_plan]
    truths = [r["nesting_code"] for r in in_plan]

    # m medido nos dados reais (pares corretos, cenário limpo)
    m_measured = measure_m(clean_rows, truths, nesting_index)
    print(f"m medido por campo (chapa/nesting): {m_measured}")
    report["m_by_field_nesting"] = m_measured

    scorer = Scorer(nesting_index, params)
    report["chapa_clean"] = run_scenario("chapa limpo", clean_rows, truths, scorer)

    noisy_rows = []
    for r in in_plan:
        row = kanban_to_row(r)
        row["nesting"] = add_noise_code(str(row["nesting"]))
        if row["maquina"]:
            row["maquina"] = add_noise_text(str(row["maquina"]))
        noisy_rows.append(row)
    calibration: list = []
    report["chapa_noisy"] = run_scenario("chapa ruído", noisy_rows, truths, scorer, calibration)

    if calibration:
        best_t, briers = calibrate_t(
            calibration, nesting_index.n, nesting_index.plan_age_days, params)
        print(f"Calibração T por Brier: {briers} → melhor T = {best_t}")
        report["brier_by_t"] = briers
        report["best_t"] = best_t

    # ---------- cantoneiras: amostra do plano com ruído ----------
    print("A carregar plano de cantoneiras…")
    cant_index = loaders.load_cantoneiras_index()
    sample_entries = RNG.sample(cant_index.entries, min(args.sample, cant_index.n))
    cant_rows, cant_truths = [], []
    for e in sample_entries:
        cant_rows.append({
            "of": add_noise_code(str(e["of"])),
            "ov": add_noise_code(str(e["ov"])) if e.get("ov") else None,
            "cliente": add_noise_text(str(e["cliente"])) if e.get("cliente") else None,
            "modelo": add_noise_code(str(e["modelo"])) if e.get("modelo") else None,
            "comp_mm": e.get("comp_mm"),
        })
        cant_truths.append(str(e["of"]))
    cant_scorer = Scorer(cant_index, params)
    report["cantoneiras_noisy"] = run_scenario(
        "cantoneiras ruído", cant_rows, cant_truths, cant_scorer)

    m_cant = measure_m(cant_rows, cant_truths, cant_index)
    report["m_by_field_cantoneiras"] = m_cant

    # ---------- gravar parâmetros medidos ----------
    out_path = Path(__file__).resolve().parent.parent / "params" / "backtest_report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    print(f"Relatório: {out_path}")

    if args.save_params:
        fitted = CrossParams()
        # Só o m medido em ESCRITA REAL de operadores (kanban diário da chapa)
        # entra nos parâmetros. O m das cantoneiras vem de ruído sintético —
        # serve para relatório, não para calibrar; fica o default até haver
        # folhas reais validadas.
        real_identity = {"nesting", "maquina"}
        fitted.score.m_by_field = {
            k: v for k, v in m_measured.items() if k in real_identity
        }
        if report.get("best_t"):
            fitted.posterior.temperature_bits = report["best_t"]
        from datetime import date
        fitted.fitted_from = f"backtest {date.today().isoformat()}"
        fitted.save()
        print(f"Parâmetros medidos gravados em {PARAMS_PATH}")


if __name__ == "__main__":
    main()
