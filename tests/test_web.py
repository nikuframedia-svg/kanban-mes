"""Testes da camada web: fluxo completo capturar → editar → cruzar → validar,
com staging SQLite temporário, índice do plano sintético e Postgres simulado.
O caminho real para o Postgres é coberto pelo teste E2E manual (não aqui)."""

from concurrent.futures import ThreadPoolExecutor
import time

import pytest
from app import db, pg_store
from app.matching.loaders import CANTONEIRAS_SPEC
from app.matching.refs import PlanIndex
from app.web import main
from tests.live_client import LiveTestClient


def make_index() -> PlanIndex:
    entries = [
        {"snapshot_id": "test-snapshot", "plan_key": "P1", "of": "OF250001", "ov": "OV2400001",
         "cliente": "SILVA & VINHA SA", "modelo": "L50X50X5", "comp_mm": 1500},
        {"snapshot_id": "test-snapshot", "plan_key": "P2", "of": "OF250002", "ov": "OV2400002",
         "cliente": "PROEF EURICO FERREIRA", "modelo": "L60X60X6", "comp_mm": 2000},
    ]
    return PlanIndex(entries, CANTONEIRAS_SPEC, plan_age_days=1.0,
                     snapshot_id="test-snapshot")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    real_connect = db.connect
    monkeypatch.setattr(db, "connect", lambda path=None: real_connect(tmp_path / "test.db"))
    monkeypatch.setattr(main, "get_index", lambda loader_name: make_index())
    monkeypatch.setattr(main.loaders, "load_active_ofs", lambda: set())
    monkeypatch.setattr(
        main.loaders, "plan_snapshot_info",
        lambda: {"snapshot_id": "test-snapshot", "age_hours": 1.0},
    )

    stored_calls: list[dict] = []
    from copy import deepcopy
    from app.web import export_source

    def archive(de="", ate="", operador=""):
        result = []
        for call in stored_calls:
            sheet = deepcopy(call["sheet"])
            header = sheet["sheet_data"]["header"]
            date = pg_store.normalize_sheet_date(header.get("data"))
            if (de and date < de) or (ate and date > ate):
                continue
            if operador and header.get("operador") != operador:
                continue
            sheet["status"] = "validated"
            result.append(sheet)
        return result
    monkeypatch.setattr(export_source, "load_validated_sheets", archive)

    def fake_store(sheet, template, edit_count, actor, **kwargs):
        stored_calls.append({"sheet": sheet, "template": template,
                             "edit_count": edit_count, "actor": actor,
                             "validated_at": kwargs.get("validated_at")})
        rows = (sheet["sheet_data"] or {}).get("rows") or []
        count = sum(1 for r in rows
                    if any(v is not None and str(v).strip() != "" for v in r.values()))
        return pg_store.StoredSheetResult(
            count, sheet["sheet_no"], sheet["sheet_no"] + 1, False)

    monkeypatch.setattr(pg_store, "store_validated_sheet", fake_store)
    monkeypatch.setattr(main, "PROCESS_IN_BACKGROUND", False)  # determinístico
    with LiveTestClient(main.app, follow_redirects=False) as c:
        c.stored_calls = stored_calls
        yield c


def create_sheet(client) -> str:
    r = client.post("/upload", data={"template_name": "cantoneiras_kanban"})
    assert r.status_code == 303
    return r.headers["location"].rsplit("/", 1)[1]


def test_current_index_reuses_snapshot_and_rebuilds_once(monkeypatch):
    snapshots = ["s1"]
    builds = []

    def load(snapshot_id=None):
        builds.append(snapshot_id)
        time.sleep(0.01)
        return PlanIndex([], CANTONEIRAS_SPEC, snapshot_id=snapshot_id)

    monkeypatch.setattr(main.loaders, "load_cantoneiras_index", load)
    monkeypatch.setattr(
        main, "_current_index_snapshot",
        lambda _loader, strict=False: snapshots[0],
    )
    with main._index_lock:
        main._index_cache.clear()
        main._index_build_locks.clear()
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            first = list(pool.map(
                lambda _: main.get_index(
                    "load_cantoneiras_index", require_current=True,
                ),
                range(6),
            ))
        assert len({id(index) for index in first}) == 1
        assert builds == ["s1"]
        assert main.get_index(
            "load_cantoneiras_index", require_current=True,
        ) is first[0]
        assert builds == ["s1"]
        snapshots[0] = "s2"
        updated = main.get_index(
            "load_cantoneiras_index", require_current=True,
        )
        assert updated.snapshot_id == "s2"
        assert builds == ["s1", "s2"]
    finally:
        with main._index_lock:
            main._index_cache.clear()
            main._index_build_locks.clear()


def get_revision(client, uid) -> int:
    conn = db.connect()
    try:
        return db.get_sheet(conn, uid)["revision"]
    finally:
        conn.close()


def edit(client, uid, field_path, value):
    r = client.post(f"/sheet/{uid}/edit", data={
        "field_path": field_path, "value": value,
        "revision": get_revision(client, uid), "actor": "teste",
    })
    return r


def test_field_path_hostil_e_rejeitado(client):
    """Endpoint público sem auth: um índice gigante criava 50M de dicts (OOM
    e queda do processo); um negativo corrompia a proteção de células humanas."""
    uid = create_sheet(client)
    for bad in ("rows[50000000].of", "rows[-1].of", "rows[abc].of",
                "x", "header.<script>", "rows[1].of; DROP",
                "rows[0]._plan_binding"):
        r = client.post(f"/sheet/{uid}/edit", data={
            "field_path": bad, "value": "x",
            "revision": get_revision(client, uid), "actor": "t"})
        assert r.status_code in (400, 422), bad


def test_scripts_sao_self_hosted(client):
    """htmx/Alpine de /static/vendor, não do unpkg: sem CDN alcançável os
    cliques da app morriam em silêncio."""
    r = client.get("/")
    assert "/static/vendor/htmx.min.js" in r.text
    assert "unpkg.com" not in r.text


