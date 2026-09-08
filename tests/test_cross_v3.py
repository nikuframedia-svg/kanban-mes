"""TPL102 contract tests, independent of automatic substitutions."""
import copy
import random
from decimal import Decimal

import pytest

from app import db, production_facts
from app.matching import similarity
from app.matching.angle_geometry import parse_profile, profile_key
from app.matching.evidence import build_evidence
from app.matching.loaders import CANTONEIRAS_SPEC
from app.matching.params import CrossParams
from app.matching.refs import PlanIndex
from app.matching.v3 import _Plan, check_sheet_v3
from app.templates_spec import get_template


def entry(key='A', **kw):
    return dict(plan_key=key, of=kw.pop('of', 'OF265574'), ov='OV2600123',
                cliente='CLIENTE', perfil=kw.pop('perfil', 'L60X60X5'),
                modelo=kw.pop('modelo', key), comp_mm=kw.pop('comp_mm', 1000),
                qtd_planeada=10, qtd_restante=kw.pop('qtd_restante', 5),
                falta_valida=kw.pop('falta_valida', True), regra_calculo='qtd-minus-made', **kw)


def run(rows, entries=None, **kw):
    return check_sheet_v3({'rows': rows}, index=PlanIndex(
        entries if entries is not None else [entry()], CANTONEIRAS_SPEC, snapshot_id='S'), **kw)


@pytest.mark.parametrize('written,truth', [('60x5','L60X60X5'),('L60x5','L60X60X5'),
    ('60×5','L60X60X5'),('100x50x6','L100X50X6'),('L60x2,9','L60X60X2.9')])
def test_geometry_keeps_legs_and_thickness(written, truth):
    assert profile_key(written) == truth
    rc = run([{'perfil': written, 'qtd': '2'}],
             [entry('WRONG',perfil='L60X60X29'),entry('RIGHT',perfil=truth)])['rows'][0]
    assert rc['matched_plan_key'] == 'RIGHT'
    assert rc['evidence_groups']['geometry']['observed_length_mm'] is None
    assert parse_profile('60x2.9').dimensions[-1] == Decimal('2.9')


@pytest.mark.parametrize('marker', ['X', '✓', 'sim', '5000'])
def test_old_comp_mm_never_supplies_cut_length(marker):
    result = run([{'perfil':'60x5','comp_mm':marker,'qtd':'2'}],
                 [entry('A',comp_mm=1000),entry('B',comp_mm=5000)])['rows'][0]
    assert result['evidence_groups']['geometry']['length_score'] == 0
    assert result['actual_line_meters'] is None


def test_full_profile_keeps_zero_children_and_partial_meters():
    rows = [{'of':'265574','perfil':'60x5','modelo':'WRITTEN','qtd':'777','perf_comp':'X'}]
    entries = [entry('A'),entry('ZERO',qtd_restante=0),entry('MISSING',comp_mm=None)]
    cross = run(rows,entries)
    rc = cross['rows'][0]
    assert rc['plan_refs_valid']
    assert len(rc['plan_refs']) == 3
    assert rc['line_meters'] is None
    assert cross['summary']['metros_parciais']
    assert next(c for c in rc['cells'] if c['field']=='modelo')['proposal'] == ''
    sheet = {'sheet_data':{'rows':rows},'cross_check':cross}
    facts = production_facts.materialize_sheet(sheet,get_template('cantoneiras_kanban'))
    assert len(facts['parents']) == 1 and facts['parents'][0]['aggregate']
    assert len(facts['plan_refs']) == 3 and len(facts['exports']) == 2
    assert rows[0]['qtd'] == '777' and rows[0]['perf_comp'] == 'X'


@pytest.mark.parametrize('value,flag',[(None,True),(-1,True),(3,False)])
def test_full_profile_invalid_remaining_blocks(value,flag):
    rc = run([{'perfil':'60x5','perf_comp':'X'}],
             [entry(qtd_restante=value,falta_valida=flag)])['rows'][0]
    assert rc['plan_refs_valid'] is False
    assert rc['plan_refs'][0]['assumed_quantity'] is None


def test_all_production_has_identity_and_activity_is_a_barrier():
    rows=[{'qtd':'2'},{'cliente':'LIMPEZA'},{}, {'cliente':'INTERNO','qtd':'1'},
          {'modelo':'discard','_deleted':True}]
    cross=run(rows)
    assert [r['row_kind'] for r in cross['rows']] == ['production','activity','empty','production']
    for rc in cross['rows']:
        if rc['row_kind']=='production':
            assert rc['matched_plan_key']
            assert {c['field'] for c in rc['cells']} >= {'of','ov','cliente','perfil','modelo'}
    facts=production_facts.materialize_sheet({'sheet_data':{'rows':rows},'cross_check':cross},
                                             get_template('cantoneiras_kanban'))
    assert [p['row_index'] for p in facts['parents']]==[0,3]
    unavailable=run(rows,[])
    assert unavailable['summary']['matched']==0 and rows[0]=={'qtd':'2'}


