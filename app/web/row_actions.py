"""Atomic human row decisions. Array indices remain permanent audit identities."""
from copy import deepcopy
import hashlib
import json
import logging
from typing import Literal

from fastapi import Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from .. import db
from ..ocr.coverage import EXCLUSION_REASONS
from ..review_guard import revision_guard, ReviewConflict
from ..templates_spec import get_template, is_marked
from ..matching import loaders, similarity as sim
from . import plan_review


class NewRow(BaseModel):
    revision: int = Field(ge=0)
    request_id: str = Field(min_length=8, max_length=100)
    values: dict[str, str | None]
    position: Literal['start', 'end', 'before', 'after'] = 'end'
    anchor: int | None = Field(default=None, ge=0)
    snapshot_id: str | None = None
    plan_key: str | None = None
    back: str = ''


def editable(conn, uid, revision, unsupported):
    sheet = db.get_sheet(conn, uid)
    if not sheet:
        raise HTTPException(404, 'Folha inexistente.')
    if sheet['status'] not in {'extracted', 'in_review'} or unsupported(sheet):
        raise HTTPException(409, 'A folha não admite alterações neste estado.')
    if sheet['revision'] != revision:
        raise HTTPException(409, 'A folha foi alterada noutra aba. As tuas alterações por guardar foram mantidas; atualiza a folha antes de continuar.')
    return sheet


def finish(conn, uid, revision, recheck, **result):
    """Never retry a saved mutation when only its independent cross failed."""
    warning = None
    try:
        with revision_guard(uid, revision) as guard:
            if not recheck(conn, uid):
                raise ReviewConflict('A folha mudou durante a verificação.')
            revision = guard['revision']
    except ReviewConflict:
        revision = guard['revision']
        warning = 'A alteração foi guardada, mas a folha mudou entretanto. Atualiza a folha antes de continuar.'
    except Exception:
        logging.getLogger(__name__).exception('Row review failed for sheet %s', uid)
        warning = 'A alteração foi guardada. A verificação do plano não terminou.'
        # Own commits only: do not adopt another tab's revision after failure.
        revision = guard['revision']
    current = db.get_sheet(conn, uid)
    return {'ok': True, 'saved': True, 'revision': revision, 'warning': warning,
            'conflict': current['revision'] != revision, **result}


