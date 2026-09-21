import copy
from pathlib import Path
import numpy as np
import pytest
from PIL import Image
from app.ocr.coverage import check_coverage, table_rows
from app.row_recovery import recover_missing
from app.templates_spec import get_template

FIXTURES = Path(__file__).parent / 'fixtures/physical_rows'


@pytest.mark.parametrize('number,expected', [(661,15),(681,5),(677,3),(686,7)])
@pytest.mark.parametrize('angle', [0,-2,2])
def test_real_tpl102_grid(number, expected, angle, tmp_path):
    image = FIXTURES/f'{number}.png'
    if angle:
        with Image.open(image) as source:
            array = np.asarray(source.convert('RGB'))
            noisy = np.clip(array.astype(float)+np.random.default_rng(17).normal(0,3,array.shape),0,255).astype('uint8')
            image = tmp_path/'variant.png'
            Image.fromarray(noisy).rotate(angle,resample=Image.Resampling.BICUBIC,fillcolor='white').save(image)
    coverage = check_coverage(image, {})
    assert coverage['expected_rows'] == expected, coverage


def test_unreliable_grid_never_asserts_count(tmp_path):
    image = tmp_path/'blank.png';Image.new('RGB',(1200,800),'white').save(image)
    coverage = check_coverage(image,{})
    assert coverage['expected_rows'] is None and coverage['status'] == 'unverified'


def specimen():
    models = ['H92HS4000AT','H92HS400P4T','H92HS2100AT','H92HS200P2T','H92HS2000AT',
              'H92HS1003AT','H92HS1002AT','H92HS2014AT','H92HS206P2AT','H92HS210P2T',
              'H92HS3009AT','H92HS4008AT','H92HS408P4AT','H92HS414P4T','H92HS500BT']
    quantities = [4,4,8,4,8,4,4,8,4,4,8,4,4,4,2]
    rows = [{'of':'264976','perfil':'L120X120X12','modelo':model,'qtd':qty} for model,qty in zip(models,quantities)]
    original = copy.deepcopy(rows[:11]+rows[12:])
    original[2]['modelo'] = 'H92HS8100AT'
    original[11]['modelo'] = 'H92HS408P4T'
    return rows, {'raw_extraction':{'rows':original}, 'sheet_data':{'rows':copy.deepcopy(rows[:11]+rows[12:])}}


def test_661_local_strip_adds_only_missing_row_keeps_corrections_and_ids():
    rows,sheet = specimen();before = copy.deepcopy(sheet)
    class Provider:
        calls = 0
        def extract(self, path, template):
            self.calls += 1
            with Image.open(path) as crop: assert crop.height < Image.open(FIXTURES/'661.png').height
            return {'rows':rows[9:]}
    provider=Provider()
    additions,positions = recover_missing(provider,FIXTURES/'661.png',get_template('cantoneiras_kanban'),sheet,table_rows(FIXTURES/'661.png'))
    assert provider.calls == 1
    assert list(additions) == ['14']
    assert additions['14']['modelo'] == 'H92HS4008AT' and additions['14']['qtd'] == 4
    assert additions['14']['_paper_position'] == 12
    assert positions[10] == 11 and positions[11] == 13
    assert sheet == before


def test_ambiguous_strip_aborts():
    rows,sheet=specimen()
    class Provider:
        def extract(self,*a): return {'rows':rows[9:-1]}
    with pytest.raises(ValueError):
        recover_missing(Provider(),FIXTURES/'661.png',get_template('cantoneiras_kanban'),sheet,table_rows(FIXTURES/'661.png'))


def specimen_686():
    partial = [dict(of='264857', cliente='TECPOLES', ov='2601327',
                    perfil='L55X55X4' if i < 2 else 'L45X45X4', modelo=ref, qtd=str(qty))
               for i, (ref, qty) in enumerate([('EA18F72',20), ('A18F31',16),
                   ('A18F29',16), ('A18F28',16), ('A18F18',8), ('A18C124',8)])]
    raw = copy.deepcopy(partial)
    raw[0]['modelo'] = 'EA18F T2'
    raw[1]['perfil'] = 'L65x5'  # The OCR fused two separate paper rows.
    header = {'data':'18/09/2026','operador':'ARSHDEEP DHINDSA', 'n_operador':'2849',
              'setor_maquina':'Peddi 8'}
    sheet = {'raw_extraction':{'header':{},'rows':raw,'footer':{}},
             'sheet_data':{'header':header,'rows':partial,'footer':{}}}
    paper = copy.deepcopy(partial)
    paper[1]['perfil'] = None  # Empty on paper; the profile was written on row 1.
    paper.insert(2, {'of':'"','perfil':'L65x5','modelo':None,'qtd':None,'perf_comp':'X'})
    return paper, sheet


