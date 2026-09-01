"""Testes da página Estado — Postgres simulado por monkeypatch."""

import pytest
from app import db
from app.web import estado, main
from tests.live_client import LiveTestClient

PLAN = [
    {"cliente": "silva & vinha sa", "ov": "OV2400001", "of": "OF250001",
     "familia": "cantoneiras", "qtd_planeada": 100, "qtd_restante": 40,
     "maquinas": "Rapid 20T - 1", "semana": 32},
    {"cliente": "proef", "ov": "OV2400002", "of": "OF250002",
     "familia": "chapa", "qtd_planeada": 50, "qtd_restante": 50,
     "maquinas": "Laser", "semana": None},
]
VALIDATED = [
    {"of": "OF250001", "familia": "cantoneiras", "cliente": "SILVA & VINHA SA",
     "ov": "OV2400001", "qtd_validada": 55, "linhas": 3, "ultima_folha": "2026-08-06"},
    {"of": "OF999999", "familia": "cantoneiras", "cliente": "FANTASMA",
     "ov": None, "qtd_validada": 7, "linhas": 1, "ultima_folha": "2026-08-01"},
]


def test_merge_by_of_junta_plano_e_validado():
    rows = estado.merge_by_of(PLAN, VALIDATED)
    by_of = {r["of"]: r for r in rows}
    assert len(rows) == 3
    r1 = by_of["OF250001"]
    assert r1["progresso"] == pytest.approx(0.6)     # (100-40)/100
    assert r1["qtd_validada"] == 55
    assert r1["sem_plano"] is False
    assert by_of["OF250002"]["progresso"] == 0.0
    ghost = by_of["OF999999"]
    assert ghost["sem_plano"] is True and ghost["qtd_planeada"] is None
    # atividade recente primeiro
    assert rows[0]["of"] == "OF250001"


def test_merge_nao_converte_falta_desconhecida_em_zero():
    rows = estado.merge_by_of([{
        "cliente": "x", "ov": "OV1", "of": "OF1",
        "familia": "cantoneiras", "qtd_planeada": 10,
        "qtd_restante": None, "maquinas": None, "semana": None,
    }], [])
    assert rows[0]["qtd_restante"] is None
    assert rows[0]["progresso"] is None


def test_estado_isola_factos_validados_desta_aplicacao(monkeypatch):
    queries = []

    def capture(sql, params=()):
        queries.append((sql, params))
        return [{"x": 1}]

    monkeypatch.setattr(estado, "_fetch", capture)
    estado.fetch_validated_rows()
    estado.fetch_mes_kpis()
    estado.fetch_of_detail("OF1")

    validated_sql, kpi_sql, _plan_sql, detail_sql = [sql for sql, _ in queries]
    assert "s.source_app = 'kanban-mes'" in validated_sql
    assert kpi_sql.count("source_app = 'kanban-mes'") == 2
    assert "s.source_app = 'kanban-mes'" in detail_sql


def test_filter_rows():
    rows = estado.merge_by_of(PLAN, VALIDATED)
    assert {r["of"] for r in estado.filter_rows(rows, q="silva")} == {"OF250001"}
    assert {r["of"] for r in estado.filter_rows(rows, familia="chapa")} == {"OF250002"}
    assert estado.filter_rows(rows, q="não existe") == []


@pytest.fixture()
def client(tmp_path, monkeypatch):
    real_connect = db.connect
    monkeypatch.setattr(db, "connect", lambda path=None: real_connect(tmp_path / "t.db"))
    monkeypatch.setattr(estado, "fetch_plan_rows", lambda: PLAN)
    monkeypatch.setattr(estado, "fetch_validated_rows", lambda: VALIDATED)
    monkeypatch.setattr(estado, "fetch_mes_kpis", lambda: {"folhas": 2, "registos": 4})
    monkeypatch.setattr(estado, "fetch_of_detail", lambda of: {
        "plan": [{"modelo": "L50X50X5", "perfil": "L50", "comp_mm": 1500,
                  "qtd_planeada": 100, "qtd_restante": 40, "maquina": "Rapid"}],
        "produced": [{"sheet_uid": "abc12345", "row_index": 0,
                      "sheet_date": "2026-08-06", "operator_name": "José",
                      "machine": "Rapid", "model_ref": "L50X50X5",
                      "quantity": 55, "match_confidence": 0.97}],
    })
    with LiveTestClient(main.app, follow_redirects=False) as c:
        yield c


def test_estado_renders(client):
    r = client.get("/estado")
    assert r.status_code == 200
    assert "OF250001" in r.text and "OF999999" in r.text
    assert "sem plano" in r.text


def test_estado_search_and_family_filter(client):
    r = client.get("/estado?q=silva")
    assert "OF250001" in r.text and "OF250002" not in r.text
    r = client.get("/estado?familia=chapa")
    assert "OF250002" in r.text and "OF250001" not in r.text


def test_estado_of_drilldown(client):
    r = client.get("/estado?of=OF250001")
    assert r.status_code == 200
    assert "Detalhe da OF" in r.text and "abc12345"[:8] in r.text


def test_estado_degrada_sem_postgres(client, monkeypatch):
    def boom():
        raise RuntimeError("connection refused")
    monkeypatch.setattr(estado, "fetch_plan_rows", boom)
    r = client.get("/estado")
    assert r.status_code == 200
    assert "Postgres indisponível" in r.text


def test_estado_pdf(client):
    r = client.get("/estado/pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content[:5] == b"%PDF-"


def test_estado_pdf_503_sem_postgres(client, monkeypatch):
    def boom():
        raise RuntimeError("connection refused")
    monkeypatch.setattr(estado, "fetch_plan_rows", boom)
    assert client.get("/estado/pdf").status_code == 503
