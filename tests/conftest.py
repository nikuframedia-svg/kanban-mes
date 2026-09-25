import sys

import pytest

from app import pg, sync_worker


def _limpar_estado_partilhado() -> None:
    pg.reset()
    main = sys.modules.get("app.web.main")
    if main is not None:
        main._index_cache.clear()


@pytest.fixture(autouse=True)
def _estado_partilhado_limpo():
    """Nenhum teste herda ligações guardadas nem índices em cache de outro."""
    _limpar_estado_partilhado()
    yield
    _limpar_estado_partilhado()


@pytest.fixture(autouse=True)
def _validacao_deterministica(monkeypatch):
    """Nos testes a gravação no histórico corre dentro do pedido («sync») e o
    trabalhador em segundo plano não arranca; os testes do modo «background»
    ativam-no explicitamente."""
    import app.web.main as main
    monkeypatch.setattr(main, "VALIDATION_MODE", "sync")
    monkeypatch.setattr(sync_worker, "start", lambda: None)
