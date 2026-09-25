import copy
from pathlib import Path
import pytest
from PIL import Image
from app import db, header_recovery as recovery
from app.matching.evidence import build_evidence
from app.matching.operador import Employee
from app.web import main
from tests.test_web import client

READING={'operador':'ARSHDEEP SINGH DHINDSA','n_operador':'2849','setor_maquina':'Peddi 8','data':'21/08/26','turno':'M'}
EMPLOYEES={2849:Employee(cod=2849,pernr='10002849',full_name=READING['operador'])}
MACHINES=[{'display_name':'Peddi 8'}]


class Provider:
    name='fake'
    calls=0
    def extract_header(self,image,template):
        self.calls+=1
        assert Image.open(image).height < 600
        return dict(READING)


def create(conn,tmp_path):
    image=tmp_path/'0123456789abcdef_21-08-2026_p1.png';Image.new('RGB',(1200,850),'white').save(image)
    uid=db.create_sheet(conn,'cantoneiras_kanban',str(image),'fixture')
    db.set_extraction(conn,uid,{'header':dict.fromkeys(READING),
        'rows':[{'of':'262797','modelo':f'QA{i}','qtd':str(i+1)} for i in range(13)],
        'footer':{'metros_produzidos':'804','horas_trabalhadas':'6.00'}})
    sheet=db.get_sheet(conn,uid)
    db.save_cross_check(conn,uid,{'rows':[{'row_index':i,'matched_plan_key':f'p{i}', 'line_meters':None, 'p_correct':0.9} for i in range(13)]},sheet['revision'])
    return uid


def call(conn,uid,provider=None):
    sheet=db.get_sheet(conn,uid)
    return recovery.recover(conn,uid,sheet['revision'],provider or Provider(),EMPLOYEES,MACHINES,'20/08/2026')


def test_header_only_preserves_original_rows_footer_and_cross(tmp_path):
    with db.connect(tmp_path/'db.sqlite') as conn:
        uid=create(conn,tmp_path);before=db.get_sheet(conn,uid);provider=Provider()
        assert call(conn,uid,provider)['status']=='review'
        after=db.get_sheet(conn,uid)
        assert after['sheet_data']['header']==READING|{'data':'20/08/2026'}
        assert after['sheet_data']['rows']==before['sheet_data']['rows']
        assert after['sheet_data']['footer']==before['sheet_data']['footer']
        assert after['raw_extraction']==before['raw_extraction']
        assert after['cross_check']['rows']==before['cross_check']['rows']
        assert recovery.date_needs_confirmation(after)
        assert db.human_header_fields(conn,uid)==set()
        assert after['sheet_data']['_header_recovery']['source']=='automatic_header_recovery'
        evidence=build_evidence(after,db.evidence_edits(conn,after))
        assert evidence.data['header']['operador']==READING['operador']
        assert evidence.provenance['field_sources']['header.operador']['source']=='header_recovery'
        assert call(conn,uid,provider)['status']=='already_recovered'
        assert provider.calls==1
        recovery.confirm_rule(conn,uid,after['revision'])
        confirmed=db.get_sheet(conn,uid)
        assert not recovery.date_needs_confirmation(confirmed)
        assert confirmed['sheet_data']['header']['data']=='20/08/2026'
        assert confirmed['raw_extraction']==before['raw_extraction']


def test_manual_values_and_explicit_empty_are_preserved(tmp_path):
    with db.connect(tmp_path/'db.sqlite') as conn:
        uid=create(conn,tmp_path);sheet=db.get_sheet(conn,uid)
        data=copy.deepcopy(sheet['sheet_data']);data['header'].update(operador='NOME CONFIRMADO',data='21/08/2026')
        edits=[('header.operador',None,'NOME CONFIRMADO','human','test'),('header.data',None,'21/08/2026','human','test'),('header.turno',None,None,'human','test')]
        db.save_sheet_data_with_edits(conn,uid,data,sheet['revision'],edits)
        call(conn,uid);after=db.get_sheet(conn,uid)
        assert after['sheet_data']['header']['operador']=='NOME CONFIRMADO'
        assert after['sheet_data']['header']['data']=='21/08/2026'
        assert after['sheet_data']['header']['turno'] is None
        assert not recovery.date_needs_confirmation(after,db.human_header_fields(conn,uid))
        evidence=build_evidence(after,db.evidence_edits(conn,after))
        assert evidence.data['header']['operador']=='NOME CONFIRMADO'
        assert evidence.data['header']['data']=='21/08/2026'


