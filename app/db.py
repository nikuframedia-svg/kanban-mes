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
    sheet_no        INTEGER UNIQUE,
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
CREATE TABLE IF NOT EXISTS app_counters (
    name        TEXT PRIMARY KEY,
    next_value  INTEGER NOT NULL CHECK (next_value > 0)
);
CREATE TABLE IF NOT EXISTS ingested_files (
    filename    TEXT NOT NULL,     -- nome do PDF na pasta do Drive
    sha256      TEXT NOT NULL PRIMARY KEY,  -- do FICHEIRO; muda → reprocessa
    n_pages     INTEGER NOT NULL,
    ingested_at TEXT NOT NULL
);
"""

# Colunas acrescentadas depois de já haver bases em uso. Bases novas nascem com
# elas (estão no SCHEMA); as antigas precisam de ALTER, e o SQLite não tem
# "ADD COLUMN IF NOT EXISTS". A escada por user_version corre uma vez por
# ficheiro de base; o try/except cobre a corrida entre processos.
_SCHEMA_VERSION = 3
_MIGRATIONS = (
    (1, "ALTER TABLE sheets ADD COLUMN image_rotation INTEGER NOT NULL DEFAULT 0"),
    # v2: ingested_files já nasce no SCHEMA (CREATE TABLE IF NOT EXISTS corre
    # em todas as ligações); a versão sobe só para o registo ficar honesto.
    (2, "SELECT 1"),
    (3, "ALTER TABLE sheets ADD COLUMN sheet_no INTEGER"),
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
        # v3: o número público é independente do UUID e comum a todos os
        # templates desta aplicação. O backfill é determinístico, e o contador
        # guarda o próximo valor para que números apagados nunca regressem.
        if version >= 3:
            missing = conn.execute(
                "SELECT uid FROM sheets WHERE sheet_no IS NULL "
                "ORDER BY created_at, uid"
            ).fetchall()
            next_no = conn.execute(
                "SELECT coalesce(max(sheet_no), 0) + 1 FROM sheets"
            ).fetchone()[0]
            for row in missing:
                conn.execute(
                    "UPDATE sheets SET sheet_no = ? WHERE uid = ?",
                    (next_no, row["uid"]),
                )
                next_no += 1
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS sheets_sheet_no_uq "
                "ON sheets(sheet_no)"
            )
            conn.execute(
                "INSERT INTO app_counters(name, next_value) VALUES ('sheet_no', ?) "
                "ON CONFLICT(name) DO UPDATE SET next_value = "
                "max(app_counters.next_value, excluded.next_value)",
                (next_no,),
            )
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
    # WAL: as escritas do worker de OCR deixam de bloquear as leituras das
    # páginas (em journal delete, um lote a gravar prendia o Histórico e o
    # event loop até 5 s por pedido).
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _migrate(conn, str(db_path))
    return conn


def create_sheet(conn: sqlite3.Connection, template_name: str,
                 image_path: str | None = None, image_sha256: str | None = None) -> str:
    uid = uuid.uuid4().hex[:12]
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR IGNORE INTO app_counters(name, next_value) "
            "VALUES ('sheet_no', coalesce((SELECT max(sheet_no) + 1 FROM sheets), 1))"
        )
        sheet_no = conn.execute(
            "SELECT next_value FROM app_counters WHERE name = 'sheet_no'"
        ).fetchone()[0]
        conn.execute(
            "UPDATE app_counters SET next_value = next_value + 1 "
            "WHERE name = 'sheet_no'"
        )
        conn.execute(
            "INSERT INTO sheets (uid, sheet_no, template_name, image_path, "
            "image_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (uid, sheet_no, template_name, image_path, image_sha256, now_iso()),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return uid


def set_error(conn: sqlite3.Connection, uid: str, message: str) -> None:
    """O worker rebentou nesta folha: fica `error`, visível e recuperável.
    Antes ficava `pending` com spinner eterno e sem botão nenhum."""
    conn.execute(
        "UPDATE sheets SET status = 'error', error_message = ?, revision = revision + 1 "
        "WHERE uid = ? AND status = 'pending'",
        (message[:500], uid),
    )
    conn.commit()


def pending_with_image(conn: sqlite3.Connection) -> list[str]:
    """Folhas à espera de OCR — para o arranque re-enfileirar o que um restart
    a meio de um lote deixou penduradas (as threads do worker são daemon)."""
    return [r[0] for r in conn.execute(
        "SELECT uid FROM sheets WHERE status = 'pending' AND image_path IS NOT NULL "
        "ORDER BY created_at"
    ).fetchall()]


def mark_pending(conn: sqlite3.Connection, uid: str) -> bool:
    """Volta a pôr a folha na fila do OCR (re-leitura). Nunca folhas validadas."""
    cur = conn.execute(
        "UPDATE sheets SET status = 'pending', revision = revision + 1 "
        "WHERE uid = ? AND status != 'validated'",
        (uid,),
    )
    conn.commit()
    return cur.rowcount == 1


def set_template(conn: sqlite3.Connection, uid: str, template_name: str) -> bool:
    """Reclassificação isolada, apenas enquanto a folha continua pendente."""
    cur = conn.execute(
        "UPDATE sheets SET template_name = ? WHERE uid = ? AND status = 'pending'",
        (template_name, uid),
    )
    conn.commit()
    return cur.rowcount == 1


def set_extraction(conn: sqlite3.Connection, uid: str, extraction: dict,
                   template_name: str | None = None) -> bool:
    """Grava a transcrição — SÓ em folhas ainda pendentes.

    ``template_name`` cobre a reclassificação frente/verso do extract_auto:
    escolher o template e gravar a transcrição na MESMA escrita impede uma
    folha reclassificada de ficar com a transcrição da face errada se o
    processo cair entre as duas operações.

    Se o revisor começou a editar enquanto o OCR corria (status já saiu de
    'pending'), gravar por cima apagava o trabalho dele. Devolve False nesse
    caso: o worker desiste e a folha fica como o humano a tem.
    """
    if template_name is None:
        row = conn.execute(
            "SELECT template_name FROM sheets WHERE uid = ?", (uid,)
        ).fetchone()
        if row is None:
            return False
        template_name = row["template_name"]
    cur = conn.execute(
        "UPDATE sheets SET template_name = ?, raw_extraction = ?, sheet_data = ?, "
        "status = 'extracted', "
        "extracted_at = ?, revision = revision + 1 WHERE uid = ? AND status = 'pending'",
        (template_name, json.dumps(extraction, ensure_ascii=False, default=str),
         json.dumps(extraction, ensure_ascii=False, default=str), now_iso(), uid),
    )
    conn.commit()
    return cur.rowcount == 1


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
        "SELECT uid, sheet_no, template_name, status, image_path, created_at, validated_at, revision, "
        "  json_extract(sheet_data, '$.header.operador')      AS operador, "
        "  json_extract(sheet_data, '$.header.data')          AS data_folha, "
        "  json_extract(sheet_data, '$.header.setor_maquina') AS setor, "
        "  json_extract(raw_extraction, '$._blank_page')      AS blank_page "
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


def save_sheet_data_with_edits(
    conn: sqlite3.Connection,
    uid: str,
    sheet_data: dict,
    expected_revision: int,
    edits: list[tuple[str, object, object, str, str]],
    *,
    cross_check: dict | None = None,
    write_cross: bool = False,
) -> bool:
    """Grava dados, auditoria e opcionalmente o cross no mesmo commit CAS.

    Serve tanto decisões humanas (a proteção não pode ficar separada do valor)
    como correções do cross (o resultado final não pode ficar separado dos
    valores que descreve).
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        data_json = json.dumps(sheet_data, ensure_ascii=False, default=str)
        if write_cross:
            cur = conn.execute(
                "UPDATE sheets SET sheet_data = ?, cross_check = ?, "
                "status = 'in_review', revision = revision + 1 "
                "WHERE uid = ? AND revision = ? AND status != 'validated'",
                (data_json,
                 json.dumps(cross_check, ensure_ascii=False, default=str),
                 uid, expected_revision),
            )
        else:
            cur = conn.execute(
                "UPDATE sheets SET sheet_data = ?, status = 'in_review', "
                "revision = revision + 1 "
                "WHERE uid = ? AND revision = ? AND status != 'validated'",
                (data_json, uid, expected_revision),
            )
        if cur.rowcount != 1:
            conn.rollback()
            return False
        edited_at = now_iso()
        conn.executemany(
            "INSERT INTO edits (sheet_uid, field_path, old_value, new_value, "
            "source, actor, edited_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    uid,
                    path,
                    None if old is None else str(old),
                    None if new is None else str(new),
                    source,
                    actor,
                    edited_at,
                )
                for path, old, new, source, actor in edits
            ],
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def apply_cross_corrections(
    conn: sqlite3.Connection,
    uid: str,
    sheet_data: dict,
    cross_check: dict,
    expected_revision: int,
    edits: list[tuple[str, object, object, str]],
) -> bool:
    """Wrapper tipado para o commit atómico das correções do cross."""
    return save_sheet_data_with_edits(
        conn, uid, sheet_data, expected_revision,
        [(path, old, new, "system", actor) for path, old, new, actor in edits],
        cross_check=cross_check,
        write_cross=True,
    )


