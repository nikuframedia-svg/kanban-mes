"""Human row edits on disposable SQLite, with immutable OCR and stable indices."""
from copy import deepcopy
import csv
import io
import uuid
import pytest
from app import db
from app.web import main, plan_review
from app.matching.evidence import build_evidence
from app.matching.carryover import resolve
from app.ocr.coverage import ALGORITHM_VERSION, coverage_resolved, coverage_view, sheet_identity
from tests.test_web import client

TEMPLATE = 'tpl999_kanban' if plan_review.IS_MTG2 else 'cantoneiras_kanban'


def create():
    with db.connect() as conn:
        uid = db.create_sheet(conn, TEMPLATE)
        db.set_extraction(conn, uid, {'header': {}, 'rows': [
            {'of': '250001', 'perfil': 'L50X50X5', 'modelo': 'FIRST', 'qtd': '11'},
            {'of': '250002', 'perfil': 'L60X60X6', 'modelo': 'LAST', 'qtd': '33'}], 'footer': {}})
        return uid, db.get_sheet(conn, uid)


def current(uid):
    with db.connect() as conn:
        return db.get_sheet(conn, uid)


def payload(sheet, **more):
    return {'revision': sheet['revision'], 'request_id': str(uuid.uuid4()),
            'values': {'of': '250009', 'perfil': 'L65X65X5', 'modelo': 'MANUAL', 'qtd': '22'}, **more}


@pytest.mark.parametrize('position,anchor,expected', [('start',None,[2,0,1]), ('before',1,[0,2,1]), ('after',0,[0,2,1]), ('end',None,[0,1,2])])
def test_add_at_paper_position_preserves_ids_original_and_values(client, monkeypatch, position, anchor, expected):
    monkeypatch.setattr(main, 'run_cross_check', lambda *a, **k: True)
    uid, before = create()
    body = payload(before, position=position, anchor=anchor)
    result = client.post(f'/sheet/{uid}/add-row', json=body)
    assert result.status_code == 200, result.text
    after = current(uid)
    assert after['raw_extraction'] == before['raw_extraction']
    assert after['sheet_data']['footer'] == before['sheet_data']['footer']
    rows = after['sheet_data']['rows']
    assert sorted(range(3), key=lambda i: rows[i]['_display_order']) == expected
    for i in (0,1):
        assert {k:v for k,v in rows[i].items() if not k.startswith('_')} == before['sheet_data']['rows'][i]
    assert rows[2]['qtd']=='22' and rows[2]['_manual_entry']['request_id']==body['request_id']
    with db.connect() as conn:
        evidence=build_evidence(after,db.evidence_edits(conn,after))
        assert evidence.data['rows'][2]['modelo']=='MANUAL'
        assert evidence.data['rows'][2]['qtd']=='22'
        assert evidence.data['rows'][2]['_display_order']==expected.index(2)+1
        assert not any('creation' in k for k in evidence.data['rows'][2])
    parsed=list(csv.reader(io.StringIO(client.get(f'/sheet/{uid}/csv').text)))[1:]
    assert [r[-3 if plan_review.IS_MTG2 else -2] for r in parsed] == [str(rows[i]['qtd']) for i in expected]
    # Double clicks/lost responses cannot duplicate a row or its audit.
    same=client.post(f'/sheet/{uid}/add-row',json=body)
    assert same.status_code==200 and same.json()['replayed']
    assert current(uid)['revision']==after['revision']
    assert len(current(uid)['sheet_data']['rows'])==3
    body['values']['qtd']='90'
    assert client.post(f'/sheet/{uid}/add-row',json=body).status_code==409


