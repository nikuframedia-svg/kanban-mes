"""Audited physical-row decisions, separate from production validation."""
import copy
from fastapi import Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from .. import db
from ..ocr.coverage import confirm_count, EXCLUSION_REASONS


automatic = None


def register(app, connect, view, location, recheck, provider, *, register_automatic=True):
    from .automatic_review import AutomaticReview
    from .. import coverage_recovery
    global automatic
    def process(conn, uid, revision):
        result = coverage_recovery.automatic(conn, uid, revision, provider)
        if result.get('added_rows'):
            recheck(conn, uid)
        return result
    if register_automatic:
        automatic = AutomaticReview(app, connect, coverage_recovery.needs_automatic, process)

    def get_editable(conn, uid, revision):
        sheet = db.get_sheet(conn, uid)
        if not sheet:
            raise HTTPException(404)
        if sheet['status'] not in {'extracted', 'in_review'}:
            raise HTTPException(409, 'A folha não admite alterações neste estado.')
        if sheet['revision'] != revision:
            raise HTTPException(409, 'A folha mudou; recarrega antes de confirmar.')
        return sheet

    def refresh(conn, uid, back):
        try:
            if recheck(conn, uid):
                return None
        except Exception:
            pass
        return RedirectResponse(location(uid, back, erro='A decisão foi guardada, mas não foi possível atualizar a verificação. Verifica novamente a folha.', erro_context='edit'), status_code=303)

    @app.post('/sheet/{uid}/coverage')
    def coverage(request: Request, uid: str, count: str = Form(''),
                 revision: int = Form(...), back: str = Form('')):
        conn = connect()
        try:
            sheet = get_editable(conn, uid, revision)
            data = copy.deepcopy(sheet['sheet_data'])
            old = copy.deepcopy(data.get('_ocr_coverage'))
            try:
                number = int(count)
                confirm_count(data, number, sheet, 'revisor', db.now_iso())
            except ValueError as exc:
                request.state.form_error = str(exc) if count.strip().lstrip('-').isdigit() else 'Indica um número inteiro de linhas.'
                request.state.error_context = 'coverage'
                request.state.coverage_count = count
                response = view(request, uid, back=back)
                response.status_code = 422
                return response
            if not db.save_sheet_data_with_edits(conn, uid, data, revision, [
                    ('coverage.confirmation', old, data['_ocr_coverage'], 'human', 'revisor')]):
                raise HTTPException(409, 'A folha mudou; confirma novamente a contagem.')
        finally:
            conn.close()
        return RedirectResponse(location(uid, back), status_code=303)

    @app.post('/sheet/{uid}/coverage/recalculate')
    def recalculate(uid: str, revision: int = Form(...), back: str = Form('')):
        from ..coverage_recovery import recalculate as calculate
        conn = connect()
        try:
            get_editable(conn, uid, revision)
            try:
                calculate(conn, uid, revision)
            except (ValueError, OSError) as exc:
                return RedirectResponse(location(uid, back, erro=str(exc), erro_context='coverage'), status_code=303)
        finally:
            conn.close()
        return RedirectResponse(location(uid, back), status_code=303)

    @app.post('/sheet/{uid}/coverage/retry')
    def retry(uid: str, revision: int = Form(...), back: str = Form('')):
        conn = connect()
        try:
            sheet = get_editable(conn, uid, revision)
            data = copy.deepcopy(sheet['sheet_data'])
            previous = data.get('_coverage_recovery') or {}
            if previous.get('observations') or previous.get('status') not in {'failed','review'}:
                raise HTTPException(422, 'Não existe uma tentativa localizada por repetir.')
            data.pop('_coverage_recovery', None)
            if not db.save_sheet_data_with_edits(conn, uid, data, revision, [
                ('coverage.retry', previous, None, 'human', 'revisor')]):
                raise HTTPException(409, 'A folha mudou; tenta novamente.')
            revision = db.get_sheet(conn, uid)['revision']
        finally:
            conn.close()
        from . import header_recovery_routes
        header_recovery_routes.automatic.enqueue(uid, revision)
        return RedirectResponse(location(uid, back), status_code=303)

    @app.post('/sheet/{uid}/rows/{row_index}/delete')
    @app.post('/sheet/{uid}/rows/{row_index}/exclude')
    def exclude(uid: str, row_index: int, revision: int = Form(...),
                reason: str = Form(...), duplicate_of: str = Form(''), back: str = Form(''), actor: str = Form('revisor')):
        conn = connect()
        try:
            sheet = get_editable(conn, uid, revision)
            data = copy.deepcopy(sheet['sheet_data'])
            rows = data.get('rows') or []
            if not 0 <= row_index < len(rows) or reason not in EXCLUSION_REASONS:
                raise HTTPException(422, 'Linha ou motivo de exclusão inválido.')
            decision = {'reason': reason}
            if reason == 'duplicate':
                try:
                    target = int(duplicate_of) - 1
                except ValueError:
                    raise HTTPException(422, 'Indica o número original da linha mantida.')
                if (not 0 <= target < len(rows) or target == row_index
                        or rows[target].get('_deleted') is True
                        or not any(v is not None and str(v).strip() for k,v in rows[target].items() if not k.startswith('_'))):
                    raise HTTPException(422, 'A duplicação tem de apontar para uma linha preenchida e incluída.')
                decision['duplicate_of'] = target
            old = copy.deepcopy(rows[row_index])
            rows[row_index].update(_deleted=True, _exclusion=decision)
            if rows[row_index] != old:
                edits = [(f'rows[{row_index}]._deleted', old.get('_deleted'), True, 'human', actor),
                         (f'rows[{row_index}]._exclusion', old.get('_exclusion'), decision, 'human', actor)]
                if not db.save_sheet_data_with_edits(conn, uid, data, revision, edits):
                    raise HTTPException(409, 'A folha mudou; a exclusão não foi guardada.')
                error = refresh(conn, uid, back)
                if error is not None:
                    return error
        finally:
            conn.close()
        return RedirectResponse(location(uid, back), status_code=303)

    @app.post('/sheet/{uid}/rows/{row_index}/restore')
    def restore(uid: str, row_index: int, revision: int = Form(...), back: str = Form('')):
        conn = connect()
        try:
            sheet = get_editable(conn, uid, revision)
            data = copy.deepcopy(sheet['sheet_data'])
            rows = data.get('rows') or []
            if not 0 <= row_index < len(rows) or rows[row_index].get('_deleted') is not True:
                raise HTTPException(422, 'A linha não está excluída.')
            old = rows[row_index].pop('_exclusion', None)
            rows[row_index]['_deleted'] = False
            edits = [(f'rows[{row_index}]._deleted', True, False, 'human', 'revisor'),
                     (f'rows[{row_index}]._exclusion', old, None, 'human', 'revisor')]
            raw_rows = (sheet.get('raw_extraction') or {}).get('rows') or []
            human_of = [e for e in db.evidence_edits(conn, sheet) if e['field_path'] == f'rows[{row_index}].of']
            confirmed_of = bool(human_of and str(human_of[-1].get('new_value') or '').strip())
            if (row_index < len(raw_rows) and not raw_rows[row_index].get('of') and not confirmed_of
                    and (row_index == 0 or not rows[row_index].get('of'))):
                if rows[row_index].get('of'):
                    edits.append((f'rows[{row_index}].of', rows[row_index]['of'], None, 'system', 'recovery:restore'))
                    rows[row_index]['of'] = None
                rows[row_index]['_identity_unresolved'] = 'OF da linha restaurada por confirmar.'
                edits.append((f'rows[{row_index}]._identity_unresolved', None,
                              rows[row_index]['_identity_unresolved'], 'system', 'recovery:restore'))
            if not db.save_sheet_data_with_edits(conn, uid, data, revision, edits):
                raise HTTPException(409, 'A folha mudou; o restauro não foi guardado.')
            error = refresh(conn, uid, back)
            if error is not None:
                return error
        finally:
            conn.close()
        return RedirectResponse(location(uid, back), status_code=303)

    @app.get('/sheet/{uid}/rows/{row_index}/audit')
    def audit(uid: str, row_index: int):
        conn = connect()
        try:
            sheet = db.get_sheet(conn, uid)
            if not sheet or not 0 <= row_index < len((sheet.get('sheet_data') or {}).get('rows', [])):
                raise HTTPException(404)
            path = f'rows[{row_index}]'
            events = [dict(e) for e in conn.execute(
                'SELECT * FROM edits WHERE sheet_uid=? AND (field_path=? OR field_path LIKE ?) ORDER BY id',
                (uid, path, path + '.%'))]
            return {'uid': uid, 'row_index': row_index, 'revision': sheet['revision'], 'events': events}
        finally:
            conn.close()
