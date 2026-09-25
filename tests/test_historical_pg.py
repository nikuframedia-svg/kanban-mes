import copy
import os
import uuid
import psycopg
import pytest
from app import pg_store, historical_quantities
from app.templates_spec import get_template
from tests.disposable_postgres import postgres16, clean_history
from tests.test_historical_quantities import entries

pytestmark = pytest.mark.skipif(os.environ.get('RUN_PG_INTEGRATION') != '1', reason='Disposable Docker PostgreSQL only')


def test_historical_parent_children_and_repeat_preserve_facts(clean_history):
    template=get_template('tpl999_kanban' if pg_store.SOURCE_APP.endswith('mtg2') else 'cantoneiras_kanban')
    sheet={'uid':uuid.uuid4().hex[:12], 'sheet_no':1, 'template_name':template.name,
           'image_sha256':'f'*64, 'raw_extraction':{}, 'status':'in_review',
           'sheet_data':{'header':{'data':'16/09/2026','operador':'TEST'},
                         'rows':[{'of':'264534','perfil':'L55X55X5','perf_comp':'X'}], 'footer':{}},
           'cross_check':{'snapshot_id':'current-zero','rows':[{'row_index':0,'matched_plan_key':'current-key','cells':[]}]}}
    historical_quantities.apply(sheet,sheet['sheet_data'],sheet['cross_check'],
        snapshot_loader=lambda day:{'snapshot_id':'snapshot-15sep'}, order_loader=lambda *a:entries())
    assert pg_store.store_validated_sheet(sheet,template,0,'test').row_count==1
    with psycopg.connect(clean_history) as conn:
        records=conn.execute('SELECT quantity,plan_snapshot_id FROM mes_kanban.production_records WHERE sheet_uid=%s',(sheet['uid'],)).fetchall()
        refs=conn.execute('SELECT component_ref,assumed_quantity,plan_snapshot_id FROM mes_kanban.production_record_plan_refs WHERE sheet_uid=%s ORDER BY component_ref',(sheet['uid'],)).fetchall()
    assert float(records[0][0])==106 and records[0][1]=='snapshot-15sep'
    assert [(r[0],float(r[1]),r[2]) for r in refs]==[('EA8B78',54,'snapshot-15sep'),('EA8B79',52,'snapshot-15sep')]
    frozen=copy.deepcopy(sheet)
    sheet['status']='validated'
    historical_quantities.apply(sheet,sheet['sheet_data'],sheet['cross_check'],snapshot_loader=lambda *_:pytest.fail('validated'))
    assert sheet['cross_check']==frozen['cross_check']
    assert pg_store.store_validated_sheet(sheet,template,0,'test').row_count==1
    from app import production_facts
    if pg_store.SOURCE_APP.endswith('mtg2'):
        exported=production_facts.materialize_sheet(sheet)[0]['export_rows']
        assert sum(float(row['qtd']) for row,_ in exported)==106
    else:
        exported=production_facts.materialize_sheet(sheet,template)['exports']
        assert sum(float(f['row']['qtd']) for f in exported)==106


def test_removed_row_never_enters_production_and_manual_order_keeps_stable_ids(clean_history):
    template=get_template('tpl999_kanban' if pg_store.SOURCE_APP.endswith('mtg2') else 'cantoneiras_kanban')
    sheet={'uid':uuid.uuid4().hex[:12], 'sheet_no':2, 'template_name':template.name,
           'image_sha256':'e'*64, 'raw_extraction':{}, 'status':'in_review',
           'sheet_data':{'header':{'data':'16/09/2026','operador':'TEST'}, 'footer':{}, 'rows':[
               {'of':'264534','perfil':'L55X55X5','modelo':'EA8B78','qtd':'2','_display_order':2},
               {'of':'264534','perfil':'L55X55X5','modelo':'REMOVED','qtd':'999','_deleted':True,
                '_exclusion':{'action':'remove'},'_display_order':3},
               {'of':'264534','perfil':'L55X55X5','modelo':'EA8B79','qtd':'4','_display_order':1,
                '_manual_entry':{'request_id':'manual-integration'}}]},
           'cross_check':{'snapshot_id':'current','rows':[]}}
    pg_store.store_validated_sheet(sheet,template,0,'test')
    with psycopg.connect(clean_history) as conn:
        records=conn.execute('SELECT row_index,quantity FROM mes_kanban.production_records WHERE sheet_uid=%s ORDER BY row_index',(sheet['uid'],)).fetchall()
    assert [(r[0],float(r[1])) for r in records]==[(0,2),(2,4)]
    from app.web.export_routes import facts_for
    exported=list(facts_for(sheet))
    assert [i for i,_,_ in exported]==[2,0]
    assert [float(row['qtd']) for _,row,_ in exported]==[4,2]
    before=copy.deepcopy(sheet)
    sheet['status']='validated'
    pg_store.store_validated_sheet(sheet,template,0,'test')
    assert sheet['sheet_data']==before['sheet_data']