def test_cross_obrigatorio_materializa_sem_link_de_proposta(client):
    uid = create_sheet(client)
    edit(client, uid, "rows[0].of", "OF250001")   # linha ligada, OV por preencher
    r = client.get(f"/sheet/{uid}")
    assert "2400001" in r.text
    conn = db.connect()
    try:
        assert db.get_sheet(conn, uid)["sheet_data"]["rows"][0]["ov"] == "2400001"
    finally:
        conn.close()


def test_reocr_com_edicoes_humanas_exige_force(client, tmp_path, monkeypatch):
    import dataclasses

    from PIL import Image, ImageDraw
    monkeypatch.setattr(main, "settings",
                        dataclasses.replace(main.settings, data_dir=tmp_path))
    im = Image.new("RGB", (400, 300), "white")
    d = ImageDraw.Draw(im)
    for y in range(20, 280, 30):
        d.line([(10, y), (390, y)], fill="black", width=3)
    im.save(tmp_path / "f.png")

    class Fake:
        name = "fake"

        def extract_auto(self, image_path, templates):
            t = templates["producao"]
            return "producao", {
                "header": {f: None for f in t.header_fields},
                "rows": [dict.fromkeys(t.row_fields) | {"of": "lido"}],
                "footer": {f: None for f in t.footer_fields}}

    monkeypatch.setattr(main, "get_provider", lambda: Fake())
    r = client.post("/upload", data={"template_name": "cantoneiras_kanban"},
                    files=[("photos", ("f.png", (tmp_path / "f.png").read_bytes(),
                                       "image/png"))])
    uid = r.headers["location"].rsplit("/", 1)[1]

    edit(client, uid, "rows[0].of", "CORRIGIDO-A-MAO")
    assert client.post(f"/sheet/{uid}/reocr").status_code == 409, \
        "re-ler por cima de correções manuais exige intenção explícita"
    assert client.post(f"/sheet/{uid}/reocr?force=1").status_code == 303


def test_worker_nao_esmaga_folha_em_revisao(client, tmp_path, monkeypatch):
    """O humano começou a editar enquanto o OCR corria: set_extraction tem de
    recusar (status já não é pending) e o trabalho humano fica intacto."""
    uid = create_sheet(client)                    # extracted (manual)
    edit(client, uid, "rows[0].of", "TRABALHO-HUMANO")
    conn = db.connect()
    try:
        ok = db.set_extraction(conn, uid, {"header": {}, "rows": [{"of": "OCR"}],
                                           "footer": {}})
        sheet = db.get_sheet(conn, uid)
    finally:
        conn.close()
    assert not ok
    assert sheet["sheet_data"]["rows"][0]["of"] == "TRABALHO-HUMANO"


def test_historico_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Folhas Kanban" in r.text
    assert "Exportar CPIS" in r.text
    assert 'action="/export/cpis"' in r.text


def test_captura_page_has_upload_form(client):
    r = client.get("/captura")
    assert r.status_code == 200
    assert "cantoneiras_kanban" in r.text
    assert 'action="/upload"' in r.text
    camera = client.get("/captura/camara")
    assert camera.status_code == 200
    assert "if (!r.ok)" in camera.text
    assert 'window.addEventListener("pagehide", stopCamera)' in camera.text


def test_back_e_seguro_e_sobrevive_acoes_da_folha(client):
    uid = create_sheet(client)
    back = "/?status=pending&operador=Silva%20%26%20Vinha&page=2"
    page = client.get(f"/sheet/{uid}", params={"back": back})
    assert 'class="btn ghost sheet-back"' in page.text
    assert 'name="back" value="/?status=pending&amp;operador=Silva%20%26%20Vinha&amp;page=2"' in page.text

    r = client.post(f"/sheet/{uid}/recheck", data={"back": back})
    assert r.status_code == 303
    assert r.headers["location"] == (
        f"/sheet/{uid}?back=%2F%3Fstatus%3Dpending%26operador%3DSilva%2520%2526%2520Vinha%26page%3D2"
    )

    hostile = client.get(f"/sheet/{uid}", params={"back": "//evil.example"})
    assert 'class="btn ghost sheet-back" href="/"' in hostile.text
    assert "evil.example" not in hostile.text


def test_old_routes_redirect(client):
    r = client.get("/capture")
    assert r.status_code == 301 and r.headers["location"] == "/captura"
    r = client.get("/dashboard")
    assert r.status_code == 301 and r.headers["location"] == "/estado"


def test_upload_creates_sheet_and_review_screen_renders(client):
    uid = create_sheet(client)
    r = client.get(f"/sheet/{uid}")
    assert r.status_code == 200
    assert "Cantoneiras" in r.text
    assert "Validar folha" in r.text


def test_header_cross_is_nested_and_renders_exact_labels(client):
    uid = create_sheet(client)
    conn = db.connect()
    try:
        cross = db.get_sheet(conn, uid)["cross_check"]
    finally:
        conn.close()

    assert set(cross["header"]["cells"]) == {
        "operador", "n_operador", "setor_maquina", "data", "turno",
    }
    r = client.get(f"/sheet/{uid}")
    assert r.status_code == 200
    for label in ("Operador", "N.º operador", "Setor/Máquina", "Data", "Turno"):
        assert label in r.text
    assert "header-field-operador cc-na" in r.text


