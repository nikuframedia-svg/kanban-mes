"""Recalculate coverage without rereading OCR or modifying production rows."""
from __future__ import annotations
import copy
from . import db, imaging, image_storage
from .ocr.coverage import check_coverage, sheet_identity, row_accounting, ALGORITHM_VERSION


def recalculate(conn, uid: str, revision: int) -> dict:
    sheet = db.get_sheet(conn, uid)
    if not sheet or sheet['status'] not in {'extracted', 'in_review'}:
        raise ValueError('Esta folha não admite recalcular a conferência neste estado.')
    if sheet['revision'] != revision:
        raise ValueError('A folha mudou; recarrega antes de recalcular.')
    if image_storage.for_processing(sheet) is None:
        raise ValueError('Imagem original indisponível.')
    data = copy.deepcopy(sheet['sheet_data'])
    old = data.get('_ocr_coverage')
    if (old and old.get('algorithm_version') == ALGORITHM_VERSION
            and old.get('context') == sheet_identity(sheet)):
        return {'uid': uid, 'status': 'already_current'}
    image = imaging.render_oriented(image_storage.for_processing(sheet), sheet.get('image_rotation') or 0)
    coverage = check_coverage(image, data)
    coverage['context'] = sheet_identity(sheet)
    coverage['recalculated_at'] = db.now_iso()
    data['_ocr_coverage'] = coverage
    if not db.save_sheet_data_with_edits(conn, uid, data, revision, [
            ('coverage.recalculation', old, coverage, 'system', 'recovery:coverage-v2')]):
        raise ValueError('A folha mudou durante o cálculo; os dados atuais foram preservados.')
    return {'uid': uid, 'status': coverage['status'], 'expected_rows': coverage['expected_rows']}


def needs_automatic(conn, sheet):
    from .templates_spec import get_template
    from .ocr.coverage import coverage_resolved
    if (sheet['status'] not in {'extracted', 'in_review'} or not sheet.get('image_path')
            or image_storage.for_processing(sheet) is None
            or sheet['template_name'] != 'cantoneiras_kanban'):
        return False
    data = sheet.get('sheet_data') or {}
    coverage = data.get('_ocr_coverage') or {}
    if coverage.get('algorithm_version') != ALGORITHM_VERSION or coverage.get('context') != sheet_identity(sheet):
        return True
    if coverage_resolved(data, sheet) or any(row.get('_deleted') is True for row in data.get('rows', [])):
        return False
    expected = coverage.get('expected_rows')
    previous = data.get('_coverage_recovery') or {}
    return (type(expected) is int and expected > row_accounting(data)['accounted_rows']
            and (previous.get('context') != sheet_identity(sheet) or previous.get('algorithm_version') != ALGORITHM_VERSION))


def automatic(conn, uid, revision, provider_factory):
    """Recover only a missing strip, bracketed by unambiguous existing rows."""
    from .ocr.coverage import coverage_resolved, table_rows
    from .templates_spec import get_template
    from .row_recovery import recover_missing
    recalculate(conn, uid, revision)
    sheet = db.get_sheet(conn, uid)
    data = copy.deepcopy(sheet['sheet_data'])
    expected = data['_ocr_coverage'].get('expected_rows')
    if (coverage_resolved(data, sheet) or type(expected) is not int
            or expected <= row_accounting(data)['accounted_rows']
            or any(row.get('_deleted') is True for row in data.get('rows', []))):
        return {'status': 'checked'}
    context = sheet_identity(sheet)
    if ((data.get('_coverage_recovery') or {}).get('context') == context
            and (data.get('_coverage_recovery') or {}).get('algorithm_version') == ALGORITHM_VERSION):
        return {'status': 'already_attempted'}
    attempt = {'context': context, 'at': db.now_iso(), 'status': 'review', 'observations': {},
               'source': 'targeted_ocr', 'algorithm_version': ALGORITHM_VERSION}
    try:
        image = imaging.render_oriented(image_storage.for_processing(sheet), sheet.get('image_rotation') or 0)
        detected = table_rows(image)
        provider = provider_factory()
        attempt['provider'] = type(provider).__name__
        anchor_observations = {}
        observations, positions = recover_missing(provider, image,
            get_template(sheet['template_name']), sheet, detected, anchor_observations)
        for i, position in positions.items():
            data['rows'][i]['_paper_position'] = position
        for index, row in observations.items():
            assert int(index) == len(data['rows'])
            data['rows'].append(row)
        attempt['observations'] = observations
        attempt['anchor_observations'] = anchor_observations
        attempt['regions'] = [{**detected['rows'][row['_paper_position']-1],
                               'paper_position': row['_paper_position']} for row in observations.values()]
        attempt['status'] = 'recovered'
        data['_ocr_coverage'].update(status='complete', extracted_rows=len(data['rows']))
    except Exception as exc:
        attempt['error'] = str(exc)[:300]
        if not isinstance(exc, ValueError):
            attempt['status'] = 'failed'
    old = data.get('_coverage_recovery')
    data['_coverage_recovery'] = attempt
    if not db.save_sheet_data_with_edits(conn, uid, data, sheet['revision'], [
            ('coverage.recovery', old, attempt, 'system', 'ocr:missing-rows')]):
        raise ValueError('A folha mudou durante a leitura; as alterações atuais foram preservadas.')
    return {'status': attempt['status'], 'added_rows': len(attempt['observations'])}
