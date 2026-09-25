import sys

import pytest

from app import pg


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
