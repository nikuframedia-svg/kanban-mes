from copy import deepcopy
import pytest
from app import db, full_profile_identity
from app.web import main
from app.matching import loaders
from app.matching.refs import PlanIndex
from app.matching.scorer import Scorer
from app.matching.cross_check import check_row
from tests.test_web import client

PROFILES = ['200x20', '90x7', '80x10', '75x6', '75x8', '90x8', '90x7']


def fixture(conn):
    uid = db.create_sheet(conn, 'cantoneiras_kanban')
    data = {'header': {'operador': 'TESTE', 'data': '28/08/2026'}, 'rows': [
        {'of': '263324', 'ov': '2504650', 'cliente': 'CLIENTE', 'perfil': p, 'perf_comp': 'X' if i else None,
         'modelo': None if i else 'AS2209', 'qtd': None if i else '2'} for i,p in enumerate(PROFILES)],
         'footer': {'metros_produzidos': '321'}}
    db.set_extraction(conn, uid, data)
    before = db.get_sheet(conn, uid)
    data = deepcopy(before['sheet_data'])
    for row in data['rows']: row['perfil'] = 'L90X90X7'
    assert db.save_sheet_data_with_edits(conn, uid, data, before['revision'], [])
    return uid


def scorer():
    return Scorer(PlanIndex([
        {'plan_key': str(i), 'of': 'OF263324', 'ov': 'OV2504650', 'cliente': 'CLIENTE', 'perfil': p,
         'modelo': 'AS2209' if i == 1 else f'REF{i}', 'comp_mm': 1000, 'qtd': 5}
        for i,p in enumerate(['L200X200X20','L90X90X7','L80X80X10','L75X75X6','L75X75X8','L90X90X8'])
    ], loaders.CANTONEIRAS_SPEC, snapshot_id='current'))


def test_explicit_full_profile_never_snaps_to_different_geometry():
    match = check_row({'of':'263324', 'perfil':'75x6', 'perf_comp':'X', 'modelo':'AS2209'}, 0, scorer())
    assert match.matched_plan_key == '3'
    profile = next(cell for cell in match.cells if cell.field == 'perfil')
    assert profile.proposal == 'L75X75X6'
    missing = check_row({'of':'263324', 'perfil':'60x5', 'perf_comp':'X', 'modelo':'AS2209'}, 0, scorer())
    assert missing.matched_plan_key is None
    assert not any(cell.auto_write for cell in missing.cells)


def test_555_recovery_only_full_profiles_and_preserves_latest_human_decision(client, monkeypatch):
    monkeypatch.setattr(main, 'get_employees', lambda: {})
    monkeypatch.setattr(main, '_load_header_machines', lambda: [])
    monkeypatch.setattr(main, '_assumed_sheet_date', lambda sheet: None)
    monkeypatch.setattr(loaders, '_fetch', lambda *_: [])
    with db.connect() as conn:
        uid = fixture(conn)
        before = db.get_sheet(conn, uid)
        data = deepcopy(before['sheet_data']); data['rows'][4]['perfil'] = '90x8'
        db.save_sheet_data_with_edits(conn, uid, data, before['revision'], [('rows[4].perfil','75x8','90x8','human','revisor')])
        current = db.get_sheet(conn, uid)
        assert full_profile_identity.corrections(current,db.evidence_edits(conn,current)) == {2:'80x10',3:'75x6',5:'90x8'}
        assert main.run_cross_check(conn, uid, scorer_override=scorer())
        after = db.get_sheet(conn, uid)
        assert [r['perfil'] for r in after['sheet_data']['rows'][1:]] == ['L90X90X7','L80X80X10','L75X75X6','L90X90X8','L90X90X8','L90X90X7']
        assert after['raw_extraction'] == before['raw_extraction']
        assert after['sheet_data']['footer'] == before['sheet_data']['footer']
        assert len(after['sheet_data']['rows']) == 7
        assert main.run_cross_check(conn, uid, scorer_override=scorer())
        again = db.get_sheet(conn, uid)
        assert again['revision'] == after['revision']
        assert not full_profile_identity.corrections(again, db.evidence_edits(conn, again))
        conn.execute("UPDATE sheets SET status='validated' WHERE uid=?", (uid,)); conn.commit()
        frozen = db.get_sheet(conn, uid)
        assert not main.run_cross_check(conn, uid, scorer_override=scorer())
        assert not full_profile_identity.observations(frozen, [])
        assert db.get_sheet(conn, uid) == frozen
