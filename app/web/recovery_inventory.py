"""Read-only inventory for resumable maintenance through the existing app."""
from fastapi import HTTPException
from .. import db, historical_quantities
from ..ocr.coverage import coverage_view


def register(app, connect, automatic):
    @app.get('/recovery/inventory')
    def inventory():
        conn = connect()
        try:
            result = []
            for item in db.list_sheets(conn, status='pending'):
                sheet = db.get_sheet(conn, item['uid'])
                if sheet['status'] not in {'extracted', 'in_review'}:
                    continue
                data = sheet.get('sheet_data') or {}
                coverage = coverage_view(data, sheet)
                excluded = [{'row_index': i, 'reason': row.get('_exclusion'),
                             'of': row.get('of'), 'perfil': row.get('perfil'), 'modelo': row.get('modelo')}
                            for i, row in enumerate(data.get('rows', [])) if row.get('_deleted') is True]
                result.append({'uid': sheet['uid'], 'sheet_no': sheet['sheet_no'],
                    'revision': sheet['revision'], 'status': sheet['status'],
                    'extraction_generation': sheet.get('extraction_generation'),
                    'image_sha256': sheet.get('image_sha256'),
                    'missing_header': [k for k in ('operador','n_operador','setor_maquina','data')
                                       if not (data.get('header') or {}).get(k)],
                    'human_header': sorted(db.human_header_fields(conn, sheet['uid'])),
                    'excluded': excluded, 'coverage': coverage,
                    'historical_refresh': historical_quantities.needs_refresh(sheet),
                    'automatic_needed': automatic.needed(conn, sheet)})
            return {'sheets': result}
        finally:
            conn.close()

    @app.get('/sheet/{uid}/recovery-state')
    def state(uid: str):
        conn = connect()
        try:
            sheet = db.get_sheet(conn, uid)
            if not sheet:
                raise HTTPException(404)
            return {key: sheet.get(key) for key in ('uid','sheet_no','status','revision',
                'image_sha256','image_rotation','extraction_generation','sheet_data','raw_extraction','cross_check')}
        finally:
            conn.close()
