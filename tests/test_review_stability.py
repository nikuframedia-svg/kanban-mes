"""Reference consultation, review jobs and HTML errors on disposable stores."""
from copy import deepcopy
import threading
import time
import pytest
from fastapi import FastAPI
from app import db
from app.web import main, plan_review
from app.web.automatic_review import AutomaticReview, run_stages
from app.templates_spec import get_template
from tests.test_web import client
from tests.test_plan_review_flow import entries, sheet, insert_local, TEMPLATE, OTHER_PROFILE


def pending():
    source = sheet()
    source["status"] = "in_review"
    source["cross_check"]["rows"][0].update(quantity_basis={"status": "unavailable", "version": 1},
        plan_refs=[], plan_refs_error="Não existe plano guardado antes do dia de produção.")
    return source


def catalog(monkeypatch):
    items = entries() + [{**entries()[0], "plan_key": "other", "component_ref": "OTHER", "profile_type": OTHER_PROFILE}]
    monkeypatch.setattr(plan_review.loaders, "plan_snapshot_info", lambda: {"snapshot_id": "current"})
    monkeypatch.setattr(plan_review, "fetch_order", lambda snapshot, of: deepcopy(items))
    return items


def test_unavailable_history_still_shows_profile_closed_zero_and_entire_order(client, monkeypatch):
    catalog(monkeypatch)
    source = pending(); before = deepcopy(source)
    ctx = plan_review.context(source, 0, get_template(TEMPLATE), "/?page=2")
    assert len(ctx["linhas"]) == 3
    assert ctx["linhas"][2]["closed_x"] and ctx["linhas"][2]["remaining_quantity"] == 0
    assert all("made_in_sheet" not in item for item in ctx["linhas"])
    assert not ctx["production_values"] and ctx["consultation_only"]
    assert len(plan_review.context(source, 0, get_template(TEMPLATE), "/", "of")["linhas"]) == 4
    assert source == before
    uid = insert_local(source)
    response = client.get(f"/sheet/{uid}/plano/0?back=/%3Fpage%3D2")
    assert response.status_code == 200
    assert 'id="tabela-plano"' in response.text and "REF-2" in response.text
    assert "Não existe plano" in response.text and "apenas consulta" in response.text
    assert "a ser verificado" not in response.text
    assert "dados guardados na validação" not in response.text
    assert "Feita nesta folha" not in response.text
    assert "Ver toda a OF" in response.text
    assert "OTHER" in client.get(f"/sheet/{uid}/plano/0?scope=of").text
    assert client.get(f"/sheet/{uid}/plano/0?scope=bad").status_code == 422


def test_ready_history_and_order_share_same_snapshot(monkeypatch):
    source = sheet(); source["status"] = "in_review"
    source["cross_check"]["rows"][0]["quantity_basis"] = {"status": "ready", "snapshot_id": "old", "date": "2026-09-17"}
    monkeypatch.setattr(plan_review.loaders, "plan_snapshot_info", lambda: pytest.fail("must never use current"))
    calls = []
    def fetch(sid, of):
        calls.append((sid, of))
        return entries() + [{**entries()[0], "plan_key": "elsewhere", "profile_type": OTHER_PROFILE}]
    monkeypatch.setattr(plan_review, "fetch_order", fetch)
    ctx = plan_review.context(source, 0, get_template(TEMPLATE), "/", "of")
    assert calls == [("old", "42")]
    assert [line.get("made_in_sheet") for line in ctx["linhas"]] == [5, 2, 0, None]
    assert ctx["totais"]["nesta_folha"] == 7


def test_partial_archived_line_lists_all_profile_references_without_mutation(monkeypatch):
    source = sheet(full=False); before = deepcopy(source)
    monkeypatch.setattr(plan_review.loaders, "plan_snapshot_info", lambda: pytest.fail("no current"))
    monkeypatch.setattr(plan_review, "fetch_order", lambda sid, of: entries() if sid == "old" else [])
    assert len(plan_review.context(source, 0, get_template(TEMPLATE), "/")["linhas"]) == 3
    assert source == before


