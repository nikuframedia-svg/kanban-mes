"""Testes da camada web: fluxo completo capturar → editar → cruzar → validar,
com staging SQLite temporário, índice do plano sintético e Postgres simulado.
O caminho real para o Postgres é coberto pelo teste E2E manual (não aqui)."""

import pytest
from fastapi.testclient import TestClient

from app import db, pg_store
from app.matching.loaders import CANTONEIRAS_SPEC
from app.matching.refs import PlanIndex
from app.web import main


def make_index() -> PlanIndex:
    entries = [
        {"plan_key": "P1", "of": "OF250001", "ov": "OV2400001",
         "cliente": "SILVA & VINHA SA", "modelo": "L50X50X5", "comp_mm": 1500},
        {"plan_key": "P2", "of": "OF250002", "ov": "OV2400002",
         "cliente": "PROEF EURICO FERREIRA", "modelo": "L60X60X6", "comp_mm": 2000},
    ]
    return PlanIndex(entries, CANTONEIRAS_SPEC, plan_age_days=1.0)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    real_connect = db.connect
    monkeypatch.setattr(db, "connect", lambda path=None: real_connect(tmp_path / "test.db"))
    monkeypatch.setattr(main, "get_index", lambda loader_name: make_index())
    monkeypatch.setattr(main.loaders, "load_active_ofs", lambda: set())

    stored_calls: list[dict] = []

    def fake_store(sheet, template, edit_count, actor):
        stored_calls.append({"sheet": sheet, "template": template,
                             "edit_count": edit_count, "actor": actor})
        rows = (sheet["sheet_data"] or {}).get("rows") or []
        return sum(1 for r in rows
                   if any(v is not None and str(v).strip() != "" for v in r.values()))

    monkeypatch.setattr(pg_store, "store_validated_sheet", fake_store)
    monkeypatch.setattr(main, "PROCESS_IN_BACKGROUND", False)  # determinístico
    c = TestClient(main.app, follow_redirects=False)
    c.stored_calls = stored_calls
    return c


def create_sheet(client) -> str:
    r = client.post("/upload", data={"template_name": "cantoneiras_kanban"})
    assert r.status_code == 303
    return r.headers["location"].rsplit("/", 1)[1]


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
                "x", "header.<script>", "rows[1].of; DROP"):
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


def test_proposta_vai_em_data_attribute(client):
    """O tojson dentro de onclick="..." fechava o atributo no primeiro «"» — o
    link «aceitar proposta» nunca funcionou. O valor vai em data-proposal."""
    uid = create_sheet(client)
    edit(client, uid, "rows[0].of", "OF250001")   # linha ligada, OV por preencher
    r = client.get(f"/sheet/{uid}")
    if "proposal" in r.text and "acceptProposal" in r.text:
        assert "data-proposal=" in r.text
        assert "acceptProposal(this)" in r.text
        assert "acceptProposal(this, " not in r.text, "valor interpolado no onclick"


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
    assert client.get("/captura/camara").status_code == 200


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


def test_header_corrige_campos_seguros_e_mantem_precedencia_do_operador(
        client, monkeypatch):
    """Turno/máquina canonizam-se sozinhos; a DATA é assumida como o dia útil
    anterior ao PDF de origem (18-08-2026, terça → 17/08/2026, segunda),
    por cima do que o OCR leu; a identidade do operador continua a ser escrita
    pelo resolve_operator (o checker descreve-a)."""
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
        "setor_maquina": "Ficep Rapid 20T -2", "data": "17/08/2026", "turno": "M",
    }
    header_cross = sheet["cross_check"]["header"]
    assert header_cross["source_document"]["filename"] == "18-08-2026.pdf"
    assert header_cross["source_document"]["page"] == 2
    assert header_cross["cells"]["operador"]["status"] == "confirmed"
    assert header_cross["cells"]["data"]["applied"] is True
    assert header_cross["cells"]["data"]["reason"] == "assumed_prev_business_day"
    assert header_cross["cells"]["setor_maquina"]["applied"] is True
    # precedência: a identidade aceite é a do resolve_operator
    assert sheet["cross_check"]["operator"]["rule"] == "token"
    assert sheet["cross_check"]["operator"]["pernr"] == "10003480"

    r = client.get(f"/sheet/{uid}")
    assert "header-field-data cc-match" in r.text
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
    assert cross["header"]["cells"]["data"]["status"] == "confirmed"
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


