"""Numeração pública sequencial, independente do UUID interno."""

from concurrent.futures import ThreadPoolExecutor
import sqlite3

from app import db


def _old_database(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sheets (
            uid TEXT PRIMARY KEY,
            template_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            image_path TEXT, image_sha256 TEXT, raw_extraction TEXT,
            sheet_data TEXT, cross_check TEXT, error_message TEXT,
            image_rotation INTEGER NOT NULL DEFAULT 0,
            revision INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, extracted_at TEXT, validated_at TEXT,
            validated_by TEXT
        );
        PRAGMA user_version = 2;
        """
    )
    conn.executemany(
        "INSERT INTO sheets(uid, template_name, created_at) VALUES (?, ?, ?)",
        [
            ("uid-b", "cantoneiras_kanban", "2026-01-01T10:00:00+00:00"),
            ("uid-a", "chapa_kanban", "2026-01-01T10:00:00+00:00"),
            ("uid-c", "cantoneiras_kanban", "2026-01-02T10:00:00+00:00"),
        ],
    )
    conn.commit()
    conn.close()


def test_backfill_deterministico_e_numero_nao_reutilizado(tmp_path):
    path = tmp_path / "old.db"
    _old_database(path)

    conn = db.connect(path)
    try:
        got = dict(conn.execute("SELECT uid, sheet_no FROM sheets").fetchall())
        assert got == {"uid-a": 1, "uid-b": 2, "uid-c": 3}
        db.delete_sheet(conn, "uid-b")
        uid = db.create_sheet(conn, "cantoneiras_paragens")
        assert db.get_sheet(conn, uid)["sheet_no"] == 4
    finally:
        conn.close()


def test_reserva_concorrente_e_unica(tmp_path):
    path = tmp_path / "concurrent.db"
    first = db.connect(path)
    first.close()

    def create_one(_):
        conn = db.connect(path)
        try:
            uid = db.create_sheet(conn, "cantoneiras_kanban")
            return db.get_sheet(conn, uid)["sheet_no"]
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        numbers = list(pool.map(create_one, range(24)))
    assert sorted(numbers) == list(range(1, 25))
    assert len(set(numbers)) == len(numbers)
