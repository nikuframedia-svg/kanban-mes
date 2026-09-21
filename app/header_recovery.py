"""Header-only recovery: append observations, never replace the original OCR."""
from __future__ import annotations
import copy
import hashlib
from pathlib import Path
from . import db, imaging
from .matching import header_cross
from .ocr.provider import _crop_header_band, _clean_header_fields
from .templates_spec import get_template

VERSION = 1
FIELDS = ('operador', 'n_operador', 'setor_maquina', 'data', 'turno')


def identity(sheet):
    return {'image_sha256': sheet.get('image_sha256'),
            'image_rotation': int(sheet.get('image_rotation') or 0),
            'generation': int(sheet.get('extraction_generation') or 0)}


def current_recovery(sheet):
    recovery = (sheet.get('sheet_data') or {}).get('_header_recovery') or {}
    return recovery if recovery.get('context') == identity(sheet) else {}


def date_needs_confirmation(sheet, protected=()):
    recovery = current_recovery(sheet)
    conflict = recovery.get('date_conflict')
    if not conflict or 'data' in protected:
        return False
    confirmed = recovery.get('date_confirmation') or {}
    return not (confirmed.get('written') == conflict['written']
                and confirmed.get('assumed') == conflict['assumed'])


def evidence_observations(sheet):
    # Only accepted, independently verified readings become matching evidence.
    # The handwritten date is evidence even while the existing date rule applies.
    recovery = current_recovery(sheet)
    values = dict(recovery.get('accepted') or {})
    written = (recovery.get('observations') or {}).get('data')
    if written:
        values['data'] = written
    return {key: value for key, value in values.items() if key in FIELDS}


def prepare(sheet, protected, provider, employees, machines, assumed_date):
    if sheet['status'] not in {'extracted', 'in_review'}:
        raise ValueError('A folha não admite recuperação neste estado.')
    if not sheet.get('image_path') or not Path(sheet['image_path']).is_file():
        raise ValueError('Imagem original indisponível.')
    data = copy.deepcopy(sheet['sheet_data'])
    header = data.setdefault('header', {})
    image = imaging.render_oriented(Path(sheet['image_path']), sheet.get('image_rotation') or 0)
    image_hash = hashlib.sha256(image.read_bytes()).hexdigest()
    previous = current_recovery(sheet)
    if (previous.get('version') == VERSION and previous.get('image_sha256') == image_hash
            and previous.get('status') not in {'failed', None}):
        return None
    recovery = {'version': VERSION, 'source': 'automatic_header_recovery',
                'context': identity(sheet), 'image_sha256': image_hash,
                'image_path': str(sheet['image_path']), 'crop': {'area': 'top', 'height_fraction': .30},
                'attempted_at': db.now_iso(), 'provider': getattr(provider, 'name', 'unknown'),
                'observations': {}, 'accepted': {}, 'proposals': {}, 'status': 'failed'}
    band = None
    try:
        band = _crop_header_band(image)
        observations = _clean_header_fields(provider.extract_header(band, get_template(sheet['template_name'])))
    except Exception as exc:
        # Persist a visible error without resetting the sheet or losing any rows.
        recovery['error'] = f'{type(exc).__name__}: {str(exc)[:300]}'
    else:
        recovery['observations'] = observations
        check = header_cross.check_header(observations, get_template(sheet['template_name']),
            employees=employees, machines=machines, assumed_date=assumed_date)
        for field in FIELDS:
            observed = observations.get(field)
            if str(header.get(field) or '').strip() or field in protected:
                continue
            cell = check['cells'][field]
            if field in {'operador', 'n_operador'}:
                safe = check['operator'].get('accepted') is True
            elif field == 'setor_maquina':
                safe = cell['status'] in {'confirmed', 'corrected'}
            elif field == 'turno':
                safe = cell['status'] in {'confirmed', 'corrected'} and bool(observed)
            else:
                safe = bool(assumed_date)
            value = assumed_date if field == 'data' else (cell.get('proposal') or observed)
            if safe and value:
                header[field] = value
                if field != 'data':
                    recovery['accepted'][field] = observed
            elif observed:
                recovery['proposals'][field] = observed
        written_date = header_cross.canonical_date(observations.get('data'))
        if written_date and assumed_date and written_date != assumed_date:
            recovery['date_conflict'] = {'written': written_date, 'assumed': assumed_date}
        missing = [f for f in FIELDS if not str(header.get(f) or '').strip()]
        recovery['missing'] = missing
        recovery['status'] = 'review' if (recovery['proposals'] or recovery.get('date_conflict')) else ('partial' if missing else 'recovered')
    finally:
        if band is not None:
            band.unlink(missing_ok=True)
    data['_header_recovery'] = recovery
    return data


