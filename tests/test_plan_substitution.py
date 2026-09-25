"""Substituição pelo plano: sem erros do Excel e sem apagar decisões humanas."""

from app import db
from app.matching import loaders, plan_values
from tests.test_web import client, create_sheet, edit  # noqa: F401


def test_erros_do_excel_e_x_das_procuras_ficam_vazios():
    entry = {"perfil": "#VALUE!", "modelo": "W2", "ov": "x", "cliente": " X ",
             "of": "OF265609", "material_description": "#REF!", "comp_mm": 15}
    assert plan_values.clean_entry(entry) == {
        "perfil": None, "modelo": "W2", "ov": None, "cliente": None,
        "of": "OF265609", "material_description": None, "comp_mm": 15}
    # «x» só é sentinela nos campos preenchidos por XLOOKUP
    assert plan_values.clean("modelo", "x") == "x"


def test_indice_do_plano_nunca_traz_erros_do_excel(monkeypatch):
    """Caso real: Met3 OF265609 W1/W2 com perfil «#VALUE!»."""
    monkeypatch.setattr(loaders, "_fetch", lambda sql, params=None: [
        {"plan_key": "k", "of": "OF265609", "ov": "x", "modelo": "W2",
         "perfil": "#VALUE!", "snapshot_loaded_at": None}])
    index = loaders.load_cantoneiras_index(snapshot_id="s1")
    assert index.entries[0]["perfil"] is None and index.entries[0]["ov"] is None


def _row(uid):
    conn = db.connect()
    try:
        return db.get_sheet(conn, uid)["sheet_data"]["rows"][0]
    finally:
        conn.close()


def test_valor_escrito_por_pessoa_diferente_do_plano_nao_e_substituido(client):
    uid = create_sheet(client)
    edit(client, uid, "rows[0].of", "250001")
    # o plano diz L50X50X5; o operador corrigiu para outra cantoneira
    edit(client, uid, "rows[0].modelo", "L60X60X6")
    assert _row(uid)["modelo"] == "L60X60X6"


def test_valor_escrito_por_pessoa_igual_ao_plano_fica_no_formato_do_plano(client):
    uid = create_sheet(client)
    edit(client, uid, "rows[0].of", "250001")
    edit(client, uid, "rows[0].modelo", "l50x50x5")
    assert _row(uid)["modelo"] == "L50X50X5"