def test_686_recovers_full_profile_as_separate_row_preserving_all_existing_values():
    paper, sheet = specimen_686(); before = copy.deepcopy(sheet)
    class Provider:
        def extract(self,*a): return {'rows':paper[1:]}
    anchors = {}
    additions,positions = recover_missing(Provider(),FIXTURES/'686.png',
        get_template('cantoneiras_kanban'),sheet,table_rows(FIXTURES/'686.png'),anchors)
    assert list(additions) == ['6']
    full = additions['6']
    assert full['of'] == '264857' and full['perfil'] == 'L65x5'
    assert full['perf_comp'] == 'X' and full['modelo'] is None and full['qtd'] is None
    assert full['_paper_position'] == 3
    assert positions == {0:1,1:2,2:4,3:5,4:6,5:7}
    assert anchors == {'1': {'perfil': None}}
    assert sheet == before


@pytest.mark.parametrize('changes', [dict(perfil=None),dict(perf_comp=None),
    dict(modelo='A18F31'),dict(qtd=16)])
def test_686_ambiguous_full_profile_never_invents_a_row(changes):
    paper,sheet=specimen_686()
    paper[2].update(changes)
    class Provider:
        def extract(self,*a):return {'rows':paper[1:]}
    with pytest.raises(ValueError,match='ambíguo'):
        recover_missing(Provider(),FIXTURES/'686.png',get_template('cantoneiras_kanban'),
                        sheet,table_rows(FIXTURES/'686.png'))


def test_existing_full_profile_can_anchor_recovery_but_repeated_profile_is_ambiguous():
    from app.row_recovery import align_strip
    full={'of':'264857','perfil':'L65X65X5','perf_comp':'X'}
    last={'modelo':'AF1','qtd':'4'}
    missing={'modelo':'AF0','qtd':'2'}
    added=align_strip([full,missing,last],[1,2,3],[full,last],[full,last])
    assert added[0][1]['modelo']=='AF0'
    with pytest.raises(ValueError,match='repetidas'):
        align_strip([full,missing,last],[1,2,3],[full,full,last],[full,full,last])


def test_scanner_specks_do_not_fill_blank_rows(tmp_path):
    with Image.open(FIXTURES/'686.png') as source:
        array=np.asarray(source.convert('RGB')).copy()
    random=np.random.default_rng(23)
    array[random.random(array.shape[:2]) < .0005] = 0
    image=tmp_path/'specks.png';Image.fromarray(array).save(image)
    assert check_coverage(image,{})['expected_rows']==7


def test_split_profile_evidence_preserves_raw_and_human_decisions():
    from app.matching.evidence import build_evidence
    from app.ocr.coverage import sheet_identity
    paper,sheet=specimen_686();before=copy.deepcopy(sheet['raw_extraction'])
    class Provider:
        def extract(self,*a):return {'rows':paper[1:]}
    anchors={}
    additions,positions=recover_missing(Provider(),FIXTURES/'686.png',
        get_template('cantoneiras_kanban'),sheet,table_rows(FIXTURES/'686.png'),anchors)
    sheet['sheet_data']['rows'].append(additions['6'])
    sheet['sheet_data']['_coverage_recovery']={'context':sheet_identity(sheet),
        'observations':additions,'anchor_observations':anchors}
    evidence=build_evidence(sheet,[])
    assert evidence.data['rows'][1]['perfil'] is None
    assert evidence.data['rows'][1]['modelo']=='A18F31'
    assert evidence.provenance['field_sources']['rows[1].perfil']['source']=='row_recovery'
    human=[{'id':1,'source':'human','field_path':'rows[1].perfil','new_value':'L70X70X7'}]
    assert build_evidence(sheet,human).data['rows'][1]['perfil']=='L70X70X7'
    sheet['extraction_generation']=2
    assert build_evidence(sheet,[]).data['rows'][1]['perfil']=='L65x5'
    assert sheet['raw_extraction']==before