def test_explicit_choice_current_and_expired():
    bindings={0:{'selected_explicitly':True,'snapshot_id':'S','plan_key':'B'}}
    rc=run([{'modelo':'A'}],[entry('A'),entry('B')],explicit_bindings=bindings)['rows'][0]
    assert rc['matched_plan_key']=='B' and rc['selected_explicitly']
    bindings[0]['snapshot_id']='OLD'
    rc=run([{'modelo':'A'}],[entry('A'),entry('B')],explicit_bindings=bindings)['rows'][0]
    assert rc['matched_plan_key']=='A' and rc['binding_status']=='reselected'


def test_repetition_reordering_and_equivalent_duplication():
    rows=[{'perfil':'60x5','modelo':'A','qtd':'2'},{'qtd':'3'}]
    entries=[entry('A'),entry('B',of='OF265573')]
    index=PlanIndex(entries,CANTONEIRAS_SPEC,snapshot_id='S')
    first=check_sheet_v3({'rows':rows},index=index)
    assert first==check_sheet_v3({'rows':rows},index=index)
    reordered=run(rows,list(reversed(entries)))
    duplicated=run(rows,entries+[entry('A2',modelo='A')])
    for other in (reordered,duplicated):
        for a,b in zip(first['rows'],other['rows']):
            assert a['score']==b['score']
            assert [(c['field'],c['proposal'],c['p_correct']) for c in a['cells'] if c['field']!='qtd']==[
                (c['field'],c['proposal'],c['p_correct']) for c in b['cells'] if c['field']!='qtd']


def test_prefilter_matches_exhaustive_top_ten_including_exact_ties():
    rng=random.Random(915)
    values=[''.join(rng.choices('ABCD012345',k=rng.randrange(4,10))) for _ in range(400)]
    index=PlanIndex([entry(str(i),modelo=v) for i,v in enumerate(values)],CANTONEIRAS_SPEC)
    plan=_Plan(index,CrossParams(),None)
    for written in values[::13]+['XXXX','A0000']:
        scored=sorted((-similarity.ratio(written,v),v) for v in plan.keys['modelo']
                      if abs(len(written)-len(v))<=3 and similarity.ratio(written,v)>=.6)[:10]
        expected=set().union(*(plan.maps['modelo'][v] for _,v in scored)) if scored else set()
        assert plan.fuzzy('modelo',written)==expected


def test_evidence_alias_humans_and_generation_do_not_use_materialized_identity(tmp_path):
    conn=db.connect(tmp_path/'app.db')
    uid=db.create_sheet(conn,'cantoneiras_kanban')
    raw={'rows':[{'perfil':'60x5','comp_mm':'X','qtd':'2'}],'footer':{'metros_produzidos':'77'}}
    assert db.set_extraction(conn,uid,raw)
    sheet=db.get_sheet(conn,uid)
    assert sheet['extraction_generation']==1
    db.record_edit(conn,uid,'rows[0].modelo',None,'HUMAN','human','tester')
    db.record_edit(conn,uid,'rows[0].comp_mm','X',None,'human','tester')
    sheet['sheet_data']['rows'][0].update(of='AUTO',modelo='AUTO')
    evidence=build_evidence(sheet,db.evidence_edits(conn,sheet))
    assert evidence.data['rows'][0]['modelo']=='HUMAN'
    assert 'of' not in evidence.data['rows'][0]
    assert evidence.data['rows'][0]['perf_comp'] is None
    assert 'comp_mm' not in evidence.data['rows'][0]
    assert evidence.data['footer']==raw['footer']
    assert db.mark_pending(conn,uid)
    assert db.set_extraction(conn,uid,raw)
    new=db.get_sheet(conn,uid)
    assert new['sheet_no']==sheet['sheet_no'] and new['extraction_generation']==2
    assert db.evidence_edits(conn,new)==[]
    conn.close()


def test_reused_exact_component_code_outweighs_rare_glyph_confusion():
    entries = [entry('exact', modelo='D8F17'), entry('near', modelo='D8F77')]
    entries += [entry('copy'+str(i), modelo='D8F17', of='OF'+str(700000+i)) for i in range(40)]
    result = run([{'modelo':'D8F17', 'qtd':'2'}], entries)['rows'][0]
    assert next(c for c in result['cells'] if c['field']=='modelo')['proposal'] == 'D8F17'