def recover(conn, uid, revision, provider, employees, machines, assumed_date):
    from .recovery_lock import single_worker
    with single_worker(conn):
        return _recover(conn, uid, revision, provider, employees, machines, assumed_date)


def _recover(conn, uid, revision, provider, employees, machines, assumed_date):
    sheet = db.get_sheet(conn, uid)
    if not sheet or sheet['revision'] != revision:
        raise ValueError('A folha mudou; recarrega antes de recuperar.')
    protected = db.human_header_fields(conn, uid)
    data = prepare(sheet, protected, provider, employees, machines, assumed_date)
    if data is None:
        return {'uid': uid, 'status': 'already_recovered'}
    old = sheet['sheet_data']
    edits = [('header_recovery.attempt', old.get('_header_recovery'), data['_header_recovery'],
              'system', 'ocr:header-recovery')]
    for field in FIELDS:
        before, after = (old.get('header') or {}).get(field), data['header'].get(field)
        if before != after:
            edits.append((f'header.{field}', before, after, 'system', 'ocr:header-recovery'))
    # Header recovery must not materialize any production/plan substitutions.
    # Carry forward the existing row cross unchanged and update only header checks.
    cross = copy.deepcopy(sheet.get('cross_check'))
    if cross:
        checked = header_cross.check_header(data['header'], get_template(sheet['template_name']),
            human_fields=protected, employees=employees, machines=machines, assumed_date=assumed_date)
        cross['header'] = {'cells': checked['cells'], 'source_document': (cross.get('header') or {}).get('source_document', {})}
        cross['operator'] = checked['operator']
        cross['data_revision'] = cross['materialized_revision'] = revision+1
    if not db.save_sheet_data_with_edits(conn, uid, data, revision, edits,
            cross_check=cross, write_cross=True):
        raise ValueError('A folha mudou durante o OCR; as correções atuais foram preservadas.')
    return {'uid': uid, 'sheet_no': sheet.get('sheet_no'), 'status': data['_header_recovery']['status'],
            'missing': data['_header_recovery'].get('missing', [])}


def confirm_rule(conn, uid, revision):
    sheet = db.get_sheet(conn, uid)
    if not sheet or sheet['revision'] != revision or sheet['status'] not in {'extracted','in_review'}:
        raise ValueError('A folha mudou ou já não admite confirmação.')
    recovery = current_recovery(sheet)
    conflict = recovery.get('date_conflict')
    if not conflict:
        raise ValueError('Não existe divergência de data para confirmar.')
    data = copy.deepcopy(sheet['sheet_data'])
    old_date = data['header'].get('data')
    data['header']['data'] = conflict['assumed']
    confirmation = {**conflict, 'at': db.now_iso(), 'actor': 'revisor'}
    data['_header_recovery']['date_confirmation'] = confirmation
    if not db.save_sheet_data_with_edits(conn, uid, data, revision, [
            ('header_recovery.date_confirmation', recovery.get('date_confirmation'), confirmation, 'human', 'revisor'),
            ('header.data', old_date, conflict['assumed'], 'human', 'revisor')]):
        raise ValueError('A folha mudou; confirma novamente a data.')
