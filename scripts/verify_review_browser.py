"""Isolated browser flow and visual comparison. No production database writes.

Requires an installed Playwright Node package (--playwright). Screenshots and
synthetic normal/full-profile workbooks are saved in docs/review-artifacts.
The OCR reference templates are rendered directly from the read-only checkout.
"""

import argparse
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--playwright", default="/home/luis/projects/ppx_mvp/scripts/node_modules/playwright")
    parser.add_argument("--original", type=Path, default=Path("/home/luis/projects/ocr/backend/app/web"))
    parser.add_argument("--output", type=Path, default=Path("docs/review-artifacts"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    from fastapi import Request
    from fastapi.responses import HTMLResponse, FileResponse
    from fastapi.staticfiles import StaticFiles
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    from app import db, pg_store
    from app.matching import loaders
    from app.matching.full_profile import attach_plan_facts
    from app.matching.params import CrossParams
    from app.matching.refs import PlanIndex
    from app.matching.scorer import Scorer
    from app.web import main as web, export_routes, export_source, plan_review
    from tests.live_client import LiveTestClient
    from tests.test_plan_review_flow import entries, sheet, TEMPLATE

    source = sheet()
    normal = sheet(False)
    for sample in (source, normal):
        for check in sample["cross_check"]["rows"]:
            check.update(cells=[{"field": f, "status": "confirmed", "p_correct": 1., "written": row.get(f), "proposal": None, "inherited_from": None}
                                for f in ("of", "ov", "cliente", "perfil", "modelo", "qtd") for row in sample["sheet_data"]["rows"]],
                         matched_plan_key="old:0", plan_identity=__import__("app.matching.full_profile", fromlist=["plan_identity"]).plan_identity(entries()[0]),
                         p_correct=1., mode="strong", row_kind="production")
    source["sheet_data"]["rows"].append(normal["sheet_data"]["rows"][0])
    normal_check = deepcopy(normal["cross_check"]["rows"][0])
    normal_check["row_index"] = 1
    source["cross_check"]["rows"].append(normal_check)
    plan = entries()
    indexed = [{**r, "of": r["production_order_no"], "ov": r["sales_order_no"], "cliente": r["customer_name"],
                "cliente_nome": r["customer_name"], "perfil": r["profile_type"], "modelo": r["component_ref"],
                "comp_mm": r["length_mm"], "qtd_restante": r["remaining_quantity"], "falta_valida": True,
                "regra_calculo": r["remaining_rule"], "qtd_planeada": r["quantity_planned"], "qtd_feita": r["quantity_made"]} for r in plan]
    index = PlanIndex(indexed, loaders.PERFIS_SPEC if plan_review.IS_MTG2 else loaders.CANTONEIRAS_SPEC, snapshot_id="old")
    web.PROCESS_IN_BACKGROUND = False
    web.get_index = lambda loader: {} if loader == "load_employees" else index
    web.get_employees = lambda: {}
    web._load_header_machines = lambda: [{"display_name": "Vanguard"}, {"display_name": "FICEP"}]
    web.make_fresh_scorer = lambda _: Scorer(index, CrossParams())
    loaders._fetch = lambda *_: []
    loaders.load_active_ofs = lambda: set()
    loaders.plan_snapshot_info = lambda: {"snapshot_id": "old", "age_hours": 1.0}
    plan_review.fetch_order = lambda sid, of: deepcopy(plan)
    plan_review.fetch_keys = lambda keys, sid=None: [deepcopy(r) for r in plan if r["plan_key"] in keys]
    def lookup(sid, query, include_done=False, offset=0):
        found = (plan if include_done else plan[:2]) if query.upper() in {"42", "OF42", "21", "OV21", "REF"} else []
        return {"snapshot_id": sid, "entries": deepcopy(found), "found": bool(found), "has_more": False,
                "offset": offset, "mode": "of", "q": query}
    plan_review.lookup = lookup
    archive = []
    def store(sheet, template, edit_count, actor, **kwargs):
        frozen = deepcopy(sheet)
        frozen["status"] = "validated"
        archive.append(frozen)
        if plan_review.IS_MTG2:
            return pg_store.StoredSheetResult(len(sheet["sheet_data"]["rows"]), sheet["sheet_no"], sheet["sheet_no"] + 1, False)
        return len(sheet["sheet_data"]["rows"])
    pg_store.store_validated_sheet = store
    export_source.load_validated_sheets = lambda *args: deepcopy(archive)
    for name, sample in (("basedados-normal", normal), ("basedados-perfil-completo", sheet())):
        (args.output / f"{name}.xlsx").write_bytes(export_routes.workbook("basedados", [sample]))

    real_connect = db.connect
    with tempfile.TemporaryDirectory(prefix="kanban-review-browser-") as temp:
        temp_path = Path(temp)
        (temp_path / "images").mkdir()
        photo_source = next(iter(sorted(Path("data/images").glob("*Xerox*.png"))), next(Path("data/images").glob("*.png")))
        photo = temp_path / "images" / "reference.png"
        shutil.copyfile(photo_source, photo)
        web.settings = replace(web.settings, data_dir=temp_path, cross_engine="v3")
        db.connect = lambda path=None: real_connect(temp_path / "staging.db")
        conn = db.connect()
        uids = []
        for validated in (False, True):
            uid = db.create_sheet(conn, TEMPLATE, str(photo))
            db.set_extraction(conn, uid, source["sheet_data"])
            db.save_cross_check(conn, uid, source["cross_check"], db.get_sheet(conn, uid)["revision"])
            if validated:
                conn.execute("UPDATE sheets SET status='validated' WHERE uid=?", (uid,))
                conn.commit()
                archive.append(db.get_sheet(conn, uid))
            uids.append(uid)
        conn.close()
        # Reference rendering only; the original application and its database are untouched.
        env = Environment(loader=FileSystemLoader(args.original / "templates"), autoescape=select_autoescape())
        env.filters["pt_to_iso"] = lambda value: value
        env.filters["iso_to_pt"] = lambda value: value
        web.app.mount("/original-static", StaticFiles(directory=args.original / "static"), name="original-static")
        @web.app.get("/image/{uid}")
        def original_image(uid: str):
            return FileResponse(photo)
        @web.app.get("/original/{view}", response_class=HTMLResponse)
        def original(request: Request, view: str, width: int = 1440):
            data = source["sheet_data"]
            ref_sheet = {"id": 1, "status": "extracted", "captured_at": "09-09-2026 10:00", "operador": "ANA",
                         "template_name": TEMPLATE, "sheet_operador": "ANA"}
            values = dict(request=SimpleNamespace(url=SimpleNamespace(path="/queue" if view == "history" else "/sheet/1"),
                                                  state=SimpleNamespace(mobile=width < 768)),
                          sheet=ref_sheet, sheets=[ref_sheet, {**ref_sheet, "id": 2, "status": "validated"}],
                          header=data["header"], rows=data["rows"], footer=data["footer"],
                          header_fields=list(data["header"]), row_fields=["of", "ov", "cliente", "perfil", "modelo", "qtd", "perf_comp"],
                          footer_fields=["metros_produzidos", "horas_trabalhadas"], field_labels={}, cells_by_path={},
                          template=None, template_name=TEMPLATE, flagged_count=0, view_mode="final", back_url="/queue",
                          crop_meta={}, crop_info={}, status_filter="all", unidade_filter=None, unidades=[],
                          operadores=["ANA"], setores=["Vanguard"], cc_status_by_path={})
            html = env.get_template("queue.html" if view == "history" else "sheet.html").render(**values)
            html = html.replace('/static/', '/original-static/').replace('https://unpkg.com/htmx.org@2.0.4', '/static/vendor/htmx.min.js').replace('https://unpkg.com/alpinejs@3.13.10/dist/cdn.min.js', '/static/vendor/alpine.min.js')
            return HTMLResponse(html)

        with LiveTestClient(web.app) as client:
            manifest = {"base": str(client._client.base_url).rstrip('/'), "review": uids[0], "validated": uids[1],
                        "output": str(args.output.resolve()), "playwright": args.playwright, "source_app": plan_review.SOURCE_APP}
            manifest_path = temp_path / "browser.json"
            manifest_path.write_text(json.dumps(manifest))
            subprocess.run(["node", str(Path(__file__).with_name("verify_review_browser.cjs")), str(manifest_path)], check=True)
        db.connect = real_connect


if __name__ == "__main__":
    main()