def test_header_cross_auto_substitui_propostas_unicas_e_fica_verde(
        client, monkeypatch):
    from app.matching.operador import Employee

    monkeypatch.setattr(main, "get_employees", lambda: {
        3480: Employee(3480, "10003480", "GURPINDER SINGH"),
    })
    monkeypatch.setattr(main, "_load_header_machines", lambda: [
        {"display_name": "Ficep Rapid 20T -1"},
        {"display_name": "Ficep Rapid 20T -2"},
        {"display_name": "Ficep XP T4"},
    ])
    raw = {
        "header": {
            "operador": "Gurpinder", "n_operador": "3480",
            "setor_maquina": "Rapid 20T - 2", "data": "15-08-26", "turno": "m",
        },
        "rows": [], "footer": {"metros_produzidos": None, "horas_trabalhadas": None},
    }
    conn = db.connect()
    try:
        uid = db.create_sheet(
            conn, "cantoneiras_kanban",
            image_path="data/images/0123456789abcdef_18-08-2026_p2.png",
            image_sha256="teste-header",
        )
        assert db.set_extraction(conn, uid, raw)
        main.run_cross_check(conn, uid)
        sheet = db.get_sheet(conn, uid)
    finally:
        conn.close()

    assert sheet["sheet_data"]["header"] == {
        "operador": "GURPINDER SINGH", "n_operador": "3480",
        "setor_maquina": "Ficep Rapid 20T -2",
        "data": "17/08/2026", "turno": "M",
    }
    header_cross = sheet["cross_check"]["header"]
    assert header_cross["source_document"]["filename"] == "18-08-2026.pdf"
    assert header_cross["source_document"]["page"] == 2
    assert header_cross["cells"]["operador"]["status"] == "confirmed"
    assert header_cross["cells"]["operador"]["applied"] is True
    assert header_cross["cells"]["data"]["applied"] is True
    assert header_cross["cells"]["data"]["proposal"] is None
    assert header_cross["cells"]["data"]["reason"] == "assumed_prev_business_day"
    assert header_cross["cells"]["setor_maquina"]["applied"] is True
    assert header_cross["cells"]["setor_maquina"]["proposal"] is None
    # Depois da canonicalização, a identidade final volta a cruzar como exata.
    assert sheet["cross_check"]["operator"]["rule"] == "exact"
    assert sheet["cross_check"]["operator"]["pernr"] == "10003480"

    r = client.get(f"/sheet/{uid}")
    assert "header-field-data cc-match" in r.text
    assert "17/08/2026" in r.text
    assert "substituído automaticamente" in r.text
    assert "18-08-2026.pdf" in r.text
    assert "não define a data da folha" in r.text


def test_plan_failure_does_not_block_header_cross(client, monkeypatch):
    monkeypatch.setattr(main, "make_scorer", lambda _name: (_ for _ in ()).throw(
        RuntimeError("plano offline")
    ))
    raw = {
        "header": {
            "operador": None, "n_operador": None, "setor_maquina": None,
            "data": "15-08-26", "turno": "m",
        },
        "rows": [], "footer": {"metros_produzidos": None, "horas_trabalhadas": None},
    }
    conn = db.connect()
    try:
        uid = db.create_sheet(conn, "cantoneiras_kanban")
        assert db.set_extraction(conn, uid, raw)
        main.run_cross_check(conn, uid)
        sheet = db.get_sheet(conn, uid)
    finally:
        conn.close()

    cross = sheet["cross_check"]
    assert cross["plan_reference"]["status"] == "no_reference"
    assert cross["rows"] == []
    # folha sem PDF de origem: a data assumida parte do created_at (UTC)
    import datetime as dt
    esperado = main.header_cross.previous_business_day(
        dt.datetime.now(dt.timezone.utc).date()).strftime("%d/%m/%Y")
    assert sheet["sheet_data"]["header"]["data"] == esperado
    assert sheet["sheet_data"]["header"]["turno"] == "M"
    assert cross["header"]["cells"]["data"]["applied"] is True
    assert cross["header"]["cells"]["turno"]["applied"] is True
    assert "Plano indisponível" in client.get(f"/sheet/{uid}").text


def test_edit_row_triggers_cross_check_with_colours(client):
    uid = create_sheet(client)
    assert edit(client, uid, "rows[0].of", "OF250001").status_code == 303
    assert edit(client, uid, "rows[0].comp_mm", "1500").status_code == 303
    r = client.get(f"/sheet/{uid}")
    assert r.status_code == 200
    # a linha cruzou contra o plano sintético e ganhou o P1
    # (classes de cor do design system: confirmed → cc-match, snapped → cc-warn)
    assert "cc-match" in r.text or "cc-warn" in r.text
    conn = db.connect()
    try:
        sheet = db.get_sheet(conn, uid)
    finally:
        conn.close()
    cross = sheet["cross_check"]
    assert cross["rows"][0]["matched_plan_key"] == "P1"


def test_substituicao_marca_applied_e_mostra_o_original_do_ocr(client):
    """Depois de o motor aplicar uma proposta, a célula recalculada descreve o
    valor GRAVADO (applied + mensagem), e a revisão mostra o que o OCR leu
    numa linha própria por baixo da célula — nunca escondido num tooltip."""
    uid = create_sheet(client)
    # linha ligada ao P1: a OV vazia é âncora da obra e é auto-preenchida
    edit(client, uid, "rows[0].of", "OF250001")
    conn = db.connect()
    try:
        sheet = db.get_sheet(conn, uid)
    finally:
        conn.close()
    assert sheet["sheet_data"]["rows"][0]["ov"] == "2400001"
    cells = {c["field"]: c for c in sheet["cross_check"]["rows"][0]["cells"]}
    ov = cells["ov"]
    assert ov["applied"] is True
    assert ov["status"] == "confirmed", "a célula final descreve o valor gravado"
    assert ov["message"].startswith("Substituído automaticamente.")

    r = client.get(f"/sheet/{uid}")
    assert 'class="ocr-original"' in r.text, "o original do OCR aparece por baixo"


def test_recheck_preserva_valores_substituidos_no_payload_final(client):
    uid = create_sheet(client)
    edit(client, uid, "rows[0].of", "OF250001")
    conn = db.connect()
    try:
        first = db.get_sheet(conn, uid)["cross_check"]["rows"][0]
    finally:
        conn.close()
    assert first["replaced_values"]["of"] == {
        "old": "OF250001", "new": "250001",
    }

    assert client.post(f"/sheet/{uid}/recheck").status_code == 303
    conn = db.connect()
    try:
        final = db.get_sheet(conn, uid)["cross_check"]["rows"][0]
    finally:
        conn.close()
    assert final["replaced_values"]["of"] == first["replaced_values"]["of"]
    assert final["selected_snapshot_id"] == "test-snapshot"


