"""Exercise recovery in Chromium against disposable SQLite and fake references/OCR."""
import argparse
import copy
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--playwright', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='kanban-recovery-browser-') as directory:
        temp = Path(directory)
        images = temp/'images'
        images.mkdir()
        os.environ['MES_DATA_DIR'] = str(temp)
        os.environ['MES_CROSS_ENGINE'] = 'legacy'
        from app import db, pg_store
        from app.matching import loaders
        from app.matching.refs import PlanIndex
        from app.web import main as web
        from tests.live_client import LiveTestClient
        perfis = web.settings.port == 8101
        spec = loaders.PERFIS_SPEC if perfis else loaders.CANTONEIRAS_SPEC
        index = PlanIndex([], spec, snapshot_id='browser')
        web.get_index = lambda *a, **kw: index
        web.get_employees = lambda: {}
        web._load_header_machines = lambda: []
        loaders.load_active_ofs = lambda: set()
        loaders.plan_snapshot_info = lambda: {'snapshot_id':'browser', 'age_hours':1}
        pg_store.store_validated_sheet = lambda *a, **kw: (_ for _ in ()).throw(AssertionError('Unexpected validation'))
        with db.connect() as conn:
            if perfis:
                from tests.test_coverage_review import create
                uid = create(conn)
                image = images/'perfis112.png'
                shutil.copyfile(db.get_sheet(conn,uid)['image_path'],image)
                conn.execute('UPDATE sheets SET image_path=? WHERE uid=?',(str(image),uid));conn.commit()
                sheet = db.get_sheet(conn, uid)
                data = copy.deepcopy(sheet['sheet_data'])
                for i in (0, 2):
                    data['rows'][i]['_deleted'] = True
                db.save_sheet_data_with_edits(conn, uid, data, sheet['revision'], [])
            else:
                from tests.test_header_recovery import create, Provider, EMPLOYEES, MACHINES
                uid = create(conn, images)
                web.get_provider = lambda: Provider()
                web.get_employees = lambda: EMPLOYEES
                web._load_header_machines = lambda: MACHINES
            before = db.get_sheet(conn, uid)
        with LiveTestClient(web.app) as client:
            config = {'base':str(client._client.base_url).rstrip('/'), 'uid':uid,
                      'kind':'perfis' if perfis else 'cantoneiras',
                      'output':str(args.output.resolve()), 'playwright':args.playwright}
            path = temp/'browser.json'
            path.write_text(json.dumps(config))
            subprocess.run(['node', str(Path(__file__).with_suffix('.cjs')), str(path)], check=True)
        with db.connect() as conn:
            after = db.get_sheet(conn, uid)
            assert after['raw_extraction'] == before['raw_extraction']
            assert after['sheet_data']['footer'] == before['sheet_data']['footer']
            assert len(after['sheet_data']['rows']) == len(before['sheet_data']['rows'])
            if perfis:
                assert after['sheet_data']['rows'][0]['_deleted'] is False
                assert after['sheet_data']['rows'][2]['_exclusion']['reason'] == 'out_of_scope'
            else:
                assert after['sheet_data']['rows'] == before['sheet_data']['rows']
                from app.matching.header_cross import canonical_date
                assert canonical_date(after['sheet_data']['header']['data']) == '21/08/2026'
            assert after['status'] == 'in_review'


if __name__ == '__main__':
    main()
