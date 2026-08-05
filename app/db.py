"""Staging local (SQLite): TODO o trabalho em curso vive aqui.
O Postgres só é tocado no ato de validação (app/pg_store.py).

Tabelas:
- sheets: uma folha kanban (foto opcional, JSON do OCR bruto imutável, JSON atual)
- edits: trilho de auditoria de todas as alterações a células
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS sheets (
    uid             TEXT PRIMARY KEY,
    template_name   TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending|extracted|in_review|validated|error
    image_path      TEXT,
    image_sha256    TEXT,
    raw_extraction  TEXT,          -- JSON imutável (OCR bruto ou folha manual inicial)
    sheet_data      TEXT,          -- JSON atual (pós-cross + edições)
    cross_check     TEXT,          -- JSON do último cruzamento
    error_message   TEXT,
    revision        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    extracted_at    TEXT,
    validated_at    TEXT,
    validated_by    TEXT
);
CREATE TABLE IF NOT EXISTS edits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sheet_uid   TEXT NOT NULL REFERENCES sheets(uid),
    field_path  TEXT NOT NULL,     -- 'rows[3].of' | 'header.data'
    old_value   TEXT,
    new_value   TEXT,
    source      TEXT NOT NULL,     -- human | system
    actor       TEXT,
    edited_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS edits_sheet_idx ON edits(sheet_uid);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = path or settings.sqlite_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(SCHEMA)
    return conn


def create_sheet(conn: sqlite3.Connection, template_name: str,
                 image_path: str | None = None, image_sha256: str | None = None) -> str:
    uid = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO sheets (uid, template_name, image_path, image_sha256, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (uid, template_name, image_path, image_sha256, now_iso()),
    )
    conn.commit()
    return uid


def set_extraction(conn: sqlite3.Connection, uid: str, extraction: dict) -> None:
    conn.execute(
        "UPDATE sheets SET raw_extraction = ?, sheet_data = ?, status = 'extracted', "
        "extracted_at = ?, revision = revision + 1 WHERE uid = ? AND status != 'validated'",
        (json.dumps(extraction, ensure_ascii=False, default=str),
         json.dumps(extraction, ensure_ascii=False, default=str), now_iso(), uid),
    )
    conn.commit()


def get_sheet(conn: sqlite3.Connection, uid: str) -> dict | None:
    row = conn.execute("SELECT * FROM sheets WHERE uid = ?", (uid,)).fetchone()
    if not row:
        return None
    sheet = dict(row)
    for key in ("raw_extraction", "sheet_data", "cross_check"):
        sheet[key] = json.loads(sheet[key]) if sheet[key] else None
    return sheet


def list_sheets(conn: sqlite3.Connection, status: str | None = None) -> list[dict]:
    if status:
        rows = conn.execute(
            "SELECT uid, template_name, status, created_at, validated_at, revision "
            "FROM sheets WHERE status = ? ORDER BY created_at DESC", (status,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT uid, template_name, status, created_at, validated_at, revision "
            "FROM sheets ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def save_sheet_data(conn: sqlite3.Connection, uid: str, sheet_data: dict,
                    expected_revision: int) -> bool:
    """Escrita com controlo otimista de concorrência: falha se a revisão mudou."""
    cur = conn.execute(
        "UPDATE sheets SET sheet_data = ?, status = 'in_review', revision = revision + 1 "
        "WHERE uid = ? AND revision = ? AND status != 'validated'",
        (json.dumps(sheet_data, ensure_ascii=False, default=str), uid, expected_revision),
    )
    conn.commit()
    return cur.rowcount == 1


def save_cross_check(conn: sqlite3.Connection, uid: str, cross: dict) -> None:
    conn.execute(
        "UPDATE sheets SET cross_check = ? WHERE uid = ? AND status != 'validated'",
        (json.dumps(cross, ensure_ascii=False, default=str), uid),
    )
    conn.commit()


def record_edit(conn: sqlite3.Connection, uid: str, field_path: str,
                old_value: object, new_value: object, source: str, actor: str | None) -> None:
    conn.execute(
        "INSERT INTO edits (sheet_uid, field_path, old_value, new_value, source, actor, edited_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (uid, field_path,
         None if old_value is None else str(old_value),
         None if new_value is None else str(new_value),
         source, actor, now_iso()),
    )
    conn.commit()


def human_fields_by_row(conn: sqlite3.Connection, uid: str) -> dict[int, set[str]]:
    """Campos por linha já corrigidos por humanos — invioláveis para o motor."""
    out: dict[int, set[str]] = {}
    for r in conn.execute(
        "SELECT field_path FROM edits WHERE sheet_uid = ? AND source = 'human'", (uid,)
    ).fetchall():
        path = r["field_path"]
        if path.startswith("rows["):
            idx_s, _, fname = path[5:].partition("].")
            try:
                out.setdefault(int(idx_s), set()).add(fname)
            except ValueError:
                continue
    return out


def mark_validated(conn: sqlite3.Connection, uid: str, actor: str) -> bool:
    cur = conn.execute(
        "UPDATE sheets SET status = 'validated', validated_at = ?, validated_by = ? "
        "WHERE uid = ? AND status != 'validated'",
        (now_iso(), actor, uid),
    )
    conn.commit()
    return cur.rowcount == 1


def edit_count(conn: sqlite3.Connection, uid: str) -> int:
    return conn.execute(
        "SELECT count(*) FROM edits WHERE sheet_uid = ? AND source = 'human'", (uid,)
    ).fetchone()[0]
