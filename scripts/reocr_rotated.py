#!/usr/bin/env python
"""Relê com OCR as folhas que foram lidas antes da correcção de orientação.

Até à correcção, as folhas iam para o modelo deitadas e ele trocava colunas —
mediu-se uma folha em que as quantidades gravadas vinham das marcas da coluna
seguinte. Este script relê essas folhas com a imagem já direita.

Só toca em folhas seguras: com imagem, ainda não validadas, e **sem edições
humanas** — reler sobrescreve `sheet_data` e `raw_extraction`, e uma folha já
corrigida à mão perderia esse trabalho.

Uso:
    python scripts/reocr_rotated.py                # mostra o que faria
    python scripts/reocr_rotated.py --apply        # relê e grava
    python scripts/reocr_rotated.py --apply --uid abc123
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db, imaging  # noqa: E402
from app.ocr.provider import OcrError, get_provider  # noqa: E402
from app.templates_spec import get_template  # noqa: E402
from app.web.main import run_cross_check  # noqa: E402


def candidates(conn, only_uid: str | None) -> list[dict]:
    sql = """
        SELECT s.uid, s.template_name, s.status, s.image_path, s.image_rotation,
               (SELECT count(*) FROM edits e
                 WHERE e.sheet_uid = s.uid AND e.source = 'human') AS human_edits
          FROM sheets s
         WHERE s.image_path IS NOT NULL AND s.status != 'validated'
         ORDER BY s.created_at
    """
    rows = [dict(r) for r in conn.execute(sql)]
    if only_uid:
        rows = [r for r in rows if r["uid"] == only_uid]
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="gravar (por omissão só mostra)")
    ap.add_argument("--uid", help="reler só esta folha")
    ap.add_argument("--sleep", type=float, default=6.0, help="segundos entre folhas")
    args = ap.parse_args()

    conn = db.connect()
    try:
        rows = candidates(conn, args.uid)
        todo, skipped = [], []
        for r in rows:
            path = Path(r["image_path"])
            if not path.is_file():
                skipped.append((r["uid"], "imagem em falta"))
            elif r["human_edits"]:
                skipped.append((r["uid"], f"{r['human_edits']} edições humanas"))
            elif imaging.render_oriented(path, int(r["image_rotation"] or 0)) == path:
                skipped.append((r["uid"], "já está direita"))
            else:
                todo.append(r)

        print(f"{len(todo)} folha(s) a reler, {len(skipped)} saltada(s)\n")
        for uid, why in skipped:
            print(f"  - {uid}  ({why})")
        if skipped:
            print()

        if not args.apply:
            for r in todo:
                print(f"  · {r['uid']}  {r['template_name']:22s} {Path(r['image_path']).name}")
            print("\n(simulação — usa --apply para gravar)")
            return 0

        provider = get_provider()
        for i, r in enumerate(todo, 1):
            uid = r["uid"]
            image = imaging.render_oriented(Path(r["image_path"]), int(r["image_rotation"] or 0))
            template_name = r["template_name"]
            before = db.get_sheet(conn, uid)["sheet_data"] or {}
            try:
                if (get_template(template_name).family == "cantoneiras"
                        and hasattr(provider, "extract_auto")):
                    kinds = {"producao": get_template("cantoneiras_kanban"),
                             "paragens": get_template("cantoneiras_paragens")}
                    kind, extraction = provider.extract_auto(image, kinds)
                    if kinds[kind].name != template_name:
                        template_name = kinds[kind].name
                        db.set_template(conn, uid, template_name)
                else:
                    extraction = provider.extract(image, get_template(template_name))
            except OcrError as exc:
                print(f"  [{i}/{len(todo)}] {uid}: FALHOU — {exc}")
                continue
            # set_extraction agora exige status 'pending' (proteção contra
            # esmagar edições humanas) — este script já só toca folhas sem
            # edições, portanto re-enfileirar é seguro.
            db.mark_pending(conn, uid)
            if not db.set_extraction(conn, uid, extraction):
                print(f"  [{i}/{len(todo)}] {uid}: SALTADA (mudou entretanto)")
                continue
            run_cross_check(conn, uid)
            n_before = len([x for x in (before.get("rows") or []) if any(x.values())])
            n_after = len(extraction.get("rows") or [])
            print(f"  [{i}/{len(todo)}] {uid} {template_name:22s} {n_before} -> {n_after} linhas")
            if i < len(todo):
                time.sleep(args.sleep)
        print("\nfeito.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
