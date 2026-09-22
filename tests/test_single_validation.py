"""Production validation is independent of optional OCR accounting."""
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import threading
import time

import pytest

from app import db, review_guard
from app.ocr.coverage import ALGORITHM_VERSION, coverage_resolved, sheet_identity
from app.web import main, plan_review, review_writes
from tests.test_web import client, make_index


def current(uid):
    with db.connect() as conn:
        return db.get_sheet(conn, uid)


def create(count=5, removed=True, coverage=None):
    entry = make_index().entries[0]
    row = {key: str(entry.get(key) or '') for key in ('of', 'ov', 'cliente', 'modelo', 'perfil')}
    row['qtd'] = '14'
    template = 'serrote_kanban' if plan_review.IS_MTG2 else 'cantoneiras_kanban'
    with db.connect() as conn:
        uid = db.create_sheet(conn, template, 'unavailable-scan.png', 'scan')
        data = {'header': {'operador': 'ANA', 'data': '21/09/2026'},
                'rows': [deepcopy(row) for _ in range(count)], 'footer': {}}
        if removed:
            data['rows'].append(deepcopy(row))
        if coverage is not None:
            data['_ocr_coverage'] = deepcopy(coverage)
        data['_coverage_recovery'] = {'status': 'failed', 'error': 'OCR indisponível'}
        db.set_extraction(conn, uid, data)
        if removed or coverage:
            sheet = db.get_sheet(conn, uid)
            data = sheet['sheet_data']
            if coverage:
                data['_ocr_coverage'] = deepcopy(coverage)
            edits = []
            if removed:
                data['rows'][-1]['_deleted'] = True
                edits.append((f'rows[{count}]._deleted', None, True, 'human', 'test'))
            db.save_sheet_data_with_edits(conn, uid, data, sheet['revision'], edits)
        return uid


@pytest.mark.parametrize('engine', ['legacy', 'v3'])
@pytest.mark.parametrize('coverage', [None, {}, {'expected_rows': None},
    {'expected_rows': 99, 'algorithm_version': ALGORITHM_VERSION},
    {'expected_rows': 5, 'algorithm_version': 0},
    {'expected_rows': 5, 'algorithm_version': ALGORITHM_VERSION, 'context': {'extraction_generation': -1}}])
def test_five_correct_rows_and_removed_row_validate_without_count(client, monkeypatch, engine, coverage):
    monkeypatch.setattr(main, 'settings', replace(main.settings, cross_engine=engine))
    uid = create(coverage=coverage)
    before = current(uid)
    assert not coverage_resolved(before['sheet_data'], before)
    result = client.post(f'/sheet/{uid}/validate', data={'revision': before['revision']})
    assert result.status_code == 303 and 'erro=' not in result.headers['location'], result.headers
    after = current(uid)
    assert after['status'] == 'validated'
    assert after['raw_extraction'] == before['raw_extraction']
    assert after['sheet_data'].get('_ocr_coverage') == before['sheet_data'].get('_ocr_coverage')
    assert after['sheet_data']['rows'][-1]['_deleted']
    assert len(client.stored_calls) == 1
    client.post(f'/sheet/{uid}/validate', data={'revision': after['revision']})
    assert len(client.stored_calls) == 1


def test_nine_rows_unknown_count_and_real_missing_reference(client, monkeypatch):
    uid = create(count=9, removed=False)
    with db.connect() as conn:
        sheet = db.get_sheet(conn, uid)
        data = sheet['sheet_data']
        changes = dict(of='999999', ov='999999', cliente='UNKNOWN', modelo='UNKNOWN', perfil='UNKNOWN')
        edits = [(f'rows[8].{key}', data['rows'][8].get(key), value, 'human', 'test') for key, value in changes.items()]
        data['rows'][8].update(changes)
        db.save_sheet_data_with_edits(conn, uid, data, sheet['revision'], edits)
    page = client.get(f'/sheet/{uid}').text
    assert 'Corrigir contagem' not in page and 'name="count"' not in page
    result = client.post(f'/sheet/{uid}/validate', data={'revision': current(uid)['revision']})
    from urllib.parse import unquote_plus
    message = unquote_plus(result.headers['location'])
    assert 'Linha 9' in message and 'contagem' not in message, message
    assert current(uid)['status'] != 'validated' and not client.stored_calls


@pytest.mark.parametrize('real_change', [False, True])
def test_stale_revision_accepts_only_diagnostic_changes(client, real_change):
    uid = create()
    before = current(uid)
    with db.connect() as conn:
        data = deepcopy(before['sheet_data'])
        data['_ocr_coverage'] = {'expected_rows': None, 'status': 'unverified'}
        if real_change:
            data['rows'][0]['qtd'] = '777'
        db.save_sheet_data_with_edits(conn, uid, data, before['revision'], [])
    result = client.post(f'/sheet/{uid}/validate', data={
        'revision': before['revision'], 'review_token': review_writes.token(before)})
    assert ('erro=' in result.headers['location']) == real_change
    assert bool(client.stored_calls) != real_change