def test_edit_with_stale_revision_preserva_valor_para_confirmar(client):
    uid = create_sheet(client)
    current_revision = get_revision(client, uid)
    r = client.post(f"/sheet/{uid}/edit", data={
        "field_path": "rows[0].of", "value": "OF250001",
        "revision": 999, "actor": "teste",
    })
    assert r.status_code == 409
    assert "Não foi possível guardar" in r.text
    assert "confirma novamente este valor" in r.text
    assert 'name="value"' in r.text and 'value="OF250001"' in r.text
    assert f'name="revision" value="{current_revision}"' in r.text

    # O primeiro POST não atropela a revisão nova. O formulário devolvido já
    # contém essa revisão e confirma o mesmo texto num segundo POST.
    conn = db.connect()
    try:
        assert db.get_sheet(conn, uid)["sheet_data"]["rows"][0]["of"] is None
    finally:
        conn.close()
    assert client.post(f"/sheet/{uid}/edit", data={
        "field_path": "rows[0].of", "value": "OF250001",
        "revision": current_revision, "actor": "teste",
    }).status_code == 303


def test_edit_preserva_valor_se_o_cross_falhar(client, monkeypatch):
    uid = create_sheet(client)

    def broken_cross(*_args, **_kwargs):
        raise RuntimeError("falha sintética do cross")

    monkeypatch.setattr(main, "run_cross_check", broken_cross)
    response = client.post(f"/sheet/{uid}/edit", data={
        "field_path": "rows[0].of", "value": "OF250001",
        "revision": get_revision(client, uid), "actor": "teste",
    })
    assert response.status_code == 303
    assert "erro=" in response.headers["location"]
    assert "erro_context=edit" in response.headers["location"]
    conn = db.connect()
    try:
        assert db.get_sheet(conn, uid)["sheet_data"]["rows"][0]["of"] == "OF250001"
    finally:
        conn.close()
    page = client.get(response.headers["location"])
    assert page.status_code == 200
    assert "Não foi possível guardar" in page.text
    assert "A edição ficou preservada" in page.text


def test_header_form_guarda_tudo_uma_vez_e_preserva_draft_no_conflito(
    client, monkeypatch
):
    uid = create_sheet(client)
    monkeypatch.setattr(main, "get_employees", lambda: {})
    calls = []
    real_cross = main.run_cross_check

    def counted_cross(conn, sheet_uid, **kwargs):
        calls.append(sheet_uid)
        return real_cross(conn, sheet_uid, **kwargs)

    monkeypatch.setattr(main, "run_cross_check", counted_cross)
    revision = get_revision(client, uid)
    payload = {
        "operador": "Ana Silva", "n_operador": "42",
        "setor_maquina": "Rapid 20T - 1", "data": "31/08/2026",
        "turno": "M", "revision": revision, "actor": "teste", "reason": "out_of_scope",
        "back": "/?status=pending&page=2",
    }
    response = client.post(f"/sheet/{uid}/header", data=payload)
    assert response.status_code == 303
    assert calls == [uid], "o cabeçalho integral dispara um único cross"

    conn = db.connect()
    try:
        sheet = db.get_sheet(conn, uid)
        edits = conn.execute(
            "SELECT field_path FROM edits WHERE sheet_uid = ? AND source = 'human' "
            "ORDER BY field_path", (uid,),
        ).fetchall()
    finally:
        conn.close()
    assert sheet["sheet_data"]["header"] == {
        "operador": "Ana Silva", "n_operador": "42",
        "setor_maquina": "Rapid 20T - 1", "data": "31/08/2026",
        "turno": "M",
    }
    assert [row[0] for row in edits] == [
        "header.data", "header.n_operador", "header.operador",
        "header.setor_maquina", "header.turno",
    ]

    stale = {**payload, "operador": "Rascunho Preservado", "turno": "T"}
    conflict = client.post(f"/sheet/{uid}/header", data=stale)
    assert conflict.status_code == 409
    assert conflict.headers["content-type"].startswith("text/html")
    assert "A folha foi alterada; confirma novamente os valores" in conflict.text
    assert 'value="Rascunho Preservado"' in conflict.text
    assert 'value="T"' in conflict.text
    assert "const headerConflict = true" in conflict.text


def test_header_fica_guardado_quando_o_cross_lanca_excecao(
    client, monkeypatch
):
    uid = create_sheet(client)
    monkeypatch.setattr(
        main, "run_cross_check",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("falha sintética do cross")
        ),
    )
    response = client.post(f"/sheet/{uid}/header", data={
        "operador": "Ana", "n_operador": "42",
        "setor_maquina": "Rapid 20T - 1", "data": "31/08/2026",
        "turno": "M", "revision": get_revision(client, uid),
        "actor": "teste",
    })
    assert response.status_code == 303
    assert "erro=" in response.headers["location"]
    assert "erro_context=edit" in response.headers["location"]
    conn = db.connect()
    try:
        assert db.get_sheet(conn, uid)["sheet_data"]["header"]["operador"] == "Ana"
    finally:
        conn.close()
    page = client.get(response.headers["location"])
    assert "Não foi possível guardar" in page.text
    assert "Os valores ficaram preservados" in page.text