def test_row_warnings_are_stored_with_the_production_record(clean_history):
    template=get_template('tpl999_kanban' if pg_store.SOURCE_APP.endswith('mtg2') else 'cantoneiras_kanban')
    uid=uuid.uuid4().hex[:12]
    sheet={'uid':uid, 'sheet_no':3, 'template_name':template.name,
           'image_sha256':'d'*64, 'raw_extraction':{}, 'status':'in_review',
           'sheet_data':{'header':{'data':'16/09/2026','operador':''}, 'footer':{},
                         'rows':[{'of':'264534','perfil':'L55X55X5','modelo':'EA8B78','qtd':'2'}]},
           'cross_check':{'snapshot_id':'current','rows':[{'row_index':0,'cells':[]}],
                          'validation_warnings':[
                              {'code':'operador_vazio','message':'Operador por preencher.'},
                              {'code':'sem_ligacao_ao_plano','message':'Linha 1: sem correspondência.',
                               'row':1,'row_index':0}]}}
    pg_store.store_validated_sheet(sheet,template,0,'test')
    with psycopg.connect(clean_history) as conn:
        record=conn.execute('SELECT extra, matched_plan_key, operator_name FROM mes_kanban.production_records WHERE sheet_uid=%s',(uid,)).fetchone()
        stored=conn.execute("SELECT cross_check->'validation_warnings' FROM mes_kanban.validated_sheets WHERE sheet_uid=%s",(uid,)).fetchone()[0]
    assert record[0]['warnings']==[{'code':'sem_ligacao_ao_plano','message':'Linha 1: sem correspondência.'}]
    assert record[1] is None and record[2]=='(desconhecido)'
    assert [w['code'] for w in stored]==['operador_vazio','sem_ligacao_ao_plano']


def test_click_time_is_stored_and_second_attempt_only_confirms(clean_history):
    template=get_template('tpl999_kanban' if pg_store.SOURCE_APP.endswith('mtg2') else 'cantoneiras_kanban')
    uid=uuid.uuid4().hex[:12]
    sheet={'uid':uid, 'sheet_no':4, 'template_name':template.name,
           'image_sha256':'e'*64, 'raw_extraction':{}, 'status':'validated',
           'sheet_data':{'header':{'data':'16/09/2026','operador':'ANA'}, 'footer':{},
                         'rows':[{'of':'264534','perfil':'L55X55X5','modelo':'EA8B78','qtd':'2'}]},
           'cross_check':{'snapshot_id':'current','rows':[{'row_index':0,'cells':[]}]}}
    clicked='2026-09-16T10:15:00+00:00'
    first=pg_store.store_validated_sheet(sheet,template,0,'test',validated_at=clicked)
    # O sync_worker caiu antes de confirmar localmente: a nova tentativa não duplica.
    again=pg_store.store_validated_sheet(sheet,template,0,'test',validated_at=clicked)
    assert not first.already_stored and again.already_stored
    assert again.sheet_no==first.sheet_no and again.row_count==first.row_count
    with psycopg.connect(clean_history) as conn:
        sheet_at=conn.execute('SELECT validated_at FROM mes_kanban.validated_sheets WHERE sheet_uid=%s',(uid,)).fetchone()[0]
        rows=conn.execute('SELECT validated_at FROM mes_kanban.production_records WHERE sheet_uid=%s',(uid,)).fetchall()
    assert sheet_at.isoformat()==clicked and [r[0].isoformat() for r in rows]==[clicked]


def test_historico_atribui_o_numero_seguinte_quando_o_provisorio_esta_ocupado(clean_history):
    """Antes, um número local já usado no histórico era um erro que o
    operador não conseguia resolver; agora o histórico dá o seguinte livre."""
    template=get_template('cantoneiras_kanban')
    def make(sheet_no):
        return {'uid':uuid.uuid4().hex[:12], 'sheet_no':sheet_no, 'template_name':template.name,
                'image_sha256':uuid.uuid4().hex*2, 'raw_extraction':{}, 'status':'validated',
                'sheet_data':{'header':{'data':'16/09/2026','operador':'ANA'}, 'footer':{},
                              'rows':[{'of':'264534','perfil':'L55X55X5','modelo':'EA8B78','qtd':'2'}]},
                'cross_check':{'snapshot_id':'current','rows':[{'row_index':0,'cells':[]}]}}
    first=pg_store.store_validated_sheet(make(7),template,0,'test')
    second=pg_store.store_validated_sheet(make(7),template,0,'test',minimum_sheet_no=5)
    assert (first.sheet_no, first.next_sheet_no)==(7, 8)
    assert (second.sheet_no, second.next_sheet_no)==(8, 9)


def test_registo_de_migracoes_marca_so_o_que_existe(postgres16):
    admin_dsn, _app_dsn = postgres16
    with psycopg.connect(admin_dsn) as conn:
        versions = [r[0] for r in conn.execute(
            'SELECT version FROM mes_kanban.schema_migrations ORDER BY version')]
    assert versions == ['010_mes_kanban', '011_mes_paragens', '012_perfil_e_marca',
                        '013_operador', '014_metros', '015_family_perfis',
                        '016_mtg2_schema_v2', '017_plan_binding_sheet_numbers',
                        '018_schema_migrations']
