"""Explicit OF/reference selection shared by the two review interfaces."""
from copy import deepcopy
import logging
from typing import Literal

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import db
from ..matching import bindings as plan_bindings
from ..matching import loaders, similarity as sim
from ..matching.params import CrossParams
from ..matching.scorer import Scorer
from ..templates_spec import field_value, get_template, is_marked
from . import plan_review, review_writes
from ..review_guard import cancel_pending


class PlanSelection(BaseModel):
    revision: int = Field(ge=0)
    snapshot_id: str = Field(min_length=1, max_length=200)
    selection_kind: Literal["reference", "profile"]
    plan_key: str = Field(min_length=1, max_length=500)
    back: str = ""
    review_token: str = ""


def register(app, conn_fn, get_index, run_cross_check, sheet_location):
    @app.get("/sheet/{uid}/of-lookup")
    def of_lookup(uid: str, row_index: int | None = None, q: str = "", include_done: bool = False, offset: int = 0):
        conn = conn_fn()
        try:
            sheet = db.get_sheet(conn, uid)
        finally:
            conn.close()
        rows = ((sheet or {}).get("sheet_data") or {}).get("rows") or []
        if not sheet or (row_index is not None and (not 0 <= row_index < len(rows) or rows[row_index].get("_deleted"))):
            raise HTTPException(404)
        if not get_template(sheet["template_name"]).index_loader:
            raise HTTPException(422, "Esta folha não usa referências do planeamento.")
        if offset < 0 or offset > 100000:
            raise HTTPException(422, "Página inválida.")
        try:
            info = loaders.plan_snapshot_info()
            sid = str(info.get("snapshot_id") or "")
            if not sid:
                return JSONResponse({"detail": "Planeamento indisponível."}, status_code=503)
            result = plan_review.lookup(sid, q, include_done=include_done, offset=offset)
            result.update(revision=sheet["revision"], review_token=review_writes.token(sheet), mtg2=plan_review.IS_MTG2,
                          selection_kind="profile" if row_index is not None and is_marked(field_value(rows[row_index], "perf_comp")) else "reference")
            return JSONResponse(jsonable_encoder(result))
        except Exception:
            return JSONResponse({"detail": "Não foi possível pesquisar o planeamento. Tenta novamente."}, status_code=503)

    @app.post("/sheet/{uid}/rows/{row_index}/plan-selection")
    def select_plan(uid: str, row_index: int, payload: PlanSelection):
        conn = conn_fn()
        saved = False
        cancel_pending(uid)
        try:
            sheet = db.get_sheet(conn, uid)
            if not sheet:
                raise HTTPException(404, "Folha inexistente.")
            if sheet["status"] == "validated":
                raise HTTPException(409, "Folha validada é só de leitura.")
            rows = (sheet.get("sheet_data") or {}).get("rows") or []
            if not 0 <= row_index < len(rows) or rows[row_index].get("_deleted"):
                raise HTTPException(404, "Linha inexistente.")
            payload.revision = review_writes.revision_for(sheet, payload.revision, payload.review_token)
            if sheet["revision"] != payload.revision:
                raise HTTPException(409, "A folha foi alterada. Reabre a pesquisa para confirmar a escolha.")
            template = get_template(sheet["template_name"])
            if not template.index_loader:
                raise HTTPException(422, "Esta folha não usa referências do planeamento.")
            full = is_marked(field_value(rows[row_index], "perf_comp"))
            if full != (payload.selection_kind == "profile"):
                raise HTTPException(409, "O tipo da linha mudou. Reabre o editor.")
            sid = str((loaders.plan_snapshot_info() or {}).get("snapshot_id") or "")
            if not sid or sid != payload.snapshot_id:
                raise HTTPException(409, "O planeamento mudou. Pesquisa novamente para confirmar a escolha.")
            candidates = plan_review.fetch_keys([payload.plan_key], sid)
            if len(candidates) != 1:
                raise HTTPException(422, "Referência inexistente neste planeamento.")
            chosen = candidates[0]
            index = get_index(template.index_loader)
            if index.snapshot_id != sid:
                index = getattr(loaders, template.index_loader)(snapshot_id=sid)
            if str((loaders.plan_snapshot_info() or {}).get("snapshot_id") or "") != sid:
                raise HTTPException(409, "O planeamento mudou. Pesquisa novamente.")
            data = deepcopy(sheet["sheet_data"])
            row = data["rows"][row_index]
            values = {
                "of": sim.strip_ref_prefix(chosen.get("production_order_no")),
                "ov": sim.strip_ref_prefix(chosen.get("sales_order_no")),
                "cliente": chosen.get("customer_name"), "perfil": chosen.get("profile_type"),
                "modelo": None if full else chosen.get("component_ref"),
            }
            edits = []
            unresolved = row.pop("_identity_unresolved", None)
            if unresolved:
                edits.append((f"rows[{row_index}]._identity_unresolved", unresolved, None, "human", "plan-picker"))
            for field, value in values.items():
                value = str(value).strip() or None if value is not None else None
                # Selecting even an unchanged value confirms the identity as
                # human evidence. Full profiles deliberately have no singular
                # binding, so these events are also their explicit group choice.
                edits.append((f"rows[{row_index}].{field}", row.get(field), value, "human", "plan-picker"))
                row[field] = value
            binding = None if full else {"snapshot_id": sid, "plan_key": payload.plan_key, "selected_explicitly": True,
                                         "identity": plan_bindings.identity_of(chosen)}
            old_binding = row.pop("_plan_binding", None)
            if binding is not None:
                row["_plan_binding"] = binding
            # Binding follows the identity events so evidence replay retains it.
            if binding is not None or old_binding is not None:
                edits.append((f"rows[{row_index}]._plan_binding", old_binding, binding, "human", "plan-picker"))
            if edits and not db.save_sheet_data_with_edits(conn, uid, data, payload.revision, edits):
                raise HTTPException(409, "A folha foi alterada. Confirma novamente.")
            saved = True
            if not run_cross_check(conn, uid, scorer_override=Scorer(index, CrossParams.load())):
                raise HTTPException(409, "A escolha ficou guardada, mas a folha mudou durante a verificação. Reabre o editor.")
            final = db.get_sheet(conn, uid)
            final_row = final["sheet_data"]["rows"][row_index]
            if (sim.strip_ref_prefix(final_row.get("of")) != values["of"]
                    or not plan_review.same_profile(final_row.get("perfil"), values["perfil"])
                    or (not full and final_row.get("modelo") != values["modelo"])):
                raise HTTPException(409, "A escolha ficou guardada, mas a verificação alterou a identidade. Confirma a linha antes de validar.")
            return JSONResponse(jsonable_encoder({"ok": True, "revision": final["revision"], "final_row": final_row,
                                 "redirect_url": sheet_location(uid, payload.back, focus=f"row-{row_index}")}))
        except HTTPException as exc:
            return JSONResponse({"detail": str(exc.detail), "saved": saved}, status_code=exc.status_code)
        except Exception:
            logging.getLogger(__name__).exception("Plan selection failed for sheet %s, row %s", uid, row_index)
            message = ("A escolha ficou guardada, mas não foi possível atualizar a verificação. Reabre a folha."
                       if saved else "Não foi possível guardar a escolha. Tenta novamente.")
            return JSONResponse({"detail": message, "saved": saved}, status_code=503)
        finally:
            conn.close()