def test_validar_guarda_o_cabecalho_visivel_sem_exigir_guardar_primeiro(
        client, monkeypatch):
    monkeypatch.setattr(main, "get_employees", lambda: {})
    uid = create_sheet(client)
    revision = get_revision(client, uid)
    page = client.get(f"/sheet/{uid}")
    assert 'id="save-header"' in page.text, "guardar separadamente é opcional"
    assert 'name="header_operador" form="validate-form"' not in page.text
    assert 'id="header-form" method="post"' in page.text
    assert "validate.disabled" not in page.text

    response = client.post(f"/sheet/{uid}/validate", data={
        "revision": revision,
        "header_operador": "Ana Silva",
        "header_n_operador": "42",
        "header_setor_maquina": "Rapid 20T - 1",
        "header_data": "31/08/2026",
        "header_turno": "M",
    })
    assert response.status_code == 303
    assert "validated=" in response.headers["location"]

    conn = db.connect()
    try:
        sheet = db.get_sheet(conn, uid)
        assert sheet["status"] == "validated"
        assert sheet["sheet_data"]["header"] == {
            "operador": "Ana Silva",
            "n_operador": "42",
            "setor_maquina": "Rapid 20T - 1",
            "data": "31/08/2026",
            "turno": "M",
        }
    finally:
        conn.close()


def test_post_incompleto_de_formulario_nunca_devolve_json_cru(client):
    uid = create_sheet(client)
    response = client.post(f"/sheet/{uid}/header", data={"operador": "Ana"})
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("text/html")
    assert not response.text.lstrip().startswith("{")


def test_escolha_explicita_prevalece_e_binding_invalida_com_edicao(
    client, monkeypatch
):
    entries = [
        {"snapshot_id": "snap-ref", "plan_key": "A", "of": "OF250001",
         "ov": "OV2400001", "cliente": "CLIENTE", "cliente_nome": "CLIENTE",
         "perfil": "L50X50X5", "modelo": "REF-A", "comp_mm": 1000},
        {"snapshot_id": "snap-ref", "plan_key": "B", "of": "OF250001",
         "ov": "OV2400001", "cliente": "CLIENTE", "cliente_nome": "CLIENTE",
         "perfil": "L50X50X5", "modelo": "REF-B", "comp_mm": 2000},
    ]
    index = PlanIndex(entries, CANTONEIRAS_SPEC, snapshot_id="snap-ref")
    monkeypatch.setattr(main, "get_index", lambda loader_name: index)
    monkeypatch.setattr(
        main.loaders, "plan_snapshot_info",
        lambda: {"snapshot_id": "snap-ref", "age_hours": 0.0},
    )
    uid = create_sheet(client)
    edit(client, uid, "rows[0].of", "250001")

    chosen = client.post(f"/sheet/{uid}/rows/0/reference", data={
        "snapshot_id": "snap-ref", "plan_key": "B",
        "revision": get_revision(client, uid), "actor": "teste",
    })
    assert chosen.status_code == 303
    conn = db.connect()
    try:
        sheet = db.get_sheet(conn, uid)
    finally:
        conn.close()
    row = sheet["sheet_data"]["rows"][0]
    assert row["modelo"] == "REF-B"
    assert row["_plan_binding"] == {
        "snapshot_id": "snap-ref", "plan_key": "B",
        "selected_explicitly": True,
        # identidade da linha, para religar a escolha depois de uma carga nova
        "identity": {"of": "OF250001", "modelo": "REF-B", "perfil": "L50X50X5",
                     "comp_mm": 2000},
    }
    cross_row = sheet["cross_check"]["rows"][0]
    assert cross_row["matched_plan_key"] == "B"
    assert cross_row["selected_explicitly"] is True
    assert cross_row["selected_snapshot_id"] == "snap-ref"

    edit(client, uid, "rows[0].perf_comp", "X")
    conn = db.connect()
    try:
        changed = db.get_sheet(conn, uid)
    finally:
        conn.close()
    assert "_plan_binding" not in changed["sheet_data"]["rows"][0]
    assert changed["cross_check"]["rows"][0]["selected_explicitly"] is False


def test_reference_obsoleta_ou_adulterada_fica_na_folha(client, monkeypatch):
    uid = create_sheet(client)
    edit(client, uid, "rows[0].of", "250001")
    response = client.post(f"/sheet/{uid}/rows/0/reference", data={
        "snapshot_id": "snapshot-antigo", "plan_key": "P1",
        "revision": get_revision(client, uid), "back": "/?status=pending",
    })
    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/sheet/{uid}?")
    assert "erro=" in response.headers["location"]

    tampered = client.post(f"/sheet/{uid}/rows/0/reference", data={
        "snapshot_id": "test-snapshot", "plan_key": "NAO-EXISTE",
        "revision": get_revision(client, uid),
    })
    assert tampered.status_code == 303
    assert "erro=" in tampered.headers["location"]

    monkeypatch.setattr(
        main.loaders, "plan_snapshot_info",
        lambda: (_ for _ in ()).throw(RuntimeError("postgres down")),
    )
    unavailable = client.post(f"/sheet/{uid}/rows/0/reference", data={
        "snapshot_id": "test-snapshot", "plan_key": "P1",
        "revision": get_revision(client, uid),
    })
    assert unavailable.status_code == 303
    assert "erro=" in unavailable.headers["location"]


def test_reference_escolhida_fica_guardada_se_o_cross_falhar(
    client, monkeypatch
):
    uid = create_sheet(client)
    edit(client, uid, "rows[0].of", "250001")
    monkeypatch.setattr(
        main, "run_cross_check",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("falha sintética do cross")
        ),
    )
    response = client.post(f"/sheet/{uid}/rows/0/reference", data={
        "snapshot_id": "test-snapshot", "plan_key": "P1",
        "revision": get_revision(client, uid), "actor": "teste",
    })
    assert response.status_code == 303
    assert "erro=" in response.headers["location"]
    conn = db.connect()
    try:
        binding = db.get_sheet(conn, uid)["sheet_data"]["rows"][0]["_plan_binding"]
    finally:
        conn.close()
    assert binding["plan_key"] == "P1"
    page = client.get(response.headers["location"])
    assert "A escolha ficou preservada" in page.text


