"""Run with PLAYWRIGHT_MODULE pointing to an installed Playwright package."""
import json
import os
import subprocess
from pathlib import Path
import pytest
from app import db
from app.web import main, plan_review
from tests.test_web import client
from tests.test_review_stability import pending, catalog, insert_local


@pytest.mark.skipif(not os.environ.get("PLAYWRIGHT_MODULE"), reason="Playwright browser check is opt-in")
def test_browser_review_does_not_navigate_or_replace_drafts(client, monkeypatch, tmp_path):
    catalog(monkeypatch)
    service = main.coverage_routes.automatic if plan_review.IS_MTG2 else main.header_recovery_routes.automatic
    monkeypatch.setattr(service, "eligible", lambda *_: True)
    source = pending()
    for row in source["cross_check"]["rows"]:
        row.update(p_correct=1.0, mode="strong", cells=[])
    uid = insert_local(source)
    path = f"/sheet/{uid}?back=/%3Fpage%3D2"
    response = client.get(path)
    assert response.status_code == 200
    initial = response.text
    with db.connect() as conn:
        sheet = db.get_sheet(conn, uid)
        data = sheet["sheet_data"]; data["header"]["operador"] = "ATUALIZADO"
        db.save_sheet_data_with_edits(conn, uid, data, sheet["revision"], [])
        revision = db.get_sheet(conn, uid)["revision"]
    config = tmp_path / "browser.json"
    config.write_text(json.dumps({"url": str(client._client.base_url).rstrip("/") + path,
                                 "initial": initial, "revision": revision}))
    subprocess.run(["node", str(Path(__file__).with_suffix(".cjs")), str(config)], check=True, timeout=90)
