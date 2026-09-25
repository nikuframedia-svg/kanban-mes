"""Menos bytes pelo túnel: estáticos em cache e respostas comprimidas."""

from fastapi.testclient import TestClient

from app.web import main


def client():
    # sem «with»: não corre o lifespan (que abriria a base local)
    return TestClient(main.app)


def test_estatico_com_versao_fica_em_cache_e_sem_versao_revalida():
    version = main.templates.env.globals["css_version"]
    cached = client().get(f"/static/design.css?v={version}")
    assert cached.status_code == 200
    assert cached.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert client().get("/static/design.css").headers["cache-control"] == "no-cache"


def test_versao_dos_estaticos_cobre_todos_os_ficheiros(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_STATIC_DIR", tmp_path)
    (tmp_path / "design.css").write_text("a")
    first = main._static_version()
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "htmx.min.js").write_text("b")
    second = main._static_version()
    (tmp_path / "history.js").write_text("c")
    assert len({first, second, main._static_version()}) == 3


def test_texto_vai_comprimido_e_imagens_nao():
    version = main.templates.env.globals["css_version"]
    css = client().get(f"/static/design.css?v={version}", headers={"Accept-Encoding": "gzip"})
    assert css.headers.get("content-encoding") == "gzip"