def test_async_edits_keep_revision_drafts_and_original(client, monkeypatch):
    uid = create()
    before = current(uid)
    headers = {'Accept': 'application/json'}
    with db.connect() as conn:
        data = deepcopy(before['sheet_data'])
        data['_ocr_coverage'] = {'expected_rows': None}
        db.save_sheet_data_with_edits(conn, uid, data, before['revision'], [])
    result = client.post(f'/sheet/{uid}/edit', headers=headers, data={
        'field_path': 'rows[0].qtd', 'value': '8', 'revision': before['revision'],
        'review_token': review_writes.token(before)})
    assert result.status_code == 200, result.text
    saved = result.json()
    assert saved['ok'] and saved['html'] and saved['review_token']
    assert current(uid)['sheet_data']['rows'][0]['qtd'] == '8'
    assert current(uid)['raw_extraction'] == before['raw_extraction']
    # A real concurrent edit is never silently overwritten on retry.
    stale = client.post(f'/sheet/{uid}/edit', headers=headers, data={
        'field_path': 'rows[0].qtd', 'value': '9', 'revision': before['revision'],
        'review_token': review_writes.token(before)})
    assert stale.status_code == 409 and not stale.json()['ok']
    assert 'revision' not in stale.json()
    assert current(uid)['sheet_data']['rows'][0]['qtd'] == '8'
    result = client.post(f'/sheet/{uid}/header', headers=headers, data={
        'operador': 'ATUALIZADO', 'data': '21/09/2026', 'revision': saved['revision'],
        'review_token': saved['review_token']})
    assert result.status_code == 200 and result.json()['ok'], result.text
    assert current(uid)['sheet_data']['header']['operador'] == 'ATUALIZADO'
    assert not client.stored_calls


def test_cancelled_automatic_write_cannot_modify_validated_sheet(client, monkeypatch):
    uid = create()
    service = main.coverage_routes.automatic if plan_review.IS_MTG2 else main.header_recovery_routes.automatic
    started, release = threading.Event(), threading.Event()
    def work(conn, target, revision):
        sheet = db.get_sheet(conn, target)
        started.set()
        assert release.wait(10)
        data = deepcopy(sheet['sheet_data'])
        data['rows'].append({'of': '999999', 'qtd': '999'})
        db.save_sheet_data_with_edits(conn, target, data, revision, [])
    monkeypatch.setattr(service, 'eligible', lambda *_: True)
    monkeypatch.setattr(service, 'process', work)
    before = current(uid)
    service.enqueue(uid, before['revision'])
    try:
        assert started.wait(5)
        result = client.post(f'/sheet/{uid}/validate', data={'revision': before['revision']})
        assert 'erro=' not in result.headers['location'], result.headers
        frozen = current(uid)
        assert frozen['status'] == 'validated'
    finally:
        release.set()
    for _ in range(200):
        if service.jobs[uid]['status'] not in {'queued', 'running'}:
            break
        time.sleep(.01)
    assert current(uid) == frozen


def test_required_header_and_storage_failure_stay_editable(client, monkeypatch):
    uid = create()
    with db.connect() as conn:
        sheet = db.get_sheet(conn, uid)
        data = sheet['sheet_data']; data['header']['operador'] = ''
        db.save_sheet_data_with_edits(conn, uid, data, sheet['revision'], [])
    result = client.post(f'/sheet/{uid}/validate', data={'revision': current(uid)['revision']})
    assert 'operador' in result.headers['location'] and not client.stored_calls
    assert current(uid)['status'] != 'validated'
    def offline(*args, **kwargs):
        raise RuntimeError('storage unavailable')
    monkeypatch.setattr(main.pg_store, 'store_validated_sheet', offline)
    result = client.post(f'/sheet/{uid}/validate', data={'revision': current(uid)['revision'], 'header_operador': 'ANA'})
    assert 'erro=' in result.headers['location'] and current(uid)['status'] != 'validated'


def test_browser_single_validation(client, monkeypatch, tmp_path):
    playwright = os.environ.get('PLAYWRIGHT_CORE_PATH', '/home/luis/.npm/_npx/fd3bca3c548369c0/node_modules/playwright-core')
    chromium = os.environ.get('CHROMIUM_PATH', '/home/luis/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome')
    if not Path(playwright).exists() or not Path(chromium).exists():
        pytest.skip('Playwright/Chromium unavailable')
    uid = create()
    config = {'url': str(client._client.base_url).rstrip('/') + '/sheet/' + uid,
              'playwright': playwright, 'chromium': chromium, 'output': str(tmp_path)}
    result = subprocess.run(['node', str(Path(__file__).with_name('single_validation_browser.cjs'))],
        input=json.dumps(config), capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(client.stored_calls) == 1
    sheet = client.stored_calls[0]['sheet']['sheet_data']
    assert sheet['rows'][0]['qtd'] == '19'
    assert sheet['header']['operador'] == 'BROWSER FINAL'
    assert sheet['rows'][2]['_deleted'] is True