def register(app, connect, recheck, location, unsupported=lambda sheet: False):
    @app.post('/sheet/{uid}/add-row')
    def add_row(uid: str, payload: NewRow):
        conn = connect()
        try:
            prior = db.get_sheet(conn, uid)
            fingerprint = hashlib.sha256(json.dumps(payload.model_dump(exclude={'revision', 'back'}), sort_keys=True).encode()).hexdigest()
            # A lost response/repeated click cannot append the same row twice.
            for i, row in enumerate(((prior or {}).get('sheet_data') or {}).get('rows', [])):
                entry = row.get('_manual_entry') or {}
                if entry.get('request_id') == payload.request_id:
                    if entry.get('fingerprint') != fingerprint:
                        raise HTTPException(409, 'Este registo já foi guardado com outros valores.')
                    return {'ok': True, 'saved': True, 'replayed': True, 'revision': entry['saved_revision'],
                            'row_index': i, 'conflict': prior['revision'] != entry['saved_revision']}
            sheet = editable(conn, uid, payload.revision, unsupported)
            data = deepcopy(sheet['sheet_data'])
            rows = data.setdefault('rows', [])
            if len(rows) >= 200:
                raise HTTPException(422, 'A folha atingiu o limite de 200 linhas.')
            template = get_template(sheet['template_name'])
            fields = template.display_fields(data) if plan_review.IS_MTG2 else template.row_fields
            if set(payload.values) - set(fields):
                raise HTTPException(422, 'Campo de linha inválido.')
            values = {f: (payload.values.get(f) or '').strip() or None for f in fields}
            if not any(values.values()) or any(len(v or '') > 500 for v in values.values()):
                raise HTTPException(422, 'Preenche a linha antes de guardar (máximo de 500 caracteres por campo).')
            binding = None
            if payload.plan_key:
                if not template.index_loader:
                    raise HTTPException(422, 'Esta folha não usa referências do planeamento.')
                sid = str((loaders.plan_snapshot_info() or {}).get('snapshot_id') or '')
                if not sid or sid != payload.snapshot_id:
                    raise HTTPException(409, 'O plano mudou. Pesquisa novamente antes de guardar.')
                candidates = plan_review.fetch_keys([payload.plan_key], sid)
                if len(candidates) != 1:
                    raise HTTPException(422, 'Referência indisponível no plano selecionado.')
                chosen = candidates[0]
                full = is_marked(values.get('perf_comp'))
                values.update(of=sim.strip_ref_prefix(chosen.get('production_order_no')),
                              ov=sim.strip_ref_prefix(chosen.get('sales_order_no')),
                              cliente=chosen.get('customer_name'), perfil=chosen.get('profile_type'),
                              modelo=None if full else chosen.get('component_ref'))
                if not full:
                    binding = {'snapshot_id': sid, 'plan_key': payload.plan_key, 'selected_explicitly': True}
            order = sorted(range(len(rows)), key=lambda i: rows[i].get('_display_order', rows[i].get('_paper_position', i + 1)))
            if payload.position in {'before', 'after'}:
                if payload.anchor not in order or rows[payload.anchor].get('_deleted'):
                    raise HTTPException(409, 'A linha de referência mudou. Escolhe novamente a posição.')
                slot = order.index(payload.anchor) + int(payload.position == 'after')
            else:
                slot = 0 if payload.position == 'start' else len(order)
            index = len(rows)
            rows.append(values)
            order.insert(slot, index)
            edits = [(f'rows[{index}].{field}', None, value, 'human', 'manual-row') for field, value in values.items()]
            if binding:
                values['_plan_binding'] = binding
                edits.append((f'rows[{index}]._plan_binding', None, binding, 'human', 'manual-row'))
            values['_manual_entry'] = {'request_id': payload.request_id, 'fingerprint': fingerprint,
                                       'at': db.now_iso(), 'initial_revision': payload.revision,
                                       'saved_revision': payload.revision + 1}
            # Explicit blanks on manual rows are decisions, not ditto marks.
            for rank, i in enumerate(order, 1):
                edits.append((f'rows[{i}]._display_order', rows[i].get('_display_order'), rank, 'human', 'manual-row'))
                rows[i]['_display_order'] = rank
            edits.append((f'rows[{index}]._creation', None, deepcopy(values), 'human', 'manual-row'))
            if not db.save_sheet_data_with_edits(conn, uid, data, payload.revision, edits):
                raise HTTPException(409, 'A folha mudou; a linha não foi guardada.')
            return finish(conn, uid, payload.revision + 1, recheck, row_index=index)
        except HTTPException:
            raise
        except Exception:
            logging.getLogger(__name__).exception('Row creation failed for sheet %s', uid)
            raise HTTPException(503, 'Não foi possível concluir o registo. Os campos preenchidos foram mantidos; tenta novamente.')
        finally:
            conn.close()

    def respond(request, uid, back, result):
        if 'application/json' in request.headers.get('accept', ''):
            return JSONResponse(result)
        return RedirectResponse(location(uid, back), status_code=303)

    @app.post('/sheet/{uid}/rows/{row_index}/delete')
    @app.post('/sheet/{uid}/rows/{row_index}/exclude')
    def exclude(request: Request, uid: str, row_index: int, revision: int = Form(...),
                reason: str = Form(''), duplicate_of: str = Form(''), back: str = Form(''), actor: str = Form('revisor')):
        conn = connect()
        try:
            sheet = editable(conn, uid, revision, unsupported)
            data = deepcopy(sheet['sheet_data'])
            rows = data.get('rows') or []
            if not 0 <= row_index < len(rows) or (reason and reason not in EXCLUSION_REASONS):
                raise HTTPException(422, 'Linha ou motivo de exclusão inválido.')
            if rows[row_index].get('_deleted') is True:
                return respond(request, uid, back, {'ok': True, 'saved': True, 'revision': revision, 'row_index': row_index})
            decision = {'action': 'remove', 'at': db.now_iso(), 'revision': revision}
            if reason:
                decision['reason'] = reason
            if reason == 'duplicate':
                try:
                    target = int(duplicate_of) - 1
                except ValueError:
                    raise HTTPException(422, 'Linha mantida inválida.')
                if not 0 <= target < len(rows) or target == row_index or rows[target].get('_deleted') or not any(v is not None and str(v).strip() for k, v in rows[target].items() if not k.startswith('_')):
                    raise HTTPException(422, 'Linha mantida inválida.')
                decision['duplicate_of'] = target
            old = deepcopy(rows[row_index])
            rows[row_index].update(_deleted=True, _exclusion=decision)
            edits = [(f'rows[{row_index}]._removal', old, decision, 'human', actor),
                     (f'rows[{row_index}]._deleted', old.get('_deleted'), True, 'human', actor),
                     (f'rows[{row_index}]._exclusion', old.get('_exclusion'), decision, 'human', actor)]
            if not db.save_sheet_data_with_edits(conn, uid, data, revision, edits):
                raise HTTPException(409, 'A folha mudou; a remoção não foi guardada.')
            return respond(request, uid, back, finish(conn, uid, revision + 1, recheck, row_index=row_index))
        finally:
            conn.close()

    @app.post('/sheet/{uid}/rows/{row_index}/restore')
    def restore(request: Request, uid: str, row_index: int, revision: int = Form(...), back: str = Form('')):
        conn = connect()
        try:
            sheet = editable(conn, uid, revision, unsupported)
            data = deepcopy(sheet['sheet_data'])
            rows = data.get('rows') or []
            if not 0 <= row_index < len(rows):
                raise HTTPException(422, 'Linha inexistente.')
            if rows[row_index].get('_deleted') is not True:
                return respond(request, uid, back, {'ok': True, 'saved': True, 'revision': revision, 'row_index': row_index})
            before = deepcopy(rows[row_index])
            old = rows[row_index].pop('_exclusion', None)
            rows[row_index]['_deleted'] = False
            edits = [(f'rows[{row_index}]._restoration', before, deepcopy(rows[row_index]), 'human', 'revisor'),
                     (f'rows[{row_index}]._deleted', True, False, 'human', 'revisor'),
                     (f'rows[{row_index}]._exclusion', old, None, 'human', 'revisor')]
            # Legacy restoration must not invent the missing OF of sheet 681.
            # Undoing a new explicit removal keeps the pre-existing identity.
            if not plan_review.IS_MTG2 and (old or {}).get('action') != 'remove':
                raw_rows = (sheet.get('raw_extraction') or {}).get('rows') or []
                human_of = [e for e in db.evidence_edits(conn, sheet) if e['field_path'] == f'rows[{row_index}].of']
                confirmed_of = bool(human_of and str(human_of[-1].get('new_value') or '').strip())
                if row_index < len(raw_rows) and not raw_rows[row_index].get('of') and not confirmed_of and (row_index == 0 or not rows[row_index].get('of')):
                    edits.append((f'rows[{row_index}].of', rows[row_index].get('of'), None, 'system', 'recovery:restore'))
                    rows[row_index]['of'] = None
                    message = 'OF da linha restaurada por confirmar.'
                    edits.append((f'rows[{row_index}]._identity_unresolved', rows[row_index].get('_identity_unresolved'), message, 'system', 'recovery:restore'))
                    rows[row_index]['_identity_unresolved'] = message
            if not db.save_sheet_data_with_edits(conn, uid, data, revision, edits):
                raise HTTPException(409, 'A folha mudou; o restauro não foi guardado.')
            return respond(request, uid, back, finish(conn, uid, revision + 1, recheck, row_index=row_index))
        finally:
            conn.close()
