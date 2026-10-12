"""Repõe a data escrita no papel nas folhas por validar (fim da regra do dia útil anterior, 12/10).

Até 12/10 o cross trocava a data da folha pelo «dia útil anterior à
digitalização». Mudar o código não desfaz o que já foi trocado nas folhas em
revisão; este passo volta a pôr a data que está escrita (OCR ou releitura do
cabeçalho, normalizada para dia/mês/ano). Sem data legível a folha fica sem
data e o Validar pede-a. Nunca toca em folhas validadas nem em datas que um
operador escreveu no sistema.

Por omissão só lista. Com --apply --backup CAMINHO_NOVO grava, uma edição de
sistema auditada por folha (actor «migracao:data-escrita»), com guarda de
revisão. Depois corre-se o «reverificar» de cada folha alterada na app.
"""
from contextlib import closing
import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ACTOR = "migracao:data-escrita"


def written_date(conn, sheet):
    """Data escrita no papel segundo a evidência (OCR + releitura do cabeçalho)."""
    from app import db
    from app.matching.evidence import build_evidence
    from app.matching.header_cross import canonical_date
    evidence = build_evidence(sheet, db.evidence_edits(conn, sheet))
    written = str((evidence.data.get("header") or {}).get("data") or "").strip() or None
    return written, (canonical_date(written) if written else None) or written


def inventory(conn):
    from app import db
    result = []
    for row in conn.execute("SELECT uid FROM sheets WHERE status IN ('extracted','in_review') "
                            "ORDER BY created_at, uid"):
        sheet = db.get_sheet(conn, row["uid"])
        if "data" in db.human_header_fields(conn, sheet["uid"]):
            continue
        current = ((sheet.get("sheet_data") or {}).get("header") or {}).get("data")
        written, target = written_date(conn, sheet)
        if (str(current or "").strip() or None) == target:
            continue
        result.append({"uid": sheet["uid"], "sheet_no": sheet["sheet_no"],
                       "status": sheet["status"], "revision": sheet["revision"],
                       "current": current, "written": written, "target": target})
    return result


def restore(conn, item):
    """Grava uma reposição; False se a folha mudou entretanto (fica para depois)."""
    import copy
    from app import db
    sheet = db.get_sheet(conn, item["uid"])
    if not sheet or sheet["revision"] != item["revision"] or sheet["status"] not in {"extracted", "in_review"}:
        return False
    data = copy.deepcopy(sheet["sheet_data"])
    data.setdefault("header", {})["data"] = item["target"]
    return db.save_sheet_data_with_edits(
        conn, item["uid"], data, item["revision"],
        [("header.data", item["current"], item["target"], "system", ACTOR)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "data/app.db")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    from app import db
    conn = sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        items = inventory(conn)
        if args.apply:
            if not args.backup or args.backup.exists():
                raise SystemExit("Indica --backup com um caminho novo antes de aplicar.")
            args.backup.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(args.backup) as backup:
                conn.backup(backup)
                if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise SystemExit("Cópia de segurança inválida.")
    finally:
        conn.close()
    applied, skipped = [], []
    if args.apply:
        with closing(db.connect(args.database)) as rw:
            for item in items:
                (applied if restore(rw, item) else skipped).append(item["uid"])
    print(json.dumps({"apply": args.apply, "count": len(items), "applied": applied,
                      "skipped_changed": skipped, "items": items}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
