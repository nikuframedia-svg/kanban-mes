"""Staging local (SQLite): TODO o trabalho em curso vive aqui.
O Postgres só é tocado no ato de validação (app/pg_store.py).

Tabelas:
- sheets: uma folha kanban (foto opcional, JSON do OCR bruto imutável, JSON atual)
- edits: trilho de auditoria de todas as alterações a células
"""

from __future__ import annotations

import json
import sqlite3
import threading
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
    image_rotation  INTEGER NOT NULL DEFAULT 0,  -- quartos de volta CW pedidos por humano
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

# Colunas acrescentadas depois de já haver bases em uso. Bases novas nascem com
# elas (estão no SCHEMA); as antigas precisam de ALTER, e o SQLite não tem
# "ADD COLUMN IF NOT EXISTS". A escada por user_version corre uma vez por
# ficheiro de base; o try/except cobre a corrida entre processos.
_SCHEMA_VERSION = 1
_MIGRATIONS = (
    (1, "ALTER TABLE sheets ADD COLUMN image_rotation INTEGER NOT NULL DEFAULT 0"),
)
_migrated: set[str] = set()
_migrate_lock = threading.Lock()


def _migrate(conn: sqlite3.Connection, key: str) -> None:
    if key in _migrated:
        return
    with _migrate_lock:
        if key in _migrated:
            return
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for target, sql in _MIGRATIONS:
            if version < target:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError as exc:
                    # Base criada de raiz pelo SCHEMA já tem a coluna.
                    if "duplicate column" not in str(exc).lower():
                        raise
                version = target
        if version < _SCHEMA_VERSION:
            version = _SCHEMA_VERSION
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
        _migrated.add(key)


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
    _migrate(conn, str(db_path))
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


def mark_pending(conn: sqlite3.Connection, uid: str) -> bool:
    """Volta a pôr a folha na fila do OCR (re-leitura). Nunca folhas validadas."""
    cur = conn.execute(
        "UPDATE sheets SET status = 'pending', revision = revision + 1 "
        "WHERE uid = ? AND status != 'validated'",
        (uid,),
    )
    conn.commit()
    return cur.rowcount == 1


def set_template(conn: sqlite3.Connection, uid: str, template_name: str) -> None:
    """Reclassificação (frente/verso) pelo worker de OCR — só antes de validada."""
    conn.execute(
        "UPDATE sheets SET template_name = ? WHERE uid = ? AND status != 'validated'",
        (template_name, uid),
    )
    conn.commit()


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


def list_sheets(conn: sqlite3.Connection, status: str | None = None,
                operador: str | None = None, setor: str | None = None,
                data_folha: str | None = None, data_captura: str | None = None,
                of: str | None = None) -> list[dict]:
    """Lista de folhas com campos do cabeçalho extraídos do JSON + filtros do Histórico.

    `status` aceita também o pseudo-estado 'pending' = tudo o que não está
    validado nem em erro (o que o Histórico chama «Pendentes»)."""
    sql = (
        "SELECT uid, template_name, status, image_path, created_at, validated_at, revision, "
        "  json_extract(sheet_data, '$.header.operador')      AS operador, "
        "  json_extract(sheet_data, '$.header.data')          AS data_folha, "
        "  json_extract(sheet_data, '$.header.setor_maquina') AS setor "
        "FROM sheets WHERE 1=1"
    )
    args: list = []
    if status == "pending":
        sql += " AND status NOT IN ('validated', 'error')"
    elif status:
        sql += " AND status = ?"
        args.append(status)
    if operador:
        sql += " AND json_extract(sheet_data, '$.header.operador') = ?"
        args.append(operador)
    if setor:
        sql += " AND json_extract(sheet_data, '$.header.setor_maquina') = ?"
        args.append(setor)
    if data_folha:
        sql += " AND substr(json_extract(sheet_data, '$.header.data'), 1, 10) = ?"
        args.append(data_folha)
    if data_captura:
        sql += " AND substr(created_at, 1, 10) = ?"
        args.append(data_captura)
    if of:
        # pesquisa simples no JSON das linhas — chega para encontrar uma OF
        sql += " AND sheet_data LIKE ?"
        args.append(f"%{of.strip()}%")
    sql += " ORDER BY created_at DESC"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def filter_options(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Valores distintos de operador e setor para os selects do Histórico."""
    ops = [r[0] for r in conn.execute(
        "SELECT DISTINCT json_extract(sheet_data, '$.header.operador') FROM sheets "
        "WHERE json_extract(sheet_data, '$.header.operador') IS NOT NULL ORDER BY 1").fetchall()]
    sets = [r[0] for r in conn.execute(
        "SELECT DISTINCT json_extract(sheet_data, '$.header.setor_maquina') FROM sheets "
        "WHERE json_extract(sheet_data, '$.header.setor_maquina') IS NOT NULL ORDER BY 1").fetchall()]
    return {"operadores": ops, "setores": sets}


def delete_sheet(conn: sqlite3.Connection, uid: str) -> str | None:
    """Apaga um RASCUNHO (folha não validada) e o seu trilho de edições.
    Devolve o image_path (para o chamador apagar o ficheiro) ou lança se validada."""
    row = conn.execute("SELECT status, image_path FROM sheets WHERE uid = ?", (uid,)).fetchone()
    if row is None:
        raise KeyError(uid)
    if row["status"] == "validated":
        raise PermissionError("folha validada é imutável")
    conn.execute("DELETE FROM edits WHERE sheet_uid = ?", (uid,))
    conn.execute("DELETE FROM sheets WHERE uid = ?", (uid,))
    conn.commit()
    return row["image_path"]


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


def set_image_rotation(conn: sqlite3.Connection, uid: str, rotation: int) -> int:
    """Rotação manual pedida pelo humano, em quartos de volta no sentido horário.

    Normalizada a {0, 90, 180, 270}. É um pedido *adicional* à correcção
    automática: 0 não quer dizer «não rodes», quer dizer «a automática chega».
    """
    norm = (int(rotation) % 360 // 90) * 90
    conn.execute("UPDATE sheets SET image_rotation = ? WHERE uid = ?", (norm, uid))
    conn.commit()
    return norm


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
