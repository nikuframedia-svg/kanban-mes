"""A ÚNICA porta de escrita para o Postgres.

Chamado exclusivamente no ato de validação humana: insere a folha validada e as
suas linhas em mes_kanban (append-only). Tudo o resto da app só lê do Postgres.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import PurePath

import psycopg

from .config import settings
from .matching import similarity as sim
from .production_facts import materialize_sheet
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


def _dsn() -> str:
    return os.environ.get("MES_PG_DSN") or settings.pg_dsn


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


def _columns_present(cur, table: str = "production_records") -> set[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'mes_kanban' AND table_name = %s",
        (table,),
    )
    return {r[0] for r in cur.fetchall()}


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
                     operator_pernr: str | None = None) -> int:
    """Linhas do verso da folha (paragens) → mes_kanban.stoppage_records."""
    machine = str(header.get("setor_maquina") or "").strip() or None
    n = 0
    for i, row in filled:
        cur.execute(
            """
            INSERT INTO mes_kanban.stoppage_records
                (sheet_uid, row_index, sheet_date, machine, operator_name,
                 motivo, inicio, fim, duracao_horas, resolvido,
                 operator_pernr, validated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            """,
            (
                sheet["uid"], i, sheet_date, machine,
                operator or "(desconhecido)",
                str(row.get("motivo") or "").strip() or None,
                str(row.get("inicio") or "").strip() or None,
                str(row.get("fim") or "").strip() or None,
                sim.parse_number(row.get("duracao")),
                str(row.get("resolvido") or "").strip() or None,
                operator_pernr,
            ),
        )
        n += 1
    return n


def store_validated_sheet(sheet: dict, template: KanbanTemplate,
                          edit_count: int, actor: str) -> int:
    """Insere a folha + linhas. Devolve o nº de linhas de produção gravadas.
    Lança em caso de erro — o chamador não deve marcar a folha como validada
    sem este INSERT ter sido confirmado."""
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

    with psycopg.connect(_dsn(), connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM mes_kanban.validated_sheets WHERE sheet_uid = %s",
                (sheet["uid"],),
            )
            if cur.fetchone():
                # A folha JÁ está no Postgres (o INSERT anterior confirmou e o
                # que falhou foi marcar o staging): o commit é atómico, por
                # isso as linhas também lá estão — repetir daria colisão de PK
                # e um 500 permanente. Devolve-se o que existe.
                cur.execute(
                    "SELECT count(*) FROM mes_kanban.production_records WHERE sheet_uid = %s",
                    (sheet["uid"],),
                )
                n_prod = cur.fetchone()[0]
                if n_prod:
                    return n_prod
                cur.execute(
                    "SELECT count(*) FROM mes_kanban.stoppage_records WHERE sheet_uid = %s",
                    (sheet["uid"],),
                )
                return cur.fetchone()[0]
            # Proveniência sondada como as outras colunas opcionais: a ordem
            # entre deploy do código e aplicação do sql/016 não pode importar.
            validated_present = _columns_present(cur, "validated_sheets")
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
                             "sheet_no": sheet.get("sheet_no"),
                             "plan_snapshot_id": cross.get("snapshot_id")}
            validated_columns = (
                "sheet_uid, sheet_date, template_name, family, operator_name, "
                "operator_no, sector_machine, shift, image_sha256, "
                "raw_extraction, sheet_data, cross_check, edit_count, "
                "validated_by, app_version, operator_pernr, operator_match_rule"
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
                *[source_values[name] for name in source_columns],
            )
            sheet_no = sheet.get("sheet_no")
            if ({"source_app", "sheet_no"}.issubset(validated_present)
                    and sheet_no is not None):
                cur.execute(
                    "SELECT sheet_uid FROM mes_kanban.validated_sheets "
                    "WHERE source_app = %s AND sheet_no = %s AND sheet_uid <> %s",
                    (SOURCE_APP, sheet_no, sheet["uid"]),
                )
                if cur.fetchone():
                    raise SheetNumberConflict(sheet_no)
            try:
                cur.execute(
                    f"INSERT INTO mes_kanban.validated_sheets ({validated_columns}) "
                    f"VALUES ({', '.join(['%s'] * len(validated_values))})",
                    validated_values,
                )
            except psycopg.errors.UniqueViolation as exc:
                # Cobre duas validações concorrentes entre a sonda e o INSERT.
                if exc.diag.constraint_name == "validated_sheets_source_app_sheet_no_uidx":
                    raise SheetNumberConflict(sheet_no) from exc
                raise
            if template.name == "cantoneiras_paragens":
                n = _store_stoppages(cur, sheet, header, filled, sheet_date, operator,
                                     operator_pernr)
                conn.commit()
                return n
            present = _columns_present(cur)
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
                  "%s, %s, %s, %s, %s, %s, %s, %s, %s, now()"
                + ", %s" * len(optional)
                + ") RETURNING id"
            )
            row_fields = set(template.row_fields)
            refs_by_row: dict[int, list[dict]] = {}
            for ref in materialized["plan_refs"]:
                refs_by_row.setdefault(ref["row_index"], []).append(ref)
            if refs_by_row and not _columns_present(cur, "production_record_plan_refs"):
                raise RuntimeError(
                    "Migração production_record_plan_refs ainda não foi aplicada."
                )
            n = 0
            for fact in materialized["parents"]:
                i, row, cr = fact["row_index"], fact["row"], fact["cross"]
                cells = {c["field"]: c for c in cr.get("cells", [])}
                cols: dict[str, object] = {}
                extra: dict[str, object] = {"plan_identity": cr["plan_identity"]} if cr.get("plan_identity") else {}
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
                cols["plan_snapshot_id"] = cross.get("snapshot_id")
                # OF/OV como números puros, a convenção do planeamento —
                # mesmo quando o valor veio do plano (com prefixo)
                for ref_col in ("production_order", "sales_order"):
                    if cols.get(ref_col):
                        cols[ref_col] = sim.strip_ref_prefix(cols[ref_col])

                machine = row.get("maquina") or header.get("setor_maquina")
                cur.execute(
                    sql,
                    (
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
                        operator_pernr,
                        *[cols.get(c) for c in optional],
                    ),
                )
                production_record_id = cur.fetchone()[0]
                for ref in refs_by_row.get(i, ()):
                    cur.execute(
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
                        ),
                    )
                n += 1
        conn.commit()
    return n
