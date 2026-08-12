"""A ÚNICA porta de escrita para o Postgres.

Chamado exclusivamente no ato de validação humana: insere a folha validada e as
suas linhas em mes_kanban (append-only). Tudo o resto da app só lê do Postgres.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date

import psycopg

from .config import settings
from .matching import carryover
from .matching import similarity as sim
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
_OPTIONAL_COLUMNS = ("profile_type", "full_profile", "plan_quantity")


def _dsn() -> str:
    return os.environ.get("MES_PG_DSN") or settings.pg_dsn


# dd/mm/aaaa, dd-mm-aa, aaaa-mm-dd… — o que os operadores escrevem de facto
_DATE_DMY = re.compile(r"^\s*(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\s*$")
_DATE_ISO = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*$")


class InvalidSheetDate(ValueError):
    """Data manuscrita que não se consegue interpretar — o chamador decide
    como a devolver ao utilizador (422, não 500)."""


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


def _columns_present(cur) -> set[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'mes_kanban' AND table_name = 'production_records'"
    )
    return {r[0] for r in cur.fetchall()}


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
    cross_rows = {r["row_index"]: r for r in cross.get("rows", [])}

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
        if any(v is not None and str(v).strip() != "" for v in row.values())
    ]

    # Identidade herdada resolvida aqui, não lida das células do cross: o
    # `cliente` das cantoneiras não cruza com o plano (fora do IndexSpec),
    # logo não tem célula — e as linhas herdadas iam para o Postgres com
    # customer_name NULL. O carryover é a fonte de verdade da herança.
    identities = carryover.resolve(
        rows, tuple(f for f in template.row_fields if f not in carryover.CARRY_FIELDS),
    ) if template.name != "cantoneiras_paragens" else []

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
            cur.execute(
                """
                INSERT INTO mes_kanban.validated_sheets
                    (sheet_uid, sheet_date, template_name, family, operator_name,
                     operator_no, sector_machine, shift, image_sha256,
                     raw_extraction, sheet_data, cross_check, edit_count,
                     validated_by, app_version, operator_pernr, operator_match_rule)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
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
                ),
            )
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
                + ")"
            )
            row_fields = set(template.row_fields)
            n = 0
            for i, row in filled:
                cr = cross_rows.get(i) or {}
                cells = {c["field"]: c for c in cr.get("cells", [])}
                cols: dict[str, object] = {}
                extra: dict[str, object] = {}
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
                # Identidade herdada da linha de cima: no staging fica em branco
                # (é o que está no papel), mas aqui tem de ser explícita, senão
                # a linha chega ao Postgres sem OF. Vem do carryover (não das
                # células do cross: o cliente não cruza e não tem célula). A
                # proveniência fica em `extra` para se saber depois o que foi
                # escrito e o que foi lido.
                identity = identities[i] if i < len(identities) else None
                inherited: dict[str, int] = {}
                if identity is not None:
                    for f, col in (("of", "production_order"), ("ov", "sales_order"),
                                   ("cliente", "customer_name")):
                        if not cols.get(col) and identity.is_inherited(f):
                            cols[col] = str(identity.values[f]).strip()
                            inherited[f] = identity.inherited_from[f]
                if inherited:
                    extra["identidade_herdada"] = inherited
                # Quantidade planeada da linha do plano que casou, para se poder
                # ver mais tarde porque é que uma quantidade foi assinalada.
                qtd_cell = cells.get("qtd") or {}
                if qtd_cell.get("plan_limit") is not None:
                    cols["plan_quantity"] = qtd_cell["plan_limit"]

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
                n += 1
        conn.commit()
    return n
