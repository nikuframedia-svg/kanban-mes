"""Real paper 686: OCR fused a complete-profile row into A18F31."""
import copy
import hashlib
import pytest
from app import db, coverage_recovery, production_facts
from app.matching.loaders import CANTONEIRAS_SPEC
from app.matching.refs import PlanIndex
from app.matching.scorer import Scorer
from app.matching.params import CrossParams
from app.ocr.coverage import coverage_resolved
from app.templates_spec import get_template
from app.web import main
from tests.test_web import client
from tests.test_physical_rows import specimen_686, FIXTURES
from tests.historical_fixtures import install


@pytest.mark.parametrize('engine',['legacy','v3'])
def test_full_recovery_cross_check_historical_children_and_export(client,monkeypatch,engine):
    paper,sample=specimen_686();image=FIXTURES/'686.png'
    entries=[dict(plan_key=r['modelo'],of='OF264857',ov='OV2601327',cliente='TECPOLES',
                  perfil=r['perfil'],modelo=r['modelo'],comp_mm=1524,qtd_planeada=100,
                  qtd_restante=100,falta_valida=True,regra_calculo='calculated:qtd_minus_maq')
             for r in sample['sheet_data']['rows']]
    entries.extend(dict(plan_key=ref,of='OF264857',ov='OV2601327',cliente='TECPOLES',
                        perfil='L65X65X5',modelo=ref,comp_mm=1524,qtd_planeada=24,
                        qtd_restante=24,falta_valida=True,regra_calculo='calculated:qtd_minus_maq')
                   for ref in ['A18B108','A18B109'])
    historical=copy.deepcopy(entries)
    install(monkeypatch,historical,snapshot='snapshot-17sep')
    index=PlanIndex(entries,CANTONEIRAS_SPEC,snapshot_id='current')
    scorer=Scorer(index,CrossParams())
    monkeypatch.setattr(main,'get_index',lambda name: index)
    monkeypatch.setattr(main,'get_employees',lambda: {})
    monkeypatch.setattr(main,'_load_header_machines',lambda: [])
    class Provider:
        def extract(self,*a):return {'rows':paper[1:]}
    with db.connect() as conn:
        uid=db.create_sheet(conn,'cantoneiras_kanban',str(image),hashlib.sha256(image.read_bytes()).hexdigest())
        db.set_extraction(conn,uid,sample['raw_extraction'])
        first=db.get_sheet(conn,uid)
        data=sample['sheet_data']
        assert db.save_sheet_data_with_edits(conn,uid,data,first['revision'],[
            (f'header.{field}',None,value,'human','edit') for field,value in data['header'].items()])
        before=db.get_sheet(conn,uid)
        assert coverage_recovery.automatic(conn,uid,before['revision'],Provider)['added_rows']==1
        for _ in range(2):
            main.run_cross_check(conn,uid,engine_override=engine,scorer_override=scorer,historical_context_override=None)
            sheet=db.get_sheet(conn,uid)
            assert sheet['raw_extraction']==first['raw_extraction']
            assert len(sheet['sheet_data']['rows'])==7
            assert coverage_resolved(sheet['sheet_data'],sheet)
            for i,row in enumerate(data['rows']):
                actual=sheet['sheet_data']['rows'][i]
                for field in ('modelo','qtd','perfil'):
                    assert actual[field]==row[field]
            full=sheet['sheet_data']['rows'][6]
            assert full['_paper_position']==3 and full['of']=='264857'
            check=next(r for r in sheet['cross_check']['rows'] if r['row_index']==6)
            if engine == 'v3' and check.get('mode') == 'weak_guess':
                # Keep the existing engine's confidence guard: recovering the
                # paper is not permission to approve an uncertain plan match.
                assert check['full_profile_quantity'] is None
                assert check['plan_refs_valid'] is False
                continue
            assert check['full_profile_quantity']==48,(
                check.get('mode'), check.get('review_required'), check.get('p_correct'),
                check.get('quantity_basis',{}).get('diagnostic'))
            assert check['quantity_basis']['snapshot_id']=='snapshot-17sep'
            facts=production_facts.materialize_sheet(sheet,get_template('cantoneiras_kanban'))
            assert [r['row_index'] for r in facts['parents']]==[0,1,6,2,3,4,5]
            assert [(r['row']['modelo'],r['row']['qtd']) for r in facts['exports'] if r['row_index']==6]==[
                ('A18B108',24),('A18B109',24)]
            assert all(r['plan_snapshot_id']=='snapshot-17sep' for r in facts['plan_refs'])
            # A later plan with zero balance cannot erase the recovered output.
            for entry in entries:entry['qtd_restante']=0
        frozen=copy.deepcopy(sheet)
        assert coverage_recovery.automatic(conn,uid,sheet['revision'],Provider)['status']=='checked'
        assert db.get_sheet(conn,uid)==frozen
        conn.execute("UPDATE sheets SET status='validated' WHERE uid=?",(uid,));conn.commit()
        with pytest.raises(ValueError):coverage_recovery.automatic(conn,uid,sheet['revision'],Provider)