def test_add_row_and_recheck(client):
    uid = create_sheet(client)
    conn = db.connect()
    try:
        n_before = len(db.get_sheet(conn, uid)["sheet_data"]["rows"])
    finally:
        conn.close()
    assert client.post(f"/sheet/{uid}/add-row", json={"revision": get_revision(client, uid), "request_id": "test-add-row", "values": {"qtd": "3"}}).status_code == 200
    conn = db.connect()
    try:
        assert len(db.get_sheet(conn, uid)["sheet_data"]["rows"]) == n_before + 1
    finally:
        conn.close()
    assert client.post(f"/sheet/{uid}/recheck").status_code == 303


def test_apagar_linha_e_logico_auditado_renumera_e_exclui_csv(client):
    uid = create_sheet(client)
    for i, qtd in enumerate(("10", "20", "30")):
        assert edit(client, uid, f"rows[{i}].qtd", qtd).status_code == 303
    revision = get_revision(client, uid)
    response = client.post(f"/sheet/{uid}/rows/1/delete", data={
        "revision": revision, "actor": "teste", "reason": "out_of_scope",
        "back": "/?status=in_review&page=2",
    })
    assert response.status_code == 303
    assert "back=%2F%3Fstatus%3Din_review%26page%3D2" in response.headers["location"]

    conn = db.connect()
    try:
        sheet = db.get_sheet(conn, uid)
        assert sheet["sheet_data"]["rows"][1]["_deleted"] is True
        assert sheet["sheet_data"]["rows"][1]["qtd"] == "20"
        cross_indexes = [row["row_index"] for row in sheet["cross_check"]["rows"]]
        assert 1 not in cross_indexes and 0 in cross_indexes and 2 in cross_indexes
        audit = conn.execute(
            "SELECT old_value, new_value, source, actor FROM edits "
            "WHERE sheet_uid=? AND field_path='rows[1]._deleted'", (uid,),
        ).fetchone()
    finally:
        conn.close()
    assert audit["new_value"] == "true"
    assert (audit["source"], audit["actor"]) == ("human", "teste")

    page = client.get(f"/sheet/{uid}")
    assert f'/sheet/{uid}/rows/1/delete' not in page.text
    assert f'/sheet/{uid}/rows/2/exclude' in page.text
    assert 'aria-label="Retirar linha 2"' in page.text
    csv_text = client.get(f"/sheet/{uid}/csv").text
    assert ",20" not in csv_text
    assert ",30" in csv_text
    assert client.get(f"/sheet/{uid}/plano/1").status_code == 404


def test_apagar_linha_respeita_revisao_e_folha_validada(client):
    uid = create_sheet(client)
    assert edit(client, uid, "rows[0].qtd", "1").status_code == 303
    stale = get_revision(client, uid)
    assert edit(client, uid, "rows[1].qtd", "2").status_code == 303
    conflict = client.post(f"/sheet/{uid}/rows/0/delete", data={"revision": stale, "reason": "out_of_scope"})
    assert conflict.status_code == 409
    conn = db.connect()
    try:
        assert db.get_sheet(conn, uid)["sheet_data"]["rows"][0].get("_deleted") is not True
        conn.execute("UPDATE sheets SET status='validated' WHERE uid=?", (uid,))
        conn.commit()
        revision = db.get_sheet(conn, uid)["revision"]
    finally:
        conn.close()
    page = client.get(f"/sheet/{uid}")
    assert "/delete" not in page.text
    frozen = client.post(f"/sheet/{uid}/rows/0/delete", data={"revision": revision, "reason": "out_of_scope"})
    assert frozen.status_code == 409


def test_apagar_primeira_intermedia_e_ultima_linha(client):
    for row_index in (0, 1, 2):
        uid = create_sheet(client)
        for i in range(3):
            assert edit(client, uid, f"rows[{i}].qtd", str(i + 1)).status_code == 303
        response = client.post(f"/sheet/{uid}/rows/{row_index}/delete", data={
            "revision": get_revision(client, uid), "reason": "out_of_scope",
        })
        assert response.status_code == 303
        conn = db.connect()
        try:
            rows = db.get_sheet(conn, uid)["sheet_data"]["rows"]
        finally:
            conn.close()
        assert rows[row_index]["_deleted"] is True
        assert sum(row.get("_deleted") is True for row in rows) == 1


def test_validate_sem_cabecalho_grava_com_avisos_e_fica_imutavel(client):
    uid = create_sheet(client)
    edit(client, uid, "rows[0].of", "OF250001")

    # Sem operador já não recusa (25/09): valida, com aviso, e a data fica a
    # assumida pela regra da casa (a coluna sheet_date é obrigatória).
    r = client.post(f"/sheet/{uid}/validate", data={"actor": "luis"})
    assert r.status_code == 303
    assert "erro=" not in r.headers["location"]
    assert "stored=" in r.headers["location"]
    assert "avisos=" in r.headers["location"]
    assert len(client.stored_calls) == 1
    assert client.stored_calls[0]["actor"] == "luis"
    stored = client.stored_calls[0]["sheet"]
    codes = {w["code"] for w in stored["cross_check"]["validation_warnings"]}
    assert "operador_vazio" in codes
    assert pg_store.normalize_sheet_date(stored["sheet_data"]["header"]["data"])

    # um segundo clique não é erro nem grava outra vez
    r = client.post(f"/sheet/{uid}/validate", data={"actor": "luis"})
    assert r.status_code == 303
    assert "erro=" not in r.headers["location"]
    assert len(client.stored_calls) == 1
    assert edit(client, uid, "rows[0].of", "OF999999").status_code == 409
    r = client.get(f"/sheet/{uid}")
    assert r.status_code == 200
    assert "validada" in r.text


