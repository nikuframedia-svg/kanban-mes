import copy
from pathlib import Path
import numpy as np
import pytest
from PIL import Image
from app.ocr.coverage import check_coverage, table_rows
from app.row_recovery import recover_missing
from app.templates_spec import get_template

FIXTURES = Path(__file__).parent / 'fixtures/physical_rows'


@pytest.mark.parametrize('number,expected', [(661,15),(681,5),(677,3)])
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