def test_remove_undo_later_restore_no_reason_and_exports(client,monkeypatch):
    monkeypatch.setattr(main,'run_cross_check',lambda *a,**k:True)
    uid,before=create()
    path=f'/sheet/{uid}/rows/0'
    headers={'Accept':'application/json'}
    response=client.post(path+'/exclude',data={'revision':before['revision']},headers=headers)
    assert response.status_code==200 and response.json()['saved']
    removed=current(uid);row=removed['sheet_data']['rows'][0]
    assert row['_deleted'] and row['_exclusion']['action']=='remove'
    assert 'reason' not in row['_exclusion']
    assert removed['raw_extraction']==before['raw_extraction']
    assert 'FIRST' not in client.get(f'/sheet/{uid}/csv').text
    assert 'LAST' in client.get(f'/sheet/{uid}/csv').text
    assert client.post(path+'/exclude',data={'revision':before['revision']},headers=headers).status_code==409
    assert client.post(path+'/exclude',data={'revision':removed['revision']},headers=headers).json()['revision']==removed['revision']
    page=client.get(f'/sheet/{uid}').text
    assert 'name="reason"' not in page and 'Por justificar' not in page and 'name="duplicate_of"' not in page
    assert 'data-detail-key="excluded"' in page
    assert client.post(path+'/restore',data={'revision':removed['revision']},headers=headers).status_code==200
    after=current(uid)
    assert {k:v for k,v in after['sheet_data']['rows'][0].items() if not k.startswith('_')}==before['sheet_data']['rows'][0]
    with db.connect() as conn:
        evidence=build_evidence(after,db.evidence_edits(conn,after))
        assert evidence.data['rows'][0]['_deleted'] is False
        assert evidence.data['rows'][0]['qtd']=='11'
        events=list(conn.execute("SELECT field_path,old_value FROM edits WHERE sheet_uid=?",(uid,)))
        assert any(e['field_path']=='rows[0]._removal' and 'FIRST' in e['old_value'] for e in events)
    assert 'FIRST' in client.get(f'/sheet/{uid}/csv').text


def test_creation_guards_current_revision_validated_empty_fields_and_limit(client,monkeypatch):
    monkeypatch.setattr(main,'run_cross_check',lambda *a,**k:True)
    uid,before=create()
    for values in ({}, {'qtd':' '}, {'_deleted':'true'}, {'of':'x'*501}):
        assert client.post(f'/sheet/{uid}/add-row',json=payload(before,values=values)).status_code==422
    assert client.post(f'/sheet/{uid}/add-row',json=payload(before,position='before',anchor=99)).status_code==409
    assert current(uid)==before
    assert client.post(f'/sheet/{uid}/add-row',json=payload(before)).status_code==200
    assert client.post(f'/sheet/{uid}/add-row',json=payload(before)).status_code==409
    with db.connect() as conn:
        conn.execute("UPDATE sheets SET status='validated' WHERE uid=?",(uid,));conn.commit()
    frozen=current(uid)
    assert client.post(f'/sheet/{uid}/add-row',json=payload(frozen)).status_code==409
    assert client.post(f'/sheet/{uid}/rows/0/exclude',data={'revision':frozen['revision']}).status_code==409
    assert client.post(f'/sheet/{uid}/rows/0/restore',data={'revision':frozen['revision']}).status_code==409
    assert current(uid)==frozen


def test_lookup_draft_does_not_create_row_and_selection_saved_once(client,monkeypatch):
    monkeypatch.setattr(main,'run_cross_check',lambda *a,**k:True)
    uid,before=create()
    entry={'plan_key':'pk','production_order_no':'OF42','sales_order_no':'OV43',
           'customer_name':'CUSTOMER','profile_type':'L60X60X6','component_ref':'REF','remaining_quantity':999}
    monkeypatch.setattr(plan_review.loaders,'plan_snapshot_info',lambda:{'snapshot_id':'test-snapshot'})
    monkeypatch.setattr(plan_review,'lookup',lambda sid,q,**kw:{'entries':[entry], 'snapshot_id':sid})
    monkeypatch.setattr(plan_review,'fetch_keys',lambda keys,sid:[entry])
    found=client.get(f'/sheet/{uid}/of-lookup?q=42')
    assert found.status_code==200
    assert current(uid)==before
    body=payload(before,snapshot_id=found.json()['snapshot_id'],plan_key='pk')
    assert client.post(f'/sheet/{uid}/add-row',json=body).status_code==200
    row=current(uid)['sheet_data']['rows'][2]
    assert row['modelo']=='REF' and row['of']=='42' and row['qtd']=='22'
    with db.connect() as conn:
        sheet=db.get_sheet(conn,uid);evidence=build_evidence(sheet,db.evidence_edits(conn,sheet))
        assert evidence.explicit_bindings[2]['plan_key']=='pk'


