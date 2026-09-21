import copy
import hashlib
import pytest
from app import db, coverage_recovery
from app.matching.evidence import build_evidence
from app.ocr.coverage import coverage_resolved, row_accounting, ALGORITHM_VERSION
from tests.test_web import client
from tests.test_physical_rows import FIXTURES, specimen


def test_recovery_cas_repeat_failure_and_validated_guard(tmp_path):
    rows, sample = specimen()
    image = FIXTURES/'661.png'
    class Provider:
        def extract(self,*a): return {'rows':rows[9:]}
    with db.connect(tmp_path/'test.db') as conn:
        uid=db.create_sheet(conn,'cantoneiras_kanban',str(image),hashlib.sha256(image.read_bytes()).hexdigest())
        db.set_extraction(conn,uid,sample['raw_extraction'])
        before=db.get_sheet(conn,uid)
        assert db.save_sheet_data_with_edits(conn,uid,sample['sheet_data'],before['revision'],[])
        revision=db.get_sheet(conn,uid)['revision']
        result=coverage_recovery.automatic(conn,uid,revision,Provider)
        assert result['added_rows']==1
        after=db.get_sheet(conn,uid)
        assert after['raw_extraction']==before['raw_extraction']
        assert coverage_resolved(after['sheet_data'],after)
        evidence=build_evidence(after,db.evidence_edits(conn,after))
        assert evidence.data['rows'][14]['modelo']=='H92HS4008AT'
        again=coverage_recovery.automatic(conn,uid,after['revision'],Provider)
        assert again['status']=='checked' and db.get_sheet(conn,uid)==after
        with pytest.raises(ValueError):coverage_recovery.automatic(conn,uid,revision,Provider)
        conn.execute("UPDATE sheets SET status='validated' WHERE uid=?",(uid,));conn.commit()
        with pytest.raises(ValueError):coverage_recovery.automatic(conn,uid,after['revision'],Provider)


def test_ocr_failure_and_concurrent_edit_never_replace_existing_rows(tmp_path):
    rows,sample=specimen();image=FIXTURES/'661.png'
    with db.connect(tmp_path/'test.db') as conn:
        uid=db.create_sheet(conn,'cantoneiras_kanban',str(image))
        db.set_extraction(conn,uid,sample['raw_extraction'])
        before=db.get_sheet(conn,uid)
        class Failed:
            def extract(self,*a):raise RuntimeError('OCR unavailable')
        result=coverage_recovery.automatic(conn,uid,before['revision'],Failed)
        assert result['status']=='failed' and result['added_rows']==0
        current=db.get_sheet(conn,uid)
        assert current['sheet_data']['rows']==before['sheet_data']['rows']
        # A new generation may try again; concurrent human edit must win.
        data=copy.deepcopy(current['sheet_data']);data.pop('_coverage_recovery')
        db.save_sheet_data_with_edits(conn,uid,data,current['revision'],[])
        current=db.get_sheet(conn,uid)
        class Racing:
            def extract(self,*a):
                sheet=db.get_sheet(conn,uid);data=copy.deepcopy(sheet['sheet_data'])
                data['rows'][0]['qtd']=99
                db.save_sheet_data_with_edits(conn,uid,data,sheet['revision'],[('rows[0].qtd',4,99,'human','test')])
                return {'rows':rows[9:]}
        with pytest.raises(ValueError,match='mudou'):
            coverage_recovery.automatic(conn,uid,current['revision'],Racing)
        assert db.get_sheet(conn,uid)['sheet_data']['rows'][0]['qtd']==99
        assert len(db.get_sheet(conn,uid)['sheet_data']['rows'])==14


def test_legacy_exclusion_restore_keeps_id_and_blocks_unknown_order(client,monkeypatch):
    from app.web import main
    with db.connect() as conn:
        uid=db.create_sheet(conn,'cantoneiras_kanban')
        raw={'header':{},'rows':[{'cliente':'TECPOLES','perfil':'60x60x5','perf_comp':'X'},
                                {'of':'264993','perfil':'110x110x10','perf_comp':'X'}]}
        db.set_extraction(conn,uid,raw)
        sheet=db.get_sheet(conn,uid);data=copy.deepcopy(sheet['sheet_data'])
        data['rows'][0]['_deleted']=True
        db.save_sheet_data_with_edits(conn,uid,data,sheet['revision'],[('rows[0]',raw['rows'][0],'<apagada>','human','legacy')])
        sheet=db.get_sheet(conn,uid)
    assert coverage_resolved(data)
    assert 'Por justificar' not in client.get(f'/sheet/{uid}').text
    response=client.post(f'/sheet/{uid}/rows/0/restore',data={'revision':sheet['revision']})
    assert response.status_code==303
    with db.connect() as conn:
        sheet=db.get_sheet(conn,uid)
        for engine in ('legacy','v3'):
            main.run_cross_check(conn,uid,engine_override=engine,historical_context_override=None)
            sheet=db.get_sheet(conn,uid);row=sheet['sheet_data']['rows'][0]
            assert row['_deleted'] is False and not row.get('of')
            assert row['perfil']=='60x60x5' and row['_identity_unresolved']
            assert not sheet['cross_check']['rows'][0]['matched_plan_key']
    audit=client.get(f'/sheet/{uid}/rows/0/audit').json()['events']
    assert any(e['new_value']=='<apagada>' for e in audit)
    assert any(e['field_path']=='rows[0]._deleted' and e['new_value']=='false' for e in audit)


def test_exclusion_accounting():
    rows=[{'qtd':1} for _ in range(6)]
    data={'rows':rows,'_ocr_coverage':{'algorithm_version':ALGORITHM_VERSION,'expected_rows':6}}
    assert coverage_resolved(data)
    rows[0]['_deleted']=rows[1]['_deleted']=True
    assert coverage_resolved(data)
    rows[0]['_exclusion']=rows[1]['_exclusion']={'reason':'out_of_scope'}
    assert coverage_resolved(data) and row_accounting(data)['included_rows']==4


def test_ordering_precedes_pagination_and_filters(client):
    with db.connect() as conn:
        for number in range(103):
            uid=db.create_sheet(conn,'cantoneiras_kanban')
            db.set_extraction(conn,uid,{'header':{'operador':'ORDER'},'rows':[]})
            conn.execute('UPDATE sheets SET created_at=? WHERE uid=?',('2026-09-21 12:00:00' if number>=100 else '2026-08-01 12:00:00',uid))
            conn.commit()
        rows=db.list_sheets(conn,status='pending',operador='ORDER')
        assert [s['sheet_no'] for s in rows]==list(range(103,0,-1))
    first=client.get('/?status=pending&operador=ORDER').text
    second=client.get('/?status=pending&operador=ORDER&page=2').text
    assert f'/sheet/{rows[0]["uid"]}' in first and f'/sheet/{rows[100]["uid"]}' not in first
    assert f'/sheet/{rows[100]["uid"]}' in second and f'/sheet/{rows[0]["uid"]}' not in second
