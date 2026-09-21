"""Audited physical-row decisions, separate from production validation."""
import copy
from fastapi import Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from .. import db
from ..ocr.coverage import confirm_count


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