def test_validate_colisao_de_numero_volta_a_folha_e_permite_retry(
    client, monkeypatch
):
    uid = create_sheet(client)
    edit(client, uid, "rows[0].of", "OF250001")
    edit(client, uid, "header.operador", "João")
    edit(client, uid, "header.data", "2026-08-06")

    with monkeypatch.context() as scoped:
        scoped.setattr(
            pg_store, "store_validated_sheet",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                pg_store.SheetNumberConflict(1)
            ),
        )
        response = client.post(
            f"/sheet/{uid}/validate", data={"actor": "luis"}
        )
    # O número é atribuído pelo histórico; um conflito que persiste depois
    # das novas tentativas do pg_store fica «a gravar» e volta a tentar.
    assert response.status_code == 303
    assert "a_gravar=1" in response.headers["location"]
    conn = db.connect()
    try:
        sheet = db.get_sheet(conn, uid)
        assert sheet["status"] == "validated" and sheet["sync_state"] == "retry"
    finally:
        conn.close()

    retry = client.post(f"/sheet/{uid}/sync-retry")
    assert retry.status_code == 303
    conn = db.connect()
    try:
        assert db.get_sheet(conn, uid)["sync_state"] == "done"
    finally:
        conn.close()


def test_validate_excecao_do_cross_nunca_devolve_erro_500(
    client, monkeypatch
):
    uid = create_sheet(client)
    edit(client, uid, "header.operador", "João")
    edit(client, uid, "header.data", "2026-08-06")
    monkeypatch.setattr(
        main, "run_cross_check",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("falha sintética do cross")
        ),
    )
    response = client.post(f"/sheet/{uid}/validate", data={"actor": "luis"})
    assert response.status_code == 303
    assert "erro=" in response.headers["location"]
    page = client.get(response.headers["location"])
    assert page.status_code == 200
    assert "Internal Server Error" not in page.text
    conn = db.connect()
    try:
        assert db.get_sheet(conn, uid)["status"] != "validated"
    finally:
        conn.close()


def test_validate_sem_quem_valida(client):
    """O form já não tem caixa de entidade: valida sem actor e regista
    «operador»."""
    uid = create_sheet(client)
    edit(client, uid, "header.operador", "João")
    edit(client, uid, "header.data", "2026-08-06")
    r = client.post(f"/sheet/{uid}/validate")
    assert r.status_code == 303
    assert "stored=" in r.headers["location"]
    assert client.stored_calls[-1]["actor"] == "operador"
    # a pill não exibe a entidade-fantasma «operador»
    page = client.get(f"/sheet/{uid}").text
    assert "✓ validada" in page
    assert "validada · operador" not in page


def test_validate_regressa_ao_back_exato_e_mostra_confirmacao(client):
    uid = create_sheet(client)
    edit(client, uid, "header.operador", "Ana Silva")
    edit(client, uid, "header.data", "2026-08-31")
    edit(client, uid, "rows[0].of", "250001")
    back = "/?status=pending&operador=Ana%20Silva&of=250001&page=2"
    response = client.post(f"/sheet/{uid}/validate", data={
        "revision": get_revision(client, uid), "back": back,
    })
    assert response.status_code == 303
    assert response.headers["location"] == (
        back + "&validated=1&stored=1"
    )
    page = client.get(response.headers["location"])
    assert "Folha 1 validada" in page.text
    assert "url.searchParams.delete('validated')" in page.text
    assert "url.searchParams.delete('stored')" in page.text


def test_validate_sem_back_volta_a_lista(client):
    uid = create_sheet(client)
    edit(client, uid, "header.operador", "Ana")
    edit(client, uid, "header.data", "2026-08-31")
    response = client.post(f"/sheet/{uid}/validate", data={
        "revision": get_revision(client, uid),
    })
    assert response.status_code == 303
    assert response.headers["location"].startswith("/?validated=1&stored=")


def test_confirmacao_de_validacao_e_transitoria_sem_apagar_filtros(client):
    page = client.get(
        "/?status=validated&operador=Ana&validated=17&stored=3"
    )
    assert page.status_code == 200
    assert "Folha 17 validada." in page.text
    assert "3 linha(s) gravadas." in page.text
    assert "url.searchParams.delete('validated')" in page.text
    assert "url.searchParams.delete('stored')" in page.text
    assert "url.searchParams.delete('status')" not in page.text
    assert "url.searchParams.delete('operador')" not in page.text


def test_validar_nao_consulta_o_plano_e_vale_a_carga_que_o_operador_viu(
    client, monkeypatch
):
    uid = create_sheet(client)
    edit(client, uid, "header.operador", "Ana")
    edit(client, uid, "header.data", "2026-08-31")
    edit(client, uid, "rows[0].of", "250001")
    # Entretanto entrou outra carga: o clique não vai ao Postgres saber disso
    # (decisão de 25/09) e grava o cruzamento que estava na folha.
    monkeypatch.setattr(
        main.loaders, "plan_snapshot_info",
        lambda: pytest.fail("validar não sonda o plano no Postgres"),
    )
    response = client.post(f"/sheet/{uid}/validate", data={
        "revision": get_revision(client, uid),
    })
    assert response.status_code == 303
    assert "erro=" not in response.headers["location"]
    assert len(client.stored_calls) == 1
    assert client.stored_calls[0]["sheet"]["cross_check"]["snapshot_id"] == "test-snapshot"
    conn = db.connect()
    try:
        assert db.get_sheet(conn, uid)["status"] == "validated"
    finally:
        conn.close()


def test_validate_linha_sem_qualquer_candidato_grava_sem_ligacao(client, monkeypatch):
    empty = PlanIndex([], CANTONEIRAS_SPEC, snapshot_id="test-snapshot")
    monkeypatch.setattr(main, "get_index", lambda loader_name: empty)
    uid = create_sheet(client)
    edit(client, uid, "header.operador", "Ana")
    edit(client, uid, "header.data", "2026-08-31")
    edit(client, uid, "rows[0].of", "NAO-EXISTE")
    response = client.post(f"/sheet/{uid}/validate", data={
        "revision": get_revision(client, uid),
    })
    assert response.status_code == 303
    assert "erro=" not in response.headers["location"]
    assert len(client.stored_calls) == 1
    warnings = client.stored_calls[0]["sheet"]["cross_check"]["validation_warnings"]
    assert any(w["code"] == "sem_ligacao_ao_plano" and w["row"] == 1 for w in warnings)


