"""Validada logo, grava depois: o clique não toca no Postgres e o
sync_worker leva a folha ao histórico, com novas tentativas."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from app import db, pg_store, sync_worker
from app.web import main
from tests.test_web import client, create_sheet, edit  # noqa: F401


@pytest.fixture()
def background(client, monkeypatch):
    """Modo de produção: validar só grava no SQLite; o teste corre o
    trabalhador à mão (run_due) em vez da thread."""
    monkeypatch.setattr(main, "VALIDATION_MODE", "background")
    monkeypatch.setattr(sync_worker, "wake", lambda: None)
    monkeypatch.setattr(main, "settings", replace(main.settings, sync_delay_with_warnings_s=0))
    return client


def sheet(uid):
    conn = db.connect()
    try:
        return db.get_sheet(conn, uid)
    finally:
        conn.close()


def validated_sheet(client):
    uid = create_sheet(client)
    edit(client, uid, "rows[0].of", "OF250001")
    edit(client, uid, "header.operador", "João")
    edit(client, uid, "header.data", "06/08/2026")
    response = client.post(f"/sheet/{uid}/validate", data={"actor": "operador"})
    assert response.status_code == 303
    return uid, response.headers["location"]


def make_due(uid):
    conn = db.connect()
    try:
        conn.execute("UPDATE sheets SET sync_next_at = ? WHERE uid = ?",
                     ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), uid))
        conn.commit()
    finally:
        conn.close()


def test_validar_nao_toca_no_postgres_e_o_trabalhador_grava(background, monkeypatch):
    uid, location = validated_sheet(background)
    assert "a_gravar=1" in location and "erro=" not in location
    assert background.stored_calls == []
    before = sheet(uid)
    assert before["status"] == "validated" and before["sync_state"] == "pending"

    assert sync_worker.run_due() == 1
    after = sheet(uid)
    assert after["sync_state"] == "done" and after["synced_at"]
    assert len(background.stored_calls) == 1
    # A hora gravada no histórico é a do clique, não a da gravação.
    assert background.stored_calls[0]["validated_at"] == before["validated_at"]
    assert sync_worker.run_due() == 0, "uma folha gravada não volta a ir"


def test_validada_nao_aceita_edicoes_enquanto_espera(background):
    uid, _ = validated_sheet(background)
    edit(background, uid, "header.operador", "Intruso")
    assert sheet(uid)["sheet_data"]["header"]["operador"] == "João"


def test_falha_de_rede_volta_a_tentar_mais_tarde(background, monkeypatch):
    uid, _ = validated_sheet(background)
    real_store = pg_store.store_validated_sheet

    def offline(*_a, **_k):
        raise psycopg.OperationalError("túnel em baixo")

    monkeypatch.setattr(pg_store, "store_validated_sheet", offline)
    assert sync_worker.run_due() == 0
    waiting = sheet(uid)
    assert waiting["status"] == "validated" and waiting["sync_state"] == "retry"
    assert waiting["sync_attempts"] == 1 and "Sem ligação" in waiting["sync_error"]
    next_at = datetime.fromisoformat(waiting["sync_next_at"])
    assert timedelta(seconds=5) < next_at - datetime.now(timezone.utc) <= timedelta(seconds=10)
    assert sync_worker.run_due() == 0, "antes da hora marcada não se tenta"

    monkeypatch.setattr(pg_store, "store_validated_sheet", real_store)
    make_due(uid)
    assert sync_worker.run_due() == 1
    assert sheet(uid)["sync_state"] == "done" and sheet(uid)["sync_error"] is None


def test_espera_cresce_a_cada_falha(background, monkeypatch):
    uid, _ = validated_sheet(background)
    monkeypatch.setattr(pg_store, "store_validated_sheet",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("rede")))
    waits = []
    for _ in range(6):
        make_due(uid)
        before = datetime.now(timezone.utc)
        sync_worker.run_due()
        waits.append(round((datetime.fromisoformat(sheet(uid)["sync_next_at"]) - before)
                           .total_seconds()))
    expected = [10, 30, 120, 600, 900, 900]
    assert all(abs(w - e) <= 2 for w, e in zip(waits, expected)), waits
    assert sheet(uid)["sync_state"] == "retry"


def test_erro_que_nao_passa_a_tentar_fica_a_vista_com_tentar_de_novo(background, monkeypatch):
    uid, _ = validated_sheet(background)
    real_store = pg_store.store_validated_sheet
    monkeypatch.setattr(pg_store, "store_validated_sheet", lambda *a, **k: (_ for _ in ()).throw(
        pg_store.SheetIdentityConflict("uid de outra app")))
    sync_worker.run_due()
    failed = sheet(uid)
    assert failed["sync_state"] == "error" and "reconciliação" in failed["sync_error"]
    page = background.get(f"/sheet/{uid}")
    assert "Tentar de novo" in page.text and "Reabrir" in page.text
    home = background.get("/?status=a_gravar")
    assert "erro ao gravar" in home.text

    monkeypatch.setattr(pg_store, "store_validated_sheet", real_store)
    monkeypatch.setattr(main, "VALIDATION_MODE", "sync")
    background.post(f"/sheet/{uid}/sync-retry")
    assert sheet(uid)["sync_state"] == "done"


def test_folha_com_avisos_espera_e_pode_ser_reaberta(background, monkeypatch):
    monkeypatch.setattr(main, "settings", replace(main.settings, sync_delay_with_warnings_s=600))
    uid = create_sheet(background)
    edit(background, uid, "rows[0].of", "OF250001")
    edit(background, uid, "header.data", "06/08/2026")  # sem operador → aviso
    background.post(f"/sheet/{uid}/validate", data={"actor": "operador"})
    assert sheet(uid)["sync_state"] == "pending"
    assert sync_worker.run_due() == 0, "com avisos espera antes de ir para o histórico"
    assert "podes reabrir" in background.get(f"/sheet/{uid}").text

    monkeypatch.setattr(sync_worker, "_stored_in_postgres", lambda _uid: False)
    response = background.post(f"/sheet/{uid}/reopen")
    assert "erro=" not in response.headers["location"]
    reopened = sheet(uid)
    assert reopened["status"] == "in_review" and reopened["sync_state"] is None
    edit(background, uid, "header.operador", "Rui")
    assert sheet(uid)["sheet_data"]["header"]["operador"] == "Rui"
    assert background.stored_calls == []


def test_reabrir_recusado_se_o_historico_ja_tem_a_folha_ou_sem_ligacao(background, monkeypatch):
    uid, _ = validated_sheet(background)
    monkeypatch.setattr(sync_worker, "_stored_in_postgres", lambda _uid: True)
    response = background.post(f"/sheet/{uid}/reopen")
    assert "erro=" in response.headers["location"]
    assert sheet(uid)["status"] == "validated"

    def offline(_uid):
        raise psycopg.OperationalError("túnel")

    monkeypatch.setattr(sync_worker, "_stored_in_postgres", offline)
    response = background.post(f"/sheet/{uid}/reopen")
    assert "erro=" in response.headers["location"]
    assert sheet(uid)["status"] == "validated"

    sync_worker.run_due()
    response = background.post(f"/sheet/{uid}/reopen")
    assert "erro=" in response.headers["location"], "gravada no histórico não reabre"


def test_queda_entre_o_postgres_e_a_confirmacao_local_confirma_depois(background, monkeypatch):
    uid, _ = validated_sheet(background)
    real_confirm = db.confirm_sync
    monkeypatch.setattr(db, "confirm_sync", lambda *a, **k: (_ for _ in ()).throw(
        OSError("processo caiu")))
    sync_worker.run_due()
    assert sheet(uid)["sync_state"] == "retry"
    monkeypatch.setattr(db, "confirm_sync", real_confirm)
    make_due(uid)
    assert sync_worker.run_due() == 1
    assert sheet(uid)["sync_state"] == "done"


def test_duas_pendentes_a_disputar_o_mesmo_numero(background, monkeypatch):
    first, _ = validated_sheet(background)
    second, _ = validated_sheet(background)
    a, b = sheet(first)["sheet_no"], sheet(second)["sheet_no"]
    assert b == a + 1
    # O histórico já tem o número provisório da primeira: dá-lhe o da segunda.
    numbers = {first: (b, b + 1), second: (b + 1, b + 2)}
    monkeypatch.setattr(pg_store, "store_validated_sheet", lambda s, *x, **k:
                        pg_store.StoredSheetResult(1, *numbers[s["uid"]], False))
    assert sync_worker.run_due() == 2
    assert sheet(first)["sheet_no"] == b and sheet(first)["sync_state"] == "done"
    assert sheet(second)["sheet_no"] == b + 1 and sheet(second)["sync_state"] == "done"


def test_saldo_que_falhou_por_falta_de_ligacao_e_completado_antes_de_gravar(background, monkeypatch):
    uid, _ = validated_sheet(background)
    conn = db.connect()
    try:
        s = db.get_sheet(conn, uid)
        cross = s["cross_check"]
        cross["rows"][0]["quantity_basis"] = {"status": "unavailable", "transient": True}
        cross["validation_warnings"].append({"code": "saldo_por_confirmar", "message": "x",
                                             "row": 1, "row_index": 0})
        conn.execute("UPDATE sheets SET cross_check = ? WHERE uid = ?",
                     (__import__("json").dumps(cross), uid))
        conn.commit()
    finally:
        conn.close()

    calls = []

    def still_down(sheet_, data, cross, **kwargs):
        calls.append(kwargs["complete_validated"])

    monkeypatch.setattr(sync_worker.historical_quantities, "apply", still_down)
    sync_worker.run_due()
    assert calls == [True]
    assert sheet(uid)["sync_state"] == "retry" and background.stored_calls == []

    def back_up(sheet_, data, cross, **kwargs):
        cross["rows"][0]["quantity_basis"] = {"status": "ready", "approximate": False}

    monkeypatch.setattr(sync_worker.historical_quantities, "apply", back_up)
    make_due(uid)
    assert sync_worker.run_due() == 1
    local = sheet(uid)["cross_check"]
    stored = background.stored_calls[0]["sheet"]["cross_check"]
    assert local["rows"][0]["quantity_basis"]["status"] == "ready"
    assert stored["rows"][0]["quantity_basis"]["status"] == "ready"
    assert "saldo_por_confirmar" not in {w["code"] for w in stored["validation_warnings"]}


def test_health_mostra_a_fila_e_exportacao_inclui_as_por_gravar(background, monkeypatch):
    uid, _ = validated_sheet(background)
    health = background.get("/health/sync").json()
    assert health["pending"] == 1 and health["oldest_validated_at"]
    from app.web import export_routes
    sheets = export_routes.export_sheets(main._conn)
    assert [s["uid"] for s in sheets] == [uid]
    sync_worker.run_due()
    assert background.get("/health/sync").json()["pending"] == 0


def test_trabalhador_em_thread_grava_sozinho(background, monkeypatch):
    import threading
    uid, _ = validated_sheet(background)
    done = threading.Event()
    real_confirm = db.confirm_sync

    def confirm(*a, **k):
        result = real_confirm(*a, **k)
        done.set()
        return result

    monkeypatch.setattr(db, "confirm_sync", confirm)
    sync_worker._stop.clear()
    worker = threading.Thread(target=sync_worker._loop, daemon=True)
    worker.start()
    try:
        assert done.wait(5)
    finally:
        sync_worker._stop.set()
        sync_worker._wake.set()
        worker.join(5)
    assert sheet(uid)["sync_state"] == "done"
