"""Cache do índice do plano: uma construção de cada vez, reconstrução por trás."""

from __future__ import annotations

import threading
import time

from app.matching.loaders import CANTONEIRAS_SPEC
from app.matching.refs import PlanIndex
from app.web import main


def plan(snapshot_id: str) -> PlanIndex:
    return PlanIndex([{"plan_key": snapshot_id + "-1", "of": "OF1", "perfil": "L50X50X5"}],
                     CANTONEIRAS_SPEC, snapshot_id=snapshot_id)


def join_rebuilds() -> None:
    for thread in threading.enumerate():
        if thread.name.startswith("rebuild-"):
            thread.join(5)


def test_pedidos_simultaneos_sem_indice_constroem_uma_so_vez(monkeypatch):
    loads = []

    def slow_loader(snapshot_id=None):
        loads.append(snapshot_id)
        time.sleep(0.2)
        return plan(snapshot_id)

    monkeypatch.setattr(main.loaders, "load_cantoneiras_index", slow_loader)
    monkeypatch.setattr(main, "_current_snapshot_id", lambda **_: "s1")
    results = []
    threads = [threading.Thread(target=lambda: results.append(main.get_index("load_cantoneiras_index")))
               for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert loads == ["s1"], "cada pedido descarregava o plano inteiro pelo túnel"
    assert len({id(index) for index in results}) == 1


def test_plano_novo_reconstroi_por_tras_e_serve_o_anterior(monkeypatch):
    snapshots = {"current": "s1"}
    started, release = threading.Event(), threading.Event()

    def loader(snapshot_id=None):
        if snapshot_id == "s2":
            started.set()
            release.wait(5)
        return plan(snapshot_id)

    monkeypatch.setattr(main.loaders, "load_cantoneiras_index", loader)
    monkeypatch.setattr(main, "_current_snapshot_id", lambda **_: snapshots["current"])
    monkeypatch.setattr(main, "_FRESHNESS_PROBE_SECONDS", 0)
    first = main.get_index("load_cantoneiras_index")
    assert first.snapshot_id == "s1"

    snapshots["current"] = "s2"
    during = main.get_index("load_cantoneiras_index")
    assert during is first, "a revisão não espera pela descarga do plano novo"
    assert started.wait(5), "a reconstrução arrancou por trás"
    release.set()
    join_rebuilds()
    assert main.get_index("load_cantoneiras_index").snapshot_id == "s2"


def test_validacao_exige_o_plano_atual_mesmo_com_reconstrucao_por_tras(monkeypatch):
    snapshots = {"current": "s1"}
    monkeypatch.setattr(main.loaders, "load_cantoneiras_index", lambda snapshot_id=None: plan(snapshot_id))
    monkeypatch.setattr(main, "_current_snapshot_id", lambda **_: snapshots["current"])
    main.get_index("load_cantoneiras_index")
    snapshots["current"] = "s2"
    assert main.get_index("load_cantoneiras_index", require_current=True).snapshot_id == "s2"
    join_rebuilds()


def test_escolha_de_referencia_reaproveita_o_indice_da_carga_atual(monkeypatch):
    loads = []
    monkeypatch.setattr(main.loaders, "load_cantoneiras_index",
                        lambda snapshot_id=None: loads.append(snapshot_id) or plan(snapshot_id))
    monkeypatch.setattr(main, "_current_snapshot_id", lambda **_: "s1")
    main.get_index("load_cantoneiras_index")
    index = main._current_plan_index("load_cantoneiras_index", "s1")
    assert index.snapshot_id == "s1"
    assert loads == ["s1"], "cada escolha de referência descarregava o plano inteiro"


def test_verificacao_da_folha_nao_consulta_ofs_ativas(monkeypatch):
    monkeypatch.setattr(main.loaders, "load_cantoneiras_index", lambda snapshot_id=None: plan("s1"))
    monkeypatch.setattr(main, "_current_snapshot_id", lambda **_: "s1")
    from app import pg
    monkeypatch.setattr(pg, "fetch", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("o scorer não deve ir ao Postgres")))
    scorer = main.make_scorer("cantoneiras_kanban")
    assert scorer.active_primary == set()