def test_edit_with_stale_revision_conflicts(client):
    uid = create_sheet(client)
    r = client.post(f"/sheet/{uid}/edit", data={
        "field_path": "rows[0].of", "value": "OF250001",
        "revision": 999, "actor": "teste",
    })
    assert r.status_code == 409


def test_add_row_and_recheck(client):
    uid = create_sheet(client)
    conn = db.connect()
    try:
        n_before = len(db.get_sheet(conn, uid)["sheet_data"]["rows"])
    finally:
        conn.close()
    assert client.post(f"/sheet/{uid}/add-row").status_code == 303
    conn = db.connect()
    try:
        assert len(db.get_sheet(conn, uid)["sheet_data"]["rows"]) == n_before + 1
    finally:
        conn.close()
    assert client.post(f"/sheet/{uid}/recheck").status_code == 303


def test_validate_requires_header_then_stores_and_freezes(client):
    uid = create_sheet(client)
    edit(client, uid, "rows[0].of", "OF250001")

    # sem operador/data → recusa, mas como redirect com banner na folha (uma
    # resposta JSON crua ao POST do form lê-se como crash no browser)
    r = client.post(f"/sheet/{uid}/validate", data={"actor": "luis"})
    assert r.status_code == 303
    assert "erro=" in r.headers["location"]
    r = client.get(r.headers["location"])
    assert "Não foi possível validar" in r.text

    edit(client, uid, "header.operador", "João")
    edit(client, uid, "header.data", "2026-08-06")
    r = client.post(f"/sheet/{uid}/validate", data={"actor": "luis"})
    assert r.status_code == 303
    assert "stored=" in r.headers["location"]
    assert len(client.stored_calls) == 1
    assert client.stored_calls[0]["actor"] == "luis"

    # imutável depois de validada — o portão volta à folha como banner
    r = client.post(f"/sheet/{uid}/validate", data={"actor": "luis"})
    assert r.status_code == 303
    assert "erro=" in r.headers["location"]
    assert len(client.stored_calls) == 1
    assert edit(client, uid, "rows[0].of", "OF999999").status_code == 409
    r = client.get(f"/sheet/{uid}")
    assert r.status_code == 200
    assert "validada" in r.text


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
    assert client.post(f"/sheet/{uid}/delete").status_code == 303
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
    assert "OF250001" in r.text


def test_historico_filters(client):
    uid = create_sheet(client)
    edit(client, uid, "header.operador", "Maria")
    assert "Maria" in client.get("/?operador=Maria").text
    r = client.get("/?operador=NãoExiste")
    assert "Sem folhas" in r.text
    # chips por estado
    assert client.get("/?status=pending").status_code == 200
    assert client.get("/?status=validated").status_code == 200


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


def test_plano_popup_totais_so_quando_vem_do_perf_comp(client, monkeypatch):
    """O mesmo pop-up serve dois cliques: da célula do perfil (lista simples)
    e da marca PERF. COMP. (que afirma «fiz tudo» — leva totais e, se o plano
    ainda mostra falta, um aviso)."""
    monkeypatch.setattr(main.loaders, "plan_snapshot_info", lambda: {"age_hours": 5.0})
    monkeypatch.setattr(main.loaders, "fetch_profile_lines",
                        lambda of, perfil: _linhas_plano_fake())
    monkeypatch.setattr(main.loaders, "fetch_profiles_in_of", lambda of: [])
    uid = create_sheet(client)
    edit(client, uid, "rows[0].of", "250001")
    edit(client, uid, "rows[0].perfil", "50 x 5")

    r = client.get(f"/sheet/{uid}/plano/0", params={"origem": "perf_comp"})
    assert r.status_code == 200
    assert "QS120" in r.text
    assert "Totais" in r.text
    assert "por fazer neste perfil" in r.text, "falta agregada (40) devia gerar aviso"

    r2 = client.get(f"/sheet/{uid}/plano/0")
    assert r2.status_code == 200
    assert "QS120" in r2.text
    assert "Totais" not in r2.text
    assert "por fazer neste perfil" not in r2.text


def test_perf_comp_marcado_mostra_seta_para_o_popup(client):
    uid = create_sheet(client)
    edit(client, uid, "rows[0].perf_comp", "x")
    r = client.get(f"/sheet/{uid}")
    assert r.status_code == 200
    assert "origem=perf_comp" in r.text, "a seta da coluna PERF. COMP. abre o pop-up"