def test_cross_failure_after_partial_save_never_loses_or_duplicates_manual_row(client,monkeypatch):
    def broken(conn,uid):
        sheet=db.get_sheet(conn,uid)
        data=sheet['sheet_data'];data['header']['turno']='M'
        db.save_sheet_data_with_edits(conn,uid,data,sheet['revision'],[])
        raise RuntimeError('synthetic failure after own write')
    monkeypatch.setattr(main,'run_cross_check',broken)
    uid,before=create();body=payload(before)
    result=client.post(f'/sheet/{uid}/add-row',json=body).json()
    assert result['saved'] and result['warning'] and not result['conflict']
    assert result['revision']==current(uid)['revision']
    assert client.post(f'/sheet/{uid}/add-row',json=body).status_code==200
    assert len(current(uid)['sheet_data']['rows'])==3


def test_manual_order_drives_inheritance_without_changing_physical_coordinates():
    rows=[{'of':'11','qtd':'1','_display_order':2,'_paper_position':1},
          {'qtd':'2','_display_order':3,'_paper_position':2},
          {'of':'99','qtd':'3','_display_order':1,'_manual_entry':{'request_id':'test'}}]
    assert resolve(rows,('qtd',))[1].values['of']=='11'
    assert rows[0]['_paper_position']==1


def test_unclassified_exclusions_not_blocked_or_claimed_physically_verified():
    sheet={'image_path':'paper.png','image_sha256':'abc','extraction_generation':1}
    data={'rows':[{'qtd':'1'} for _ in range(6)],'_ocr_coverage':{
        'algorithm_version':ALGORITHM_VERSION,'expected_rows':6,'context':sheet_identity(sheet)}}
    for i in (0,2):data['rows'][i]['_deleted']=True
    view=coverage_view(data,sheet)
    assert view['resolved'] and not view['physical_verified'] and view['status']=='reviewed'
    assert view['included_rows']==4 and view['removed_rows']==2 and view['pending_exclusions']==[]
    # Genuine missing rows and uncertain image counts remain detectable.
    data['_ocr_coverage']['expected_rows']=7
    assert not coverage_resolved(data,sheet)
    data['_ocr_coverage']['expected_rows']=None
    assert not coverage_resolved(data,sheet)


@pytest.mark.parametrize('engine',['legacy','v3'])
def test_real_cross_keeps_manual_quantity_identity_and_restore(client,monkeypatch,engine):
    original_cross=main.run_cross_check
    monkeypatch.setattr(main,'get_employees',lambda:{})
    monkeypatch.setattr(main,'_load_header_machines',lambda:[])
    monkeypatch.setattr(main,'run_cross_check',lambda conn,uid:original_cross(conn,uid,engine_override=engine,historical_context_override=None))
    uid,before=create()
    result=client.post(f'/sheet/{uid}/add-row',json=payload(before,position='before',anchor=1))
    assert result.status_code==200 and not result.json().get('warning'),result.text
    for action in ('exclude','restore'):
        sheet=current(uid)
        result=client.post(f'/sheet/{uid}/rows/2/{action}',data={'revision':sheet['revision']},headers={'Accept':'application/json'})
        assert result.status_code==200 and not result.json().get('warning'),result.text
    with db.connect() as conn:
        assert main.run_cross_check(conn,uid)
    after=current(uid);row=after['sheet_data']['rows'][2]
    assert row['qtd']=='22' and row['_display_order']==2 and row['_deleted'] is False
    assert after['raw_extraction']==before['raw_extraction']


def test_concurrent_write_during_cross_is_not_adopted_or_overwritten(client,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    def racing(conn,uid):
        def edit_in_other_thread():
            with db.connect() as other:
                sheet=db.get_sheet(other,uid);data=sheet['sheet_data'];data['rows'][0]['qtd']='123'
                db.save_sheet_data_with_edits(other,uid,data,sheet['revision'],[('rows[0].qtd','11','123','human','other-tab')])
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(edit_in_other_thread).result()
        sheet=db.get_sheet(conn,uid)
        data=sheet['sheet_data'];data['rows'][0]['qtd']='999'
        db.save_sheet_data_with_edits(conn,uid,data,sheet['revision'],[])
        return True
    monkeypatch.setattr(main,'run_cross_check',racing)
    uid,before=create()
    result=client.post(f'/sheet/{uid}/add-row',json=payload(before)).json()
    assert result['saved'] and result['conflict'] and result['warning']
    after=current(uid)
    assert after['sheet_data']['rows'][0]['qtd']=='123'
    assert len(after['sheet_data']['rows'])==3
    assert result['revision']<after['revision']