def test_missing_html_preserves_back_but_data_routes_stay_json(client):
    response = client.get("/sheet/absent?back=/%3Fpage%3D2%26status%3Din_review")
    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]
    assert 'href="/?page=2&amp;status=in_review"' in response.text
    assert response.headers["x-request-id"] in response.text
    assert "Voltar à lista" in response.text
    assert "Not Found" not in response.text
    for suffix in ("automatic-review", "recovery-state"):
        response = client.get("/sheet/absent/" + suffix)
        assert response.status_code == 404
        assert "application/json" in response.headers["content-type"]
    malicious = client.get("/sheet/absent?back=https://example.org")
    assert 'href="/"' in malicious.text and 'href="https://example.org"' not in malicious.text


def setup_job(tmp_path, process):
    path = tmp_path / "automatic.sqlite"
    connect = lambda: db.connect(path)
    with connect() as conn:
        uid = db.create_sheet(conn, TEMPLATE)
        db.set_extraction(conn, uid, {"header": {}, "rows": [{"of": "42"}], "footer": {}})
        revision = db.get_sheet(conn, uid)["revision"]
    service = AutomaticReview(FastAPI(), connect, lambda *_: True, process)
    return service, connect, uid, revision


def terminal(service, uid):
    for _ in range(300):
        job = service.jobs.get(uid, {})
        if job.get("status") not in {"queued", "running"}:
            return job
        time.sleep(.01)
    pytest.fail("job never finished")


def save_value(conn, uid, value):
    current = db.get_sheet(conn, uid)
    data = current["sheet_data"]; data["rows"][0]["of"] = value
    return db.save_sheet_data_with_edits(conn, uid, data, current["revision"], [("rows[0].of", "42", value, "system", "test")])


def test_partial_failure_runs_other_stages_and_deduplicates_final_revision(tmp_path):
    def process(conn, uid, revision):
        def partial(sheet):
            assert save_value(conn, uid, "43")
            raise RuntimeError("simulated coverage failure")
        return run_stages(conn, uid, [("coverage", partial), ("cross_history", lambda sheet: save_value(conn, uid, "44"))])
    service, connect, uid, revision = setup_job(tmp_path, process)
    service.enqueue(uid, revision)
    job = terminal(service, uid)
    assert job["status"] == "error" and job["initial_revision"] == revision
    assert job["final_revision"] == revision + 2
    assert job["stages"]["coverage"]["status"] == "error"
    assert job["stages"]["cross_history"]["status"] == "complete"
    with connect() as conn:
        current = db.get_sheet(conn, uid)
        assert current["sheet_data"]["rows"][0]["of"] == "44"
        assert not service.needed(conn, current)
    assert service.enqueue(uid, revision + 2)["job_id"] == job["job_id"]


def test_two_tabs_join_one_job_and_concurrent_write_stops_remaining_stages(tmp_path):
    reached, release = threading.Event(), threading.Event()
    ran = []
    def process(conn, uid, revision):
        def first(sheet):
            save_value(conn, uid, "43")
            reached.set(); assert release.wait(5)
            return True
        return run_stages(conn, uid, [("coverage", first), ("cross_history", lambda sheet: ran.append("cross"))])
    service, connect, uid, revision = setup_job(tmp_path, process)
    original = service.enqueue(uid, revision)
    assert reached.wait(5)
    assert service.enqueue(uid, revision)["job_id"] == original["job_id"]
    with connect() as other:
        save_value(other, uid, "HUMAN")
    release.set()
    job = terminal(service, uid)
    assert job["status"] == "conflict" and not ran
    assert job["final_revision"] == revision + 1
    with connect() as conn:
        current = db.get_sheet(conn, uid)
        assert current["sheet_data"]["rows"][0]["of"] == "HUMAN"
        assert not service.needed(conn, current)


def test_recheck_cannot_adopt_concurrent_revision(tmp_path):
    attempted = []
    def process(conn, uid, revision):
        def human_edit():
            with connect() as other:
                save_value(other, uid, "HUMAN")
        editor = threading.Thread(target=human_edit)
        editor.start(); editor.join(5)
        assert not editor.is_alive()
        # Even a service that re-reads the latest revision before CAS must not
        # silently adopt a revision written by another tab.
        attempted.append(save_value(conn, uid, "AUTOMATIC"))
    service, connect, uid, revision = setup_job(tmp_path, process)
    service.enqueue(uid, revision)
    assert terminal(service, uid)["status"] == "conflict"
    assert not attempted
    with connect() as conn:
        assert db.get_sheet(conn, uid)["sheet_data"]["rows"][0]["of"] == "HUMAN"
