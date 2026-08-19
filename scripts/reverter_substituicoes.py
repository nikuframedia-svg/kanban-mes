#!/usr/bin/env python
"""Reverte as substituições de modelo/perfil feitas pela política de 19/08.

A primeira versão do «substituir sempre» escreveu modelos/perfis com o
marginal do campo a ignorar — no pior caso, um modelo escolhido por ordem
alfabética entre 8 linhas irmãs empatadas (p=0.013) por cima de um valor bem
manuscrito (AT1T515 → AT1T145). Este script repõe o valor anterior a partir do
trilho de auditoria, apenas onde é seguro:

- só edits `system/cross` de 2026-08-19 em rows[i].modelo / rows[i].perfil;
- inclui as escritas em células VAZIAS: uma materialização baseada num match
  errado deixou perfis do palpite (ex.: L40X40X5 do AT1T145) a guiar os
  matches seguintes — repõe-se o vazio e o recheck rematerializa só o que o
  motor corrigido confirmar;
- só em folhas não validadas;
- só se a célula ainda tem exatamente o valor que o motor escreveu (se um
  humano mexeu depois, não se toca).

Depois de reverter, correr o recheck geral: o motor corrigido volta a
substituir apenas o que o marginal autoriza.

    python scripts/reverter_substituicoes.py            # mostra o que faria
    python scripts/reverter_substituicoes.py --apply
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402
from app.matching import similarity as sim  # noqa: E402

_PATH = re.compile(r"^rows\[(\d+)\]\.(modelo|perfil)$")


def _norm(value: object) -> str:
    return sim.strip_ref_prefix(sim.compact(value))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="gravar (por omissão só mostra)")
    args = ap.parse_args()

    conn = db.connect()
    try:
        edits = conn.execute(
            """SELECT id, sheet_uid, field_path, old_value, new_value FROM edits
               WHERE source = 'system' AND actor = 'cross'
                 AND edited_at >= '2026-08-19'
                 AND (field_path LIKE '%.modelo' OR field_path LIKE '%.perfil')
               ORDER BY id"""
        ).fetchall()

        # última escrita por célula (se o motor escreveu 2x, interessa a última)
        by_cell: dict[tuple[str, str], dict] = {}
        for e in edits:
            if _norm(e["old_value"]) != _norm(e["new_value"]):
                by_cell[(e["sheet_uid"], e["field_path"])] = dict(e)

        revertidas = saltadas = 0
        for (uid, path), e in sorted(by_cell.items()):
            m = _PATH.match(path)
            if not m:
                continue
            i, campo = int(m.group(1)), m.group(2)
            sheet = db.get_sheet(conn, uid)
            if not sheet or sheet["status"] == "validated" or not sheet["sheet_data"]:
                saltadas += 1
                continue
            # humano mexeu nesta célula depois? então a decisão é dele
            humano_depois = conn.execute(
                "SELECT 1 FROM edits WHERE sheet_uid=? AND field_path=? "
                "AND source='human' AND id > ? LIMIT 1", (uid, path, e["id"])
            ).fetchone()
            if humano_depois:
                saltadas += 1
                continue
            rows = sheet["sheet_data"].get("rows") or []
            if i >= len(rows) or str(rows[i].get(campo) or "").strip() != str(e["new_value"]).strip():
                saltadas += 1          # a célula já não tem o valor do motor
                continue
            print(f"  {uid[:8]} {path}: {e['new_value']!r} -> {e['old_value']!r}")
            revertidas += 1
            if args.apply:
                rows[i][campo] = e["old_value"]
                if not db.save_sheet_data(conn, uid, sheet["sheet_data"], sheet["revision"]):
                    print(f"    AVISO: {uid[:8]} mudou entretanto — não gravada")
                    revertidas -= 1
                    continue
                db.record_edit(conn, uid, path, e["new_value"], e["old_value"],
                               "system", "reversao-19-08")

        print(f"\n{revertidas} célula(s) revertida(s), {saltadas} saltada(s)")
        if not args.apply:
            print("(simulação — usa --apply para gravar; depois corre o recheck geral)")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
