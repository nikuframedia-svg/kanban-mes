"""Escolhas explícitas de referência que sobrevivem a uma carga nova do plano.

A chave de uma linha do plano inclui a carga (``mtg2_x:plan:4167``): depois de
cada carga do Excel, todas as escolhas feitas até aí deixavam de existir e a
validação obrigava a reabrir Referências. A escolha passa a guardar também a
identidade da linha (OF, referência, perfil, comprimento) e é religada à mesma
linha na carga atual quando há exatamente uma igual. Escolhas antigas, sem
identidade, continuam a valer só na sua carga (a validação avisa).
"""

from __future__ import annotations

from . import similarity as sim


def _value(entry: dict, *keys: str):
    for key in keys:
        if entry.get(key) not in (None, ""):
            return entry.get(key)
    return None


def identity_of(entry: dict) -> dict:
    """Identidade de uma linha do plano, venha do índice ou de uma consulta."""
    return {
        "of": _value(entry, "of", "production_order_no"),
        "modelo": _value(entry, "modelo", "component_ref"),
        "perfil": _value(entry, "perfil", "profile_type"),
        "comp_mm": _value(entry, "comp_mm", "length_mm"),
    }


def _same_line(index, entry: dict, identity: dict) -> bool:
    for field in ("modelo", "perfil"):
        wanted = identity.get(field)
        if wanted not in (None, "") and not index.same_identity(field, wanted, entry.get(field)):
            return False
    wanted_length = sim.parse_number(identity.get("comp_mm"))
    length = sim.parse_number(entry.get("comp_mm"))
    return wanted_length is None or (length is not None and abs(length - wanted_length) <= 1)


def reattach(bindings: dict[int, dict], index) -> dict[int, dict]:
    """Escolhas feitas noutra carga, religadas à mesma linha da carga atual."""
    current = str(getattr(index, "snapshot_id", None) or "")
    if not current or not bindings:
        return bindings
    key_field = index.spec.key_field
    out: dict[int, dict] = {}
    for row_index, binding in bindings.items():
        identity = binding.get("identity") if isinstance(binding, dict) else None
        if not identity or str(binding.get("snapshot_id") or "") == current:
            out[row_index] = binding
            continue
        hits = sorted(index.exact_matches("of", str(identity.get("of") or "")))
        matches = [index.entries[i] for i in hits if _same_line(index, index.entries[i], identity)]
        if len(matches) == 1:
            out[row_index] = {
                **binding, "snapshot_id": current,
                "plan_key": str(matches[0].get(key_field)),
                "reattached_from": {"snapshot_id": binding.get("snapshot_id"),
                                    "plan_key": binding.get("plan_key")},
            }
        else:
            out[row_index] = binding
    return out