def test_provider_failure_is_visible_and_retryable(tmp_path):
    class Failing:
        name='failed'
        def extract_header(self,*a):raise RuntimeError('OCR indisponível')
    with db.connect(tmp_path/'db.sqlite') as conn:
        uid=create(conn,tmp_path);before=db.get_sheet(conn,uid)
        assert call(conn,uid,Failing())['status']=='failed'
        after=db.get_sheet(conn,uid)
        assert after['sheet_data']['header']==before['sheet_data']['header']
        assert after['sheet_data']['rows']==before['sheet_data']['rows']
        assert 'OCR indisponível' in after['sheet_data']['_header_recovery']['error']
        assert call(conn,uid)['status']=='review'


def test_ambiguous_reading_is_only_a_proposal(tmp_path):
    with db.connect(tmp_path/'db.sqlite') as conn:
        uid=create(conn,tmp_path);sheet=db.get_sheet(conn,uid)
        recovery.recover(conn,uid,sheet['revision'],Provider(),{},[],'20/08/2026')
        after=db.get_sheet(conn,uid)
        assert after['sheet_data']['header']['operador'] is None
        assert after['sheet_data']['_header_recovery']['proposals']['operador']==READING['operador']
        assert build_evidence(after,[]).data['header']['operador'] is None


def test_edit_during_ocr_is_not_overwritten(tmp_path):
    with db.connect(tmp_path/'db.sqlite') as conn:
        uid=create(conn,tmp_path);sheet=db.get_sheet(conn,uid)
        class Concurrent(Provider):
            def extract_header(self,*a):
                data=copy.deepcopy(sheet['sheet_data']);data['rows'][0]['qtd']='999'
                db.save_sheet_data_with_edits(conn,uid,data,sheet['revision'],[('rows[0].qtd','1','999','human','test')])
                return super().extract_header(*a)
        with pytest.raises(ValueError,match='mudou durante'):
            call(conn,uid,Concurrent())
        after=db.get_sheet(conn,uid)
        assert after['sheet_data']['rows'][0]['qtd']=='999'
        assert '_header_recovery' not in after['sheet_data']


def test_validated_refused_and_new_generation_invalidates_recovery(tmp_path):
    with db.connect(tmp_path/'db.sqlite') as conn:
        uid=create(conn,tmp_path);call(conn,uid);after=db.get_sheet(conn,uid)
        changed=copy.deepcopy(after);changed['extraction_generation']+=1
        assert recovery.current_recovery(changed)=={}
        conn.execute("UPDATE sheets SET status='validated' WHERE uid=?",(uid,));conn.commit()
        with pytest.raises(ValueError):call(conn,uid)
        assert db.get_sheet(conn,uid)['sheet_data']==after['sheet_data']


def test_ui_exposes_date_difference_without_separate_confirmation(client,tmp_path,monkeypatch):
    monkeypatch.setattr(main,'run_cross_check',lambda *a,**k:True)
    with db.connect() as conn:
        uid=create(conn,tmp_path);call(conn,uid);after=db.get_sheet(conn,uid)
    page=client.get(f'/sheet/{uid}')
    assert page.status_code==200
    assert '21/08/2026' in page.text and '20/08/2026' in page.text
    assert 'Confirmar data pela regra' not in page.text
    confirmed=client.post(f'/sheet/{uid}/header-recovery/confirm-date',data={'revision':after['revision']})
    assert confirmed.status_code==303
    assert 'Confirmar data pela regra' not in client.get(f'/sheet/{uid}').text
    # Nada bloqueia (25/09): validar não pede uma confirmação à parte da data.
    with db.connect() as conn:
        revision=db.get_sheet(conn,uid)['revision']
    result=client.post(f'/sheet/{uid}/validate',data={'revision':revision})
    assert 'diverg' not in result.headers.get('location','')
    assert 'erro=' not in result.headers.get('location','')
    assert len(client.stored_calls)==1


def test_only_one_worker_and_lock_is_released_after_failure(tmp_path):
    from app.recovery_lock import single_worker
    with db.connect(tmp_path/'lock.db') as conn:
        uid=create(conn,tmp_path)
        with single_worker(conn):
            with pytest.raises(ValueError,match='em curso'):
                call(conn,uid)
        assert call(conn,uid)['status']=='review'


