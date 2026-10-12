"""Reposição da data escrita nas folhas por validar (12/10)."""
import copy

from app import db
from scripts import restore_written_dates as restore


def make(conn, written, current, status="in_review", human=False):
    uid = db.create_sheet(conn, "cantoneiras_kanban", "data/images/x_12-10-2026_p1.png", "fixture")
    db.set_extraction(conn, uid, {"header": {"data": written}, "rows": [], "footer": {}})
    sheet = db.get_sheet(conn, uid)
    data = copy.deepcopy(sheet["sheet_data"])
    data["header"]["data"] = current
    source, actor = ("human", "operador") if human else ("system", "header:date")
    db.save_sheet_data_with_edits(conn, uid, data, sheet["revision"],
                                  [("header.data", written, current, source, actor)])
    if status == "validated":
        conn.execute("UPDATE sheets SET status='validated' WHERE uid=?", (uid,))
        conn.commit()
    return uid


def test_repoe_a_data_escrita_e_deixa_o_resto(tmp_path):
    with db.connect(tmp_path / "db.sqlite") as conn:
        trocada = make(conn, "09-10-2026", "08/10/2026")
        sem_data = make(conn, None, "08/10/2026")
        humana = make(conn, "09-10-2026", "07/10/2026", human=True)
        validada = make(conn, "09-10-2026", "08/10/2026", status="validated")
        ja_certa = make(conn, "09-10-2026", "09/10/2026")
        items = {i["uid"]: i for i in restore.inventory(conn)}
        assert set(items) == {trocada, sem_data}
        assert items[trocada]["target"] == "09/10/2026"
        assert items[sem_data]["target"] is None
        for item in items.values():
            assert restore.restore(conn, item)
        assert db.get_sheet(conn, trocada)["sheet_data"]["header"]["data"] == "09/10/2026"
        assert db.get_sheet(conn, sem_data)["sheet_data"]["header"]["data"] is None
        assert db.get_sheet(conn, humana)["sheet_data"]["header"]["data"] == "07/10/2026"
        assert db.get_sheet(conn, validada)["sheet_data"]["header"]["data"] == "08/10/2026"
        assert db.get_sheet(conn, ja_certa)["sheet_data"]["header"]["data"] == "09/10/2026"
        last = conn.execute("SELECT source, actor FROM edits WHERE sheet_uid=? ORDER BY id DESC",
                            (trocada,)).fetchone()
        assert tuple(last) == ("system", restore.ACTOR)
        assert restore.inventory(conn) == []


def test_folha_que_mudou_entretanto_fica_para_depois(tmp_path):
    with db.connect(tmp_path / "db.sqlite") as conn:
        uid = make(conn, "09-10-2026", "08/10/2026")
        item = restore.inventory(conn)[0]
        sheet = db.get_sheet(conn, uid)
        data = copy.deepcopy(sheet["sheet_data"]); data["header"]["turno"] = "M"
        db.save_sheet_data_with_edits(conn, uid, data, sheet["revision"],
                                      [("header.turno", None, "M", "human", "operador")])
        assert not restore.restore(conn, item)
        assert db.get_sheet(conn, uid)["sheet_data"]["header"]["data"] == "08/10/2026"