def test_upload_multiple_images_creates_multiple_sheets(client):
    files = [
        ("photos", ("a.png", _tiny_png(), "image/png")),
        ("photos", ("b.png", _tiny_png(), "image/png")),
    ]
    r = client.post("/upload", data={"template_name": "cantoneiras_kanban"}, files=files)
    assert r.status_code == 303
    assert r.headers["location"] == "/?created=2"


def test_upload_pdf_creates_sheet_per_page(client):
    from fpdf import FPDF
    pdf = FPDF()
    for _ in range(2):
        pdf.add_page()
        pdf.set_font("helvetica", size=12)
        pdf.cell(0, 10, "kanban teste")
    content = bytes(pdf.output())
    r = client.post("/upload", data={"template_name": "cantoneiras_kanban"},
                    files=[("photos", ("lote.pdf", content, "application/pdf"))])
    assert r.status_code == 303
    assert r.headers["location"] == "/?created=2"


def _tiny_png() -> bytes:
    from io import BytesIO

    from PIL import Image
    buf = BytesIO()
    Image.new("RGB", (40, 40), "white").save(buf, format="PNG")
    return buf.getvalue()


def test_delete_draft_but_never_validated(client):
    uid = create_sheet(client)
    deleted = client.post(f"/sheet/{uid}/delete", data={
        "back": "/?status=pending&operador=Maria&page=3",
    })
    assert deleted.status_code == 303
    assert deleted.headers["location"] == "/?status=pending&operador=Maria&page=3&deleted=1"
    assert client.get(f"/sheet/{uid}").status_code == 404

    uid = create_sheet(client)
    edit(client, uid, "header.operador", "João")
    edit(client, uid, "header.data", "2026-08-07")
    assert client.post(f"/sheet/{uid}/validate", data={"actor": "luis"}).status_code == 303
    assert client.post(f"/sheet/{uid}/delete").status_code == 409


def test_sheet_csv_downloads(client):
    uid = create_sheet(client)
    edit(client, uid, "rows[0].of", "OF250001")
    r = client.get(f"/sheet/{uid}/csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "250001" in r.text, "OF é exportada na forma canónica materializada"


def test_historico_filters(client):
    uid = create_sheet(client)
    edit(client, uid, "header.operador", "Maria")
    assert "Maria" in client.get("/?operador=Maria").text
    r = client.get("/?operador=NãoExiste")
    assert "Sem folhas" in r.text
    # chips por estado
    assert client.get("/?status=pending").status_code == 200
    assert client.get("/?status=validated").status_code == 200
    html = client.get("/?status=pending&operador=Maria").text
    assert "/static/history.js" in html
    assert 'data-history-key="kanban-mes:history"' in html


def test_sheet_pdf_downloads(client):
    uid = create_sheet(client)
    edit(client, uid, "rows[0].of", "OF250001")
    r = client.get(f"/sheet/{uid}/pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content[:5] == b"%PDF-"


def test_sheet_photo_404_without_image(client):
    uid = create_sheet(client)
    assert client.get(f"/sheet/{uid}/photo").status_code == 404


def _linhas_plano_fake():
    return [
        {"component_ref": "QS120", "length_mm": 1500.0, "quantity_planned": 100.0,
         "quantity_made": 60.0, "remaining_quantity": 40.0, "cutting_machine": "M1",
         "planning_week": "W30", "closed_x": False},
        {"component_ref": "QS121", "length_mm": 1500.0, "quantity_planned": 50.0,
         "quantity_made": 50.0, "remaining_quantity": 0.0, "cutting_machine": "M1",
         "planning_week": "W30", "closed_x": False},
    ]


def test_popup_consulta_leve_e_sem_producao_inventada(client, monkeypatch):
    from app.web import plan_review
    conn = db.connect()
    try:
        uid = db.create_sheet(conn, "serrote_kanban" if "mtg2" in main.pg_store.SOURCE_APP else "cantoneiras_kanban")
        assert db.set_extraction(conn, uid, {"header": {}, "rows": [{"of": "256000", "perfil": "100x10", "qtd": "1"}], "footer": {}})
    finally:
        conn.close()
    monkeypatch.setattr(main.loaders, "plan_snapshot_info", lambda: {"snapshot_id": "snap-1", "age_hours": 5.0})
    monkeypatch.setattr(main, "get_index", lambda *_: pytest.fail("popup must not build an index"))
    monkeypatch.setattr(plan_review, "fetch_order", lambda sid, of: [{
        "plan_key": "P1", "component_ref": "REF-A", "profile_type": "100x10",
        "length_mm": 1350., "quantity_planned": 5., "quantity_made": 0., "remaining_quantity": 5.,
    }])
    response = client.get(f"/sheet/{uid}/plano/0?origem=perf_comp")
    assert response.status_code == 200
    assert "REF-A" in response.text
    assert 'id="tabela-plano"' in response.text
    assert "Totais" not in response.text, "query string cannot assert production"
    assert "por fazer neste perfil" not in response.text


def test_consulta_do_popup_nao_limita_as_referencias(monkeypatch):
    seen = {}

    def fake_fetch(sql, params=()):
        seen.update(sql=sql, params=params)
        return [{"component_ref": f"REF-{i}"} for i in range(501)]

    monkeypatch.setattr(main.loaders, "_fetch", fake_fetch)
    rows = main.loaders.fetch_profile_lines(
        "OF250001", "50 x 5", snapshot_id="snap-all",
    )
    assert len(rows) == 501
    assert "LIMIT" not in seen["sql"].upper()


def test_perf_comp_marcado_mostra_seta_para_o_popup(client):
    uid = create_sheet(client)
    edit(client, uid, "rows[0].perf_comp", "x")
    r = client.get(f"/sheet/{uid}")
    assert r.status_code == 200
    assert "origem=perf_comp" in r.text, "a seta da coluna PERF. COMP. abre o pop-up"
