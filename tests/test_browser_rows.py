"""Exercise the real HTTP mutation routes in a browser, only on disposable DBs."""
import json
import os
import subprocess
from pathlib import Path
import pytest
from app.web import main, plan_review
from tests.test_web import client
from tests.test_row_actions import create, current


@pytest.mark.skipif(not os.environ.get('PLAYWRIGHT_MODULE'), reason='Browser checks are opt-in')
def test_browser_rows_one_click_no_reload_preserves_drafts_and_conflicts(client,monkeypatch,tmp_path):
    monkeypatch.setattr(main,'run_cross_check',lambda *a,**kw:True)
    service=main.coverage_routes.automatic if plan_review.IS_MTG2 else main.header_recovery_routes.automatic
    monkeypatch.setattr(service,'eligible',lambda *a:False)
    uid,before=create()
    entry={'plan_key':'pk','production_order_no':'OF42','sales_order_no':'OV43',
           'customer_name':'CUSTOMER','profile_type':'L60X60X6','component_ref':'REF','remaining_quantity':999}
    monkeypatch.setattr(plan_review.loaders,'plan_snapshot_info',lambda:{'snapshot_id':'test-snapshot'})
    monkeypatch.setattr(plan_review,'lookup',lambda sid,q,**kw:{'entries':[entry], 'snapshot_id':sid,'offset':0,'has_more':False})
    monkeypatch.setattr(plan_review,'fetch_keys',lambda keys,sid:[entry])
    config=tmp_path/'browser.json'
    config.write_text(json.dumps({'url':str(client._client.base_url).rstrip('/')+f'/sheet/{uid}', 'uid':uid}))
    subprocess.run(['node',str(Path(__file__).with_suffix('.cjs')),str(config)],check=True,timeout=90)
    after=current(uid)
    assert after['raw_extraction']==before['raw_extraction']
    assert len(after['sheet_data']['rows'])==5
    assert after['sheet_data']['rows'][3]['qtd']=='8'
