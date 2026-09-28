"""Avisos da validação: o que antes impedia validar passa a ficar registado.

Decisão de 25/09: nada de negócio bloqueia a validação. Cada antigo portão
(operador ou data em falta, linha sem candidato, saldo histórico, plano que
mudou…) é um aviso ``{code, message, row, row_index}`` guardado com a folha
— no SQLite e no Postgres — e visível no Histórico, para quem quiser rever
depois. O operador valida sempre.
"""

from __future__ import annotations

from .templates_spec import field_value, is_marked


def _meaningful(row: dict) -> bool:
    return any(
        not str(key).startswith("_") and value is not None and str(value).strip()
        for key, value in row.items()
    )


def collect(sheet: dict, *, current_snapshot: str | None = None,
            assumed_date: str | None = None,
            extra: list[dict] | tuple = ()) -> list[dict]:
    """Avisos da folha tal como vai ser gravada. Nunca lança nem bloqueia."""
    data = sheet.get("sheet_data") or {}
    header = data.get("header") or {}
    cross = sheet.get("cross_check") or {}
    warnings: list[dict] = []

    def add(code: str, message: str, row: int | None = None,
            row_index: int | None = None) -> None:
        item = {"code": code, "message": message}
        if row is not None:
            item.update(row=row, row_index=row_index)
        warnings.append(item)

    if not str(header.get("operador") or "").strip():
        add("operador_vazio", "Operador por preencher: gravado como «(desconhecido)».")
    check = (sheet.get("raw_extraction") or {}).get("_ocr_check") or {}
    if check.get("suspect"):
        add("leitura_suspeita",
            "Leitura do OCR suspeita (" + "; ".join(
                p.get("message", "") for p in check.get("first_problems") or []) + ").")
    if assumed_date:
        add("data_assumida",
            f"Data da folha assumida: {assumed_date} (dia útil anterior à digitalização).")
    for item in data.get("_canonical_warnings") or []:
        code = str(item.get("code") or "") if isinstance(item, dict) else ""
        if code.startswith("ultima_coluna_"):
            continue
        message = item.get("message") if isinstance(item, dict) else None
        add("conflito_migracao", message or "Conflito de migração por resolver.")
    for item in extra:
        warnings.append(dict(item))

    snapshot = cross.get("snapshot_id")
    plan_status = (cross.get("plan_reference") or {}).get("status")
    if plan_status == "no_reference":
        add("plano_indisponivel",
            "Plano indisponível ao validar: gravada com o cruzamento que estava na folha.")
    elif current_snapshot and snapshot and str(snapshot) != str(current_snapshot):
        add("plano_mudou",
            f"Validada com a carga do plano que estava aberta ({snapshot}); "
            f"entretanto entrou outra ({current_snapshot}).")
    if cross.get("engine") != "cross-v3" and cross.get("fixed_point") is False:
        add("cruzamento_instavel",
            "O cruzamento não estabilizou; revê a identidade das linhas.")

    checks = {r.get("row_index"): r for r in cross.get("rows", [])}
    visible = 0
    for i, row in enumerate(data.get("rows") or []):
        if row.get("_deleted") is True:
            continue
        visible += 1
        if not _meaningful(row):
            continue
        rc = checks.get(i) or {}
        if rc.get("row_kind") in {"activity", "empty"} or rc.get("mode") in {"activity", "empty"}:
            continue
        if row.get("_identity_unresolved"):
            add("identidade_por_confirmar", f"Linha {visible}: {row['_identity_unresolved']}",
                visible, i)
        if rc.get("binding_status") == "stale" or rc.get("binding_stale"):
            add("escolha_de_outra_carga",
                f"Linha {visible}: a referência foi escolhida noutra carga do plano; confirma-a.",
                visible, i)
        if not rc.get("matched_plan_key") and not rc.get("plan_refs"):
            add("sem_ligacao_ao_plano",
                f"Linha {visible}: sem correspondência no plano; gravada sem ligação.",
                visible, i)
        elif rc.get("mode") == "weak_guess" or rc.get("review_required"):
            add("correspondencia_fraca",
                f"Linha {visible}: correspondência fraca com o plano; confirma OF e perfil.",
                visible, i)
        if is_marked(field_value(row, "perf_comp")):
            basis = rc.get("quantity_basis") or {}
            if not rc.get("plan_refs_valid") or basis.get("status") != "ready":
                reason = rc.get("plan_refs_error") or "saldo histórico indisponível"
                add("saldo_por_confirmar",
                    f"Linha {visible}: {reason.rstrip('.')} — quantidade por confirmar.",
                    visible, i)
            elif basis.get("approximate"):
                add("saldo_aproximado",
                    f"Linha {visible}: saldo da primeira carga do plano com esta OF "
                    f"({basis.get('snapshot_id')}), posterior ao dia de produção.",
                    visible, i)
    return warnings


def for_row(warnings: list[dict] | None, row_index: int) -> list[dict]:
    """Avisos de uma linha, para o extra do registo de produção."""
    return [
        {"code": w["code"], "message": w["message"]}
        for w in warnings or () if w.get("row_index") == row_index
    ]


_BALANCE_CODES = frozenset({"saldo_por_confirmar", "saldo_aproximado"})


def refresh_balance(warnings: list[dict] | None, sheet: dict) -> list[dict]:
    """Depois de o sync_worker completar o saldo histórico: os avisos de saldo
    passam a refletir o saldo calculado; os restantes ficam como estavam."""
    kept = [w for w in warnings or () if w.get("code") not in _BALANCE_CODES]
    fresh = [w for w in collect(sheet) if w.get("code") in _BALANCE_CODES]
    return kept + fresh