def set_image_rotation(conn: sqlite3.Connection, uid: str, rotation: int) -> int:
    """Rotação manual pedida pelo humano, em quartos de volta no sentido horário.

    Normalizada a {0, 90, 180, 270}. É um pedido *adicional* à correcção
    automática: 0 não quer dizer «não rodes», quer dizer «a automática chega».
    """
    norm = (int(rotation) % 360 // 90) * 90
    conn.execute("UPDATE sheets SET image_rotation = ? WHERE uid = ?", (norm, uid))
    conn.commit()
    return norm


def save_cross_check(conn: sqlite3.Connection, uid: str, cross: dict,
                     expected_revision: int | None = None) -> bool:
    """Grava o cruzamento. Com `expected_revision`, só se a folha ainda for a
    mesma sobre a qual ele foi calculado — um cross velho a sobrepor-se ao
    novo pintava cores calculadas sobre valores que já não existem."""
    sql = "UPDATE sheets SET cross_check = ? WHERE uid = ? AND status != 'validated'"
    args: list = [json.dumps(cross, ensure_ascii=False, default=str), uid]
    if expected_revision is not None:
        sql += " AND revision = ?"
        args.append(expected_revision)
    cur = conn.execute(sql, args)
    conn.commit()
    return cur.rowcount == 1


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
    # `old IS NOT new` exclui edições no-op (ex.: gravar vazio numa célula já
    # vazia, o clique-e-Enter distraído do revisor): um no-op não é uma
    # decisão, e contá-lo desligava a herança daquela célula para sempre
    # (caso real 1fb333b28059, cliente None→None três vezes seguidas).
    for r in conn.execute(
        "SELECT field_path FROM edits WHERE sheet_uid = ? AND source = 'human' "
        "AND old_value IS NOT new_value", (uid,)
    ).fetchall():
        path = r["field_path"]
        if path.startswith("rows["):
            idx_s, _, fname = path[5:].partition("].")
            try:
                out.setdefault(int(idx_s), set()).add(fname)
            except ValueError:
                continue
    return out


def human_header_fields(conn: sqlite3.Connection, uid: str) -> set[str]:
    """Campos do cabeçalho corrigidos à mão — o motor não lhes toca.

    Se o revisor escreveu o nome do operador, foi uma decisão: substituí-lo
    pelo nome da lista seria desfazê-la.
    """
    return {
        r["field_path"][len("header."):]
        for r in conn.execute(
            "SELECT field_path FROM edits WHERE sheet_uid = ? AND source = 'human' "
            "AND field_path LIKE 'header.%'", (uid,)
        ).fetchall()
    }


def mark_validated(conn: sqlite3.Connection, uid: str, actor: str,
                   expected_revision: int | None = None) -> bool:
    sql = (
        "UPDATE sheets SET status = 'validated', validated_at = ?, validated_by = ? "
        "WHERE uid = ? AND status != 'validated'"
    )
    args: list[object] = [now_iso(), actor, uid]
    if expected_revision is not None:
        sql += " AND revision = ?"
        args.append(expected_revision)
    cur = conn.execute(sql, args)
    conn.commit()
    return cur.rowcount == 1


def ingested_shas(conn: sqlite3.Connection) -> set[str]:
    """sha256 dos PDFs do Drive já processados — a versão conta, o nome não."""
    return {r[0] for r in conn.execute("SELECT sha256 FROM ingested_files")}


def record_ingested(conn: sqlite3.Connection, filename: str, sha256: str,
                    n_pages: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO ingested_files (filename, sha256, n_pages, ingested_at) "
        "VALUES (?, ?, ?, ?)",
        (filename, sha256, n_pages, now_iso()),
    )
    conn.commit()


def image_path_in_use(conn: sqlite3.Connection, image_path: str) -> bool:
    """Outra folha ainda aponta para este ficheiro? Uploads repetidos e
    frente/verso partilham o mesmo PNG — apagá-lo com o rascunho deixava a
    folha irmã sem foto e sem prova de auditoria."""
    return conn.execute(
        "SELECT 1 FROM sheets WHERE image_path = ? LIMIT 1", (image_path,)
    ).fetchone() is not None


def image_sha_exists(conn: sqlite3.Connection, sha256: str) -> bool:
    """Já existe uma folha com esta imagem? Protege o ingest de duplicar
    páginas que entraram por upload manual (ex.: os lotes de 06 e 10-08)."""
    return conn.execute(
        "SELECT 1 FROM sheets WHERE image_sha256 = ? LIMIT 1", (sha256,)
    ).fetchone() is not None


def edit_count(conn: sqlite3.Connection, uid: str) -> int:
    return conn.execute(
        "SELECT count(*) FROM edits WHERE sheet_uid = ? AND source = 'human'", (uid,)
    ).fetchone()[0]
