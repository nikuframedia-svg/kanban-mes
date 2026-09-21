from dataclasses import replace
import copy

import pytest

from app import db, production_facts
from app.matching import history
from app.matching.scorer import Scorer
from app.matching.params import CrossParams
from app.matching.refs import PlanIndex
from app.matching.loaders import CANTONEIRAS_SPEC
from app.templates_spec import get_template
from app.web import main
from tests.test_web import client, create_sheet, edit, get_revision


@pytest.fixture
def v3_client(client, monkeypatch):
    monkeypatch.setattr(main, 'settings', replace(main.settings, cross_engine='v3'))
    monkeypatch.setattr(history, 'load_history_context', lambda *args: None)
    monkeypatch.setattr(main, '_load_header_machines', lambda: [])
    index=PlanIndex([
        {'plan_key':'A','of':'OF123','ov':'OV111','cliente':'PLAN','perfil':'L60X60X5','modelo':'MODEL-A',
         'comp_mm':1000,'qtd_planeada':20,'qtd_restante':10,'falta_valida':True,'regra_calculo':'calculated:qtd_minus_maq'},
        {'plan_key':'B','of':'OF123','ov':'OV111','cliente':'PLAN','perfil':'L60X60X5','modelo':'MODEL-B',
         'comp_mm':2000,'qtd_planeada':20,'qtd_restante':0,'falta_valida':True,'regra_calculo':'calculated:qtd_minus_maq'},
    ],CANTONEIRAS_SPEC,snapshot_id='S')
    from tests.historical_fixtures import install
    install(monkeypatch, index.entries)
    monkeypatch.setattr(main,'get_index', lambda name: index)
    monkeypatch.setattr(main,'make_fresh_scorer', lambda name: Scorer(index,CrossParams()))
    monkeypatch.setattr(main.loaders,'plan_snapshot_info',lambda:{'snapshot_id':'S'})
    monkeypatch.setattr(main,'get_employees',lambda:{})
    return client


def sheet(uid):
    with db.connect() as conn:return db.get_sheet(conn,uid)


def test_live_projection_is_repeatable_and_maintains_observations(v3_client):
    uid=create_sheet(v3_client)
    assert edit(v3_client,uid,'rows[0].qtd','7').status_code==303
    first=sheet(uid)
    with db.connect() as conn: edits=db.edit_count(conn,uid)
    assert v3_client.post(f'/sheet/{uid}/recheck').status_code==303
    second=sheet(uid)
    assert second['sheet_data']['rows'][0]['qtd']=='7'
    assert second['raw_extraction']==first['raw_extraction']
    assert second['cross_check']==first['cross_check']
    assert second['revision']==first['revision']
    assert second['cross_check']['data_revision']==second['revision']
    with db.connect() as conn:assert db.edit_count(conn,uid)==edits
    html=v3_client.get(f'/sheet/{uid}').text
    assert 'Substituído' in html and 'Pouca evidência' in html and 'Estimativa' in html
    assert 'Origem e alternativas' in html
    raw=v3_client.get(f'/sheet/{uid}?view=raw').text
    assert 'Origem e alternativas' not in raw
    assert 'value="7"' not in raw


def test_reference_selection_uses_exact_fresh_index_and_human_audit(v3_client):
    uid=create_sheet(v3_client);edit(v3_client,uid,'rows[0].of','123')
    result=v3_client.post(f'/sheet/{uid}/rows/0/reference',data={
        'plan_key':'B','snapshot_id':'S','revision':get_revision(v3_client,uid)})
    assert result.status_code==303
    current=sheet(uid)
    assert current['sheet_data']['rows'][0]['modelo']=='MODEL-B'
    assert current['cross_check']['rows'][0]['selected_explicitly']
    assert current['cross_check']['data_revision']==current['revision']
    with db.connect() as conn:
        events=db.evidence_edits(conn,current)
    assert any(e['field_path']=='rows[0]._plan_binding' for e in events)


def test_profile_complete_refuses_invalid_and_preserves_zero_audit(v3_client):
    uid=create_sheet(v3_client)
    edit(v3_client,uid,'rows[0].perf_comp','X')
    current=sheet(uid)
    rc=current['cross_check']['rows'][0]
    assert rc['plan_refs_valid'] and len(rc['plan_refs'])==2
    facts=production_facts.materialize_sheet(current,get_template('cantoneiras_kanban'))
    assert len(facts['parents'])==1 and len(facts['exports'])==1 and len(facts['plan_refs'])==2
    index=main.get_index('load_cantoneiras_index')
    invalid=copy.deepcopy(index.entries);invalid[0]['qtd_restante']=None
    with db.connect() as conn:
        assert main.run_cross_check(conn,uid,engine_override='v3',scorer_override=Scorer(
            PlanIndex(invalid,CANTONEIRAS_SPEC,snapshot_id='S'),CrossParams()),historical_context_override=None)
    assert sheet(uid)['cross_check']['rows'][0]['plan_refs_valid'] is True
    assert sheet(uid)['cross_check']['rows'][0]['plan_refs'] == rc['plan_refs']


def test_web_cas_conflict_preserves_measurement(v3_client,monkeypatch):
    uid=create_sheet(v3_client);edit(v3_client,uid,'rows[0].qtd','7')
    old=sheet(uid)
    edit(v3_client,uid,'rows[0].qtd','8')
    result=v3_client.post(f'/sheet/{uid}/edit',data={'field_path':'rows[0].qtd','value':'99',
                                                  'revision':old['revision']})
    assert result.status_code==409
    assert sheet(uid)['sheet_data']['rows'][0]['qtd']=='8'
    assert '99' in result.text


def test_health_has_only_process_identity(v3_client):
    response=v3_client.get('/health');assert response.status_code==200
    payload=response.json()
    assert set(payload)=={'app','engine','platform','code_fingerprint','commit'}
    assert payload['app']=='kanban-mes' and len(payload['code_fingerprint'])==64
