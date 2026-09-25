"""A ÚNICA porta de escrita para o Postgres.

Chamado pelo sync_worker depois da validação humana: insere a folha validada e
as suas linhas em mes_kanban (append-only). Tudo o resto da app só lê do Postgres.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import PurePath

import psycopg

from . import pg
from .matching import similarity as sim
from .production_facts import materialize_sheet
from .validation_warnings import for_row as warnings_for_row
from .templates_spec import LEGACY_FIELD_ALIASES, KanbanTemplate, is_marked

APP_VERSION = "kanban-mes 0.1.0"

# campos da linha kanban → colunas de mes_kanban.production_records
_FIELD_TO_COLUMN = {
    "of": "production_order",
    "ov": "sales_order",
    "cliente": "customer_name",
    "modelo": "model_ref",
    "nesting": "model_ref",
    "perfil": "profile_type",
    "qtd": "quantity",
    "repeticoes": "quantity",
    "perf_comp": "full_profile",
    "comp_mm": "length_mm",     # chapa: aqui é mesmo um comprimento
    "larg_mm": "width_mm",
    "esp": "thickness_mm",
    "lote": "lot_ref",
    "sucata": "scrap",
}
_NUMERIC_COLUMNS = {"quantity", "length_mm", "width_mm", "thickness_mm", "plan_quantity"}
_BOOLEAN_COLUMNS = {"full_profile"}

# Colunas acrescentadas por migrações posteriores ao primeiro schema. A app
# sonda-as a cada validação em vez de as assumir: assim a ordem entre o deploy
# do código e a aplicação do SQL deixa de importar. Sem cache de propósito —
# a sonda é um SELECT ao information_schema por validação (raras), e a cache
# fixava para sempre o schema visto na primeira validação do processo.
_OPTIONAL_COLUMNS = ("profile_type", "full_profile", "plan_quantity",
                     "plan_length_mm", "line_meters", "meters_produced",
                     "plan_snapshot_id")

SOURCE_APP = "kanban-mes"
# Numeração pública atribuída pelo histórico (como nos perfis): um número
# provisório local já ocupado deixa de ser um erro que o operador não
# consegue resolver. Chave própria desta app para o bloqueio consultivo.
_SHEET_NUMBER_LOCK_KEY = 323417719601
_SHEET_NUMBER_INDEX = "validated_sheets_source_app_sheet_no_uidx"
_STORE_ATTEMPTS = 3


def _dsn() -> str:
    return pg.dsn()


# dd/mm/aaaa, dd-mm-aa, aaaa-mm-dd… — o que os operadores escrevem de facto
_DATE_DMY = re.compile(r"^\s*(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\s*$")
_DATE_ISO = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*$")


class InvalidSheetDate(ValueError):
    """Data manuscrita que não se consegue interpretar — o chamador decide
    como a devolver ao utilizador (422, não 500)."""


class SheetNumberConflict(RuntimeError):
    """O número público local já pertence a outro UID no PostgreSQL."""

    def __init__(self, sheet_no: object):
        self.sheet_no = sheet_no
        super().__init__(f"número público {sheet_no} já utilizado")


class SheetNumberingConfigurationError(RuntimeError):
    """O histórico não oferece o contrato necessário para numerar folhas."""


class SheetIdentityConflict(RuntimeError):
    """O UID já existe no histórico, mas não pertence a esta aplicação."""


@dataclass(frozen=True)
class StoredSheetResult:
    row_count: int
    sheet_no: int
    next_sheet_no: int
    already_stored: bool


def normalize_sheet_date(raw: object) -> str:
    """Data manuscrita → ISO (aaaa-mm-dd), SEMPRE dia/mês/ano à portuguesa.

    Antes ia crua para a coluna `date` e era o Postgres a adivinhar — com
    `DateStyle MDY`, «06/08/2026» ficou gravado como 8 de junho (aconteceu na
    primeira folha validada). Datas ambíguas cá dentro não existem: quem
    escreve 06/08 numa fábrica portuguesa quer dizer 6 de agosto.
    """
    text = str(raw or "").strip()
    m = _DATE_ISO.match(text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = _DATE_DMY.match(text)
        if not m:
            raise InvalidSheetDate(text)
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
    try:
        return date(y, mo, d).isoformat()
    except ValueError as exc:
        raise InvalidSheetDate(text) from exc


_SCHEMA_TABLES = ("validated_sheets", "production_records",
                  "production_record_plan_refs", "stoppage_records")
_INDEXES = "__indexes__"


def _schema(cur) -> dict[str, set[str]]:
    """Colunas das tabelas de escrita e índice de numeração numa só ida.

    Continua a ser sondado em cada gravação (a ordem entre o deploy do código e
    o SQL não pode importar), mas numa consulta e não numa por tabela: pelo
    túnel da fábrica cada ida custa ~70 ms.
    """
    cur.execute(
        "SELECT table_name::text, column_name::text FROM information_schema.columns "
        "WHERE table_schema = 'mes_kanban' AND table_name = ANY(%s) "
        "UNION ALL "
        "SELECT %s, indexname::text FROM pg_indexes "
        "WHERE schemaname = 'mes_kanban' AND tablename = 'validated_sheets' "
        "AND indexname = %s",
        (list(_SCHEMA_TABLES), _INDEXES, _SHEET_NUMBER_INDEX),
    )
    schema: dict[str, set[str]] = {name: set() for name in (*_SCHEMA_TABLES, _INDEXES)}
    for table, column in cur.fetchall():
        schema.setdefault(table, set()).add(column)
    return schema


def _insert_returning_ids(cur, sql: str, params_seq: list[tuple]) -> list[int]:
    """INSERT … RETURNING id de várias linhas numa só ida (pipeline)."""
    if not params_seq:
        return []
    cur.executemany(sql, params_seq, returning=True)
    ids = []
    while True:
        ids.append(cur.fetchone()[0])
        if not cur.nextset():
            return ids


# Como o upload/ingest gravam as páginas de PDF: {sha16}_{stem-do-pdf}_pNN.png
# (ver _save_image e _pdf_to_images em app/web/main.py). Fotos têm nomes
# livres e ficam de fora — inventar-lhes um "PDF de origem" seria falsificar
# a auditoria.
_SOURCE_RENDER_RE = re.compile(
    r"^[0-9a-f]{16}_(?P<stem>.+)_p(?P<page>\d{1,4})\.[A-Za-z0-9]{1,5}$"
)


def source_from_image_path(image_path: object) -> tuple[str | None, int | None]:
    """Proveniência (PDF de origem, página 1-based) derivada do nome do render.

    O staging não guarda o nome do PDF à parte, mas o render preserva-o no
    próprio nome de ficheiro. Sem match (foto, folha manual), fica NULL — a
    proveniência é auditoria de origem, nunca uma referência para a data.
    """
    name = PurePath(str(image_path or "")).name
    match = _SOURCE_RENDER_RE.match(name)
    if not match:
        return None, None
    return f"{match.group('stem')}.pdf", int(match.group("page"))


def _store_stoppages(cur, sheet: dict, header: dict, filled: list,
                     sheet_date: str | None, operator: str,
                     operator_pernr: str | None = None,
                     validated_at: str | None = None) -> int:
    """Linhas do verso da folha (paragens) → mes_kanban.stoppage_records."""
    machine = str(header.get("setor_maquina") or "").strip() or None
    params = [
        (
            sheet["uid"], i, sheet_date, machine,
            operator or "(desconhecido)",
            str(row.get("motivo") or "").strip() or None,
            str(row.get("inicio") or "").strip() or None,
            str(row.get("fim") or "").strip() or None,
            sim.parse_number(row.get("duracao")),
            str(row.get("resolvido") or "").strip() or None,
            operator_pernr, validated_at,
        )
        for i, row in filled
    ]
    if params:
        # executemany envia as linhas em pipeline: uma ida para todas
        cur.executemany(
            """
            INSERT INTO mes_kanban.stoppage_records
                (sheet_uid, row_index, sheet_date, machine, operator_name,
                 motivo, inicio, fim, duracao_horas, resolvido,
                 operator_pernr, validated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    COALESCE(%s::timestamptz, now()))
            """,
            params,
        )
    return len(params)


def _numbering_schema_ready(schema: dict[str, set[str]]) -> bool:
    return ({"source_app", "sheet_no"} <= schema["validated_sheets"]
            and _SHEET_NUMBER_INDEX in schema[_INDEXES])


def _next_number(cur, sheet_no: int, minimum_sheet_no: int) -> int:
    cur.execute(
        "SELECT COALESCE(MAX(sheet_no), 0) FROM mes_kanban.validated_sheets "
        "WHERE source_app = %s", (SOURCE_APP,),
    )
    return max(int(cur.fetchone()[0] or 0), sheet_no, minimum_sheet_no - 1) + 1


def _existing(cur, sheet: dict, minimum_sheet_no: int) -> StoredSheetResult | None:
    """A folha JÁ está no Postgres (o commit anterior confirmou e o que falhou
    foi a confirmação local): devolve-se o que existe, sem duplicar."""
    cur.execute(
        "SELECT source_app, sheet_no FROM mes_kanban.validated_sheets "
        "WHERE sheet_uid = %s",
        (sheet["uid"],),
    )
    existing = cur.fetchone()
    if not existing:
        return None
    source_app, raw_sheet_no = existing
    if source_app != SOURCE_APP:
        raise SheetIdentityConflict(
            f"a folha {sheet['uid']} já existe no histórico com source_app={source_app!r}")
    try:
        historic_sheet_no = int(raw_sheet_no)
    except (TypeError, ValueError) as exc:
        raise SheetIdentityConflict(
            f"a folha {sheet['uid']} existe no histórico sem número válido") from exc
    if historic_sheet_no < 1:
        raise SheetIdentityConflict(
            f"a folha {sheet['uid']} existe no histórico sem número válido")
    cur.execute(
        "SELECT (SELECT count(*) FROM mes_kanban.production_records WHERE sheet_uid = %s), "
        "       (SELECT count(*) FROM mes_kanban.stoppage_records WHERE sheet_uid = %s)",
        (sheet["uid"], sheet["uid"]),
    )
    n_prod, n_stop = cur.fetchone()
    return StoredSheetResult(int(n_prod or n_stop), historic_sheet_no,
                             _next_number(cur, historic_sheet_no, minimum_sheet_no), True)


def store_validated_sheet(sheet: dict, template: KanbanTemplate,
                          edit_count: int, actor: str, *,
                          minimum_sheet_no: int = 1,
                          validated_at: str | None = None) -> StoredSheetResult:
    """Grava uma folha e atribui o número definitivo no mesmo commit.

    ``validated_at`` é a hora do clique em Validar (a gravação pode ser
    minutos depois, pelo sync_worker). Idempotente pelo UID."""
    try:
        minimum_sheet_no = max(1, int(minimum_sheet_no))
    except (TypeError, ValueError):
        minimum_sheet_no = 1
    last_error: psycopg.errors.UniqueViolation | None = None
    for attempt in range(_STORE_ATTEMPTS):
        try:
            return _store_validated_sheet_once(
                sheet, template, edit_count, actor, minimum_sheet_no, validated_at)
        except psycopg.errors.UniqueViolation as exc:
            if exc.diag.constraint_name not in {
                    _SHEET_NUMBER_INDEX, "validated_sheets_pkey"}:
                raise
            last_error = exc
            if attempt + 1 == _STORE_ATTEMPTS:
                break
    raise SheetNumberConflict(sheet.get("sheet_no")) from last_error


def _store_validated_sheet_once(sheet: dict, template: KanbanTemplate,
                                edit_count: int, actor: str, minimum_sheet_no: int,
                                validated_at: str | None) -> StoredSheetResult:
    """Uma tentativa transacional de gravar a folha e as suas linhas.
    Lança em caso de erro — nada fica meio gravado."""
    data = sheet["sheet_data"] or {}
    header = data.get("header") or {}
    rows = data.get("rows") or []
    footer = data.get("footer") or {}
    cross = sheet.get("cross_check") or {}
    materialized = materialize_sheet(sheet, template)

    # Levanta InvalidSheetDate se a data não se interpretar — o chamador
    # transforma isso num 422 com mensagem, nunca num 500.
    sheet_date = normalize_sheet_date(header.get("data")) if str(header.get("data") or "").strip() else None
    operator = str(header.get("operador") or "").strip()
    # Identidade resolvida contra a lista de colaboradores (ver app/matching/operador.py).
    op_match = cross.get("operator") or {}
    operator_pernr = op_match.get("pernr") or None
    operator_rule = op_match.get("rule") or None
    # valor de folha (rodapé), desnormalizado para cada linha — é a única
    # coluna de horas no schema
    hours_worked = sim.parse_number(footer.get("horas_trabalhadas"))

    # linhas com conteúdo (pelo menos um campo preenchido)
    filled = [
        (i, row) for i, row in enumerate(rows)
        if row.get("_deleted") is not True
        and any(not str(k).startswith("_")
                and v is not None and str(v).strip() != ""
                for k, v in row.items())
    ]

    with pg.write_connection() as conn:
        with conn.cursor() as cur:
            # Proveniência sondada como as outras colunas opcionais: a ordem
            # entre deploy do código e aplicação do sql/016 não pode importar.
            schema = _schema(cur)
            if not _numbering_schema_ready(schema):
                raise SheetNumberingConfigurationError(
                    "faltam source_app, sheet_no ou o índice único de numeração")
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_SHEET_NUMBER_LOCK_KEY,))
            stored = _existing(cur, sheet, minimum_sheet_no)
            if stored is not None:
                return stored
            validated_present = schema["validated_sheets"]
            cur.execute(
                "SELECT COALESCE(MAX(sheet_no), 0) FROM mes_kanban.validated_sheets "
                "WHERE source_app = %s", (SOURCE_APP,),
            )
            historic_max = int(cur.fetchone()[0] or 0)
            try:
                proposed_no = int(sheet.get("sheet_no"))
            except (TypeError, ValueError):
                proposed_no = 0
            number_free = False
            if proposed_no > 0:
                cur.execute(
                    "SELECT 1 FROM mes_kanban.validated_sheets "
                    "WHERE source_app = %s AND sheet_no = %s",
                    (SOURCE_APP, proposed_no),
                )
                number_free = cur.fetchone() is None
            # O número local é só uma proposta: se já está usado no
            # histórico, o histórico dá o seguinte livre.
            sheet_no = (proposed_no if number_free else
                        max(minimum_sheet_no, historic_max + 1, 1))
            next_sheet_no = max(historic_max, sheet_no, minimum_sheet_no - 1) + 1
            source_columns = [
                name for name in (
                    "source_filename", "source_page", "source_app",
                    "sheet_no", "plan_snapshot_id",
                )
                if name in validated_present
            ]
            source_filename, source_page = source_from_image_path(
                sheet.get("image_path"))
            source_values = {"source_filename": source_filename,
                             "source_page": source_page,
                             "source_app": SOURCE_APP,
                             "sheet_no": sheet_no,
                             "plan_snapshot_id": cross.get("snapshot_id")}
            validated_columns = (
                "sheet_uid, sheet_date, template_name, family, operator_name, "
                "operator_no, sector_machine, shift, image_sha256, "
                "raw_extraction, sheet_data, cross_check, edit_count, "
                "validated_by, app_version, operator_pernr, operator_match_rule, validated_at"
                + "".join(f", {name}" for name in source_columns)
            )
            validated_values = (
                sheet["uid"], sheet_date, template.name, template.family,
                operator or "(desconhecido)",
                str(header.get("n_operador") or "") or None,
                str(header.get("setor_maquina") or "") or None,
                str(header.get("turno") or "") or None,
                sheet.get("image_sha256") or "",
                json.dumps(sheet.get("raw_extraction") or {}, ensure_ascii=False, default=str),
                json.dumps(data, ensure_ascii=False, default=str),
                json.dumps(cross, ensure_ascii=False, default=str),
                edit_count, actor, APP_VERSION,
                operator_pernr, operator_rule,
                # A hora do clique em Validar, não a da gravação por trás.
                validated_at or datetime.now(timezone.utc).isoformat(),
                *[source_values[name] for name in source_columns],
            )
            cur.execute(
                f"INSERT INTO mes_kanban.validated_sheets ({validated_columns}) "
                f"VALUES ({', '.join(['%s'] * len(validated_values))})",
                validated_values,
            )
            if template.name == "cantoneiras_paragens":
                n = _store_stoppages(cur, sheet, header, filled, sheet_date, operator,
                                     operator_pernr, validated_at)
                conn.commit()
                return StoredSheetResult(n, sheet_no, next_sheet_no, False)
            present = schema["production_records"]
            optional = [c for c in _OPTIONAL_COLUMNS if c in present]
            sql = (
                "INSERT INTO mes_kanban.production_records "
                "(sheet_uid, row_index, sheet_date, family, operator_name, "
                " machine, production_order, sales_order, customer_name, "
                " model_ref, matched_plan_key, match_confidence, "
                " quantity, length_mm, width_mm, thickness_mm, lot_ref, "
                " scrap, hours_worked, extra, operator_pernr, validated_at"
                + "".join(f", {c}" for c in optional)
                + ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                  "%s, %s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s::timestamptz, now())"
                + ", %s" * len(optional)
                + ") RETURNING id"
            )
            row_fields = set(template.row_fields)
            refs_by_row: dict[int, list[dict]] = {}
            for ref in materialized["plan_refs"]:
                refs_by_row.setdefault(ref["row_index"], []).append(ref)
            if refs_by_row and not schema["production_record_plan_refs"]:
                raise RuntimeError(
                    "Migração production_record_plan_refs ainda não foi aplicada."
                )
            record_params: list[tuple] = []
            record_rows: list[int] = []
            for fact in materialized["parents"]:
                i, row, cr = fact["row_index"], fact["row"], fact["cross"]
                cells = {c["field"]: c for c in cr.get("cells", [])}
                cols: dict[str, object] = {}
                extra: dict[str, object] = {"plan_identity": cr["plan_identity"]} if cr.get("plan_identity") else {}
                if cr.get("quantity_basis"):
                    extra["quantity_basis"] = cr["quantity_basis"]
                row_warnings = warnings_for_row(cross.get("validation_warnings"), i)
                if row_warnings:
                    extra["warnings"] = row_warnings
                for f, value in row.items():
                    if value is None or str(value).strip() == "":
                        continue
                    # folhas lidas antes de a coluna mudar de nome
                    f = LEGACY_FIELD_ALIASES.get(f, f) if f not in row_fields else f
                    col = _FIELD_TO_COLUMN.get(f)
                    if col is None:
                        extra[f] = value
                    elif col in _BOOLEAN_COLUMNS:
                        cols[col] = is_marked(value)
                    elif col in _NUMERIC_COLUMNS:
                        cols[col] = sim.parse_number(value)
                    else:
                        cols.setdefault(col, str(value).strip())
                if fact.get("aggregate"):
                    extra["full_profile_aggregate"] = True
                    extra["plan_ref_count"] = len(refs_by_row.get(i, ()))
                # Quantidade planeada da linha do plano que casou, para se poder
                # ver mais tarde porque é que uma quantidade foi assinalada.
                qtd_cell = cells.get("qtd") or {}
                if qtd_cell.get("plan_limit") is not None:
                    cols["plan_quantity"] = qtd_cell["plan_limit"]
                # Metros teóricos (qtd × comprimento do plano) e o total
                # manuscrito do rodapé — a base do controlo de desperdício.
                cols["plan_length_mm"] = cr.get("plan_length_mm")
                cols["line_meters"] = cr.get("line_meters")
                cols["meters_produced"] = sim.parse_number(
                    footer.get("metros_produzidos"))
                cols["plan_snapshot_id"] = (cr.get("quantity_basis") or {}).get("snapshot_id") or cross.get("snapshot_id")
                # OF/OV como números puros, a convenção do planeamento —
                # mesmo quando o valor veio do plano (com prefixo)
                for ref_col in ("production_order", "sales_order"):
                    if cols.get(ref_col):
                        cols[ref_col] = sim.strip_ref_prefix(cols[ref_col])

                machine = row.get("maquina") or header.get("setor_maquina")
                record_params.append((
                    sheet["uid"], i, sheet_date, template.family,
                    operator or "(desconhecido)",
                    str(machine).strip() if machine else None,
                    cols.get("production_order"), cols.get("sales_order"),
                    cols.get("customer_name"), cols.get("model_ref"),
                    cr.get("matched_plan_key"), cr.get("p_correct"),
                    cols.get("quantity"), cols.get("length_mm"),
                    cols.get("width_mm"), cols.get("thickness_mm"),
                    cols.get("lot_ref"), cols.get("scrap"),
                    hours_worked,
                    json.dumps(extra, ensure_ascii=False, default=str) if extra else None,
                    operator_pernr, validated_at,
                    *[cols.get(c) for c in optional],
                ))
                record_rows.append(i)
            # Todas as linhas numa ida (pipeline) em vez de uma ida por linha;
            # os ids voltam pela mesma ordem para ligar as referências.
            record_ids = _insert_returning_ids(cur, sql, record_params)
            ref_params = [
                (
                    production_record_id, sheet["uid"], i,
                    ref.get("plan_snapshot_id"), ref.get("plan_key"),
                    ref.get("component_ref"), ref.get("profile_type"),
                    ref.get("length_mm"), ref.get("quantity_planned"),
                    ref.get("quantity_made_before"),
                    ref.get("remaining_before"),
                    ref.get("overproduction_before"),
                    ref.get("assumed_quantity"),
                    ref.get("remaining_rule"),
                    json.dumps({"source_app": SOURCE_APP, "plan_identity": ref}, ensure_ascii=False, default=str),
                )
                for production_record_id, i in zip(record_ids, record_rows)
                for ref in refs_by_row.get(i, ())
            ]
            if ref_params:
                cur.executemany(
                    """
                    INSERT INTO mes_kanban.production_record_plan_refs
                        (production_record_id, sheet_uid, row_index,
                         plan_snapshot_id, plan_key, component_ref,
                         profile_type, length_mm, quantity_planned,
                         quantity_made_before, remaining_before,
                         overproduction_before, assumed_quantity,
                         remaining_rule, extra)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s)
                    """,
                    ref_params,
                )
            n = len(record_params)
        conn.commit()
    return StoredSheetResult(n, sheet_no, next_sheet_no, False)