def test_batch_inventory_and_failure_stop_preserve_unprocessed_sheets(tmp_path,monkeypatch,capsys):
    import sys
    from scripts import recover_headers_pending as cli
    database=tmp_path/'batch.db'
    with db.connect(database) as conn:
        first=create(conn,tmp_path)
        second=create(conn,tmp_path)
        frozen=db.get_sheet(conn,second)
    args=['recovery','--database',str(database)]
    monkeypatch.setattr(sys,'argv',args)
    assert cli.main()==0
    with db.connect(database) as conn:
        assert db.get_sheet(conn,second)==frozen
    class Offline(Provider):
        def extract_header(self,*args):
            raise RuntimeError('OCR indisponível')
    monkeypatch.setattr(main,'get_provider',lambda:Offline())
    monkeypatch.setattr(main,'get_employees',lambda:EMPLOYEES)
    monkeypatch.setattr(main,'_load_header_machines',lambda:MACHINES)
    monkeypatch.setattr(sys,'argv',args+['--apply','--backup',str(tmp_path/'backup.db')])
    assert cli.main()==1
    with db.connect(database) as conn:
        attempted=[db.get_sheet(conn,uid) for uid in (first,second)]
        assert sum('_header_recovery' in s['sheet_data'] for s in attempted)==1
        assert all(s['raw_extraction']==frozen['raw_extraction'] for s in attempted)
        assert all(s['sheet_data']['rows']==frozen['sheet_data']['rows'] for s in attempted)
    monkeypatch.setattr(main,'get_provider',lambda:Provider())
    monkeypatch.setattr(sys,'argv',args+['--apply','--backup',str(tmp_path/'resumed.db')])
    assert cli.main()==0
    with db.connect(database) as conn:
        assert all(recovery.current_recovery(db.get_sheet(conn,uid))['status']=='review' for uid in (first,second))


def test_automatic_header_job_starts_once_and_only_date_exception_remains(client,tmp_path,monkeypatch):
    import time
    provider=Provider()
    monkeypatch.setattr(main,'get_provider',lambda:provider)
    monkeypatch.setattr(main,'get_employees',lambda:EMPLOYEES)
    monkeypatch.setattr(main,'_load_header_machines',lambda:MACHINES)
    with db.connect() as conn:
        uid=create(conn,tmp_path);before=db.get_sheet(conn,uid)
    page=client.get(f'/sheet/{uid}')
    assert 'data-automatic-review=' in page.text
    assert 'Recuperar cabeçalho</button>' not in page.text
    assert client.post(f'/sheet/{uid}/automatic-review',data={'revision':before['revision']}).status_code==200
    for _ in range(100):
        job=client.get(f'/sheet/{uid}/automatic-review').json()
        if job['status'] not in {'queued','running'}:break
        time.sleep(.02)
    assert job['status']=='complete'
    page=client.get(f'/sheet/{uid}')
    assert 'data-automatic-review=' not in page.text
    assert 'Confirmar data pela regra' not in page.text
    with db.connect() as conn:
        after=db.get_sheet(conn,uid)
        assert after['sheet_data']['rows']==before['sheet_data']['rows']
        recovery.confirm_rule(conn,uid,after['revision'])
    page=client.get(f'/sheet/{uid}')
    assert 'Confirmar data pela regra' not in page.text
    assert 'Confirma estes dados' not in page.text
    assert provider.calls==1


def test_failed_automatic_header_attempt_does_not_loop(client,tmp_path,monkeypatch):
    import time
    class Offline(Provider):
        def extract_header(self,*args):
            raise RuntimeError('offline')
    monkeypatch.setattr(main,'get_provider',lambda:Offline())
    monkeypatch.setattr(main,'get_employees',lambda:{})
    monkeypatch.setattr(main,'_load_header_machines',lambda:[])
    with db.connect() as conn:
        uid=create(conn,tmp_path);sheet=db.get_sheet(conn,uid)
    client.post(f'/sheet/{uid}/automatic-review',data={'revision':sheet['revision']})
    for _ in range(100):
        if client.get(f'/sheet/{uid}/automatic-review').json()['status'] not in {'queued','running'}:break
        time.sleep(.02)
    page=client.get(f'/sheet/{uid}')
    assert 'data-start="true"' not in page.text
    assert 'data-status="error"' in page.text
    job = client.get(f'/sheet/{uid}/automatic-review').json()
    assert job['status'] == 'error'
    assert job['final_revision'] == job['current_revision']
    repeated = client.post(f'/sheet/{uid}/automatic-review', data={'revision': job['final_revision']}).json()
    assert repeated['job_id'] == job['job_id']
    assert 'Tentar leitura novamente' in page.text
