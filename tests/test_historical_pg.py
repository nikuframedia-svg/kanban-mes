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
    assert pg_store.store_validated_sheet(sheet,template,0,'test')==1
    with psycopg.connect(clean_history) as conn:
        records=conn.execute('SELECT quantity,plan_snapshot_id FROM mes_kanban.production_records WHERE sheet_uid=%s',(sheet['uid'],)).fetchall()
        refs=conn.execute('SELECT component_ref,assumed_quantity,plan_snapshot_id FROM mes_kanban.production_record_plan_refs WHERE sheet_uid=%s ORDER BY component_ref',(sheet['uid'],)).fetchall()
    assert float(records[0][0])==106 and records[0][1]=='snapshot-15sep'
    assert [(r[0],float(r[1]),r[2]) for r in refs]==[('EA8B78',54,'snapshot-15sep'),('EA8B79',52,'snapshot-15sep')]
    frozen=copy.deepcopy(sheet)
    sheet['status']='validated'
    historical_quantities.apply(sheet,sheet['sheet_data'],sheet['cross_check'],snapshot_loader=lambda *_:pytest.fail('validated'))
    assert sheet['cross_check']==frozen['cross_check']
    assert pg_store.store_validated_sheet(sheet,template,0,'test')==1
    from app import production_facts
    if pg_store.SOURCE_APP.endswith('mtg2'):
        exported=production_facts.materialize_sheet(sheet)[0]['export_rows']
        assert sum(float(row['qtd']) for row,_ in exported)==106
    else:
        exported=production_facts.materialize_sheet(sheet,template)['exports']
        assert sum(float(f['row']['qtd']) for f in exported)==106
