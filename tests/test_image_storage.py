import hashlib
import io
from pathlib import Path
import pytest
from fastapi import FastAPI
from PIL import Image
from app import db, image_storage
from app.web import image_routes, main
from tests.live_client import LiveTestClient


def png(color='white'):
    out = io.BytesIO()
    Image.new('RGB', (20, 10), color).save(out, 'PNG')
    return out.getvalue()


@pytest.fixture
def storage(tmp_path, monkeypatch):
    from dataclasses import replace
    monkeypatch.setattr(image_storage, 'settings', replace(image_storage.settings, data_dir=tmp_path))
    (tmp_path / 'images').mkdir()
    return tmp_path


@pytest.mark.parametrize('stored', ['/home/old/app/data/images/scan.png', r'C:\old\app\data\images\scan.png', r'F:\old\data\images\scan.png'])
def test_migrated_basename_requires_exact_hash(storage, stored):
    target = storage / 'images/scan.png'
    target.write_bytes(png())
    sheet = {'image_path': stored, 'image_sha256': hashlib.sha256(png()).hexdigest()}
    assert image_storage.resolve(sheet) == target
    assert image_storage.for_processing(sheet) == target
    sheet['image_sha256'] = 'a' * 64
    assert image_storage.resolve(sheet) is None
    sheet['image_sha256'] = None
    assert image_storage.resolve(sheet) is None


def test_never_serves_outside_images_even_with_matching_hash(storage):
    outside = storage / 'secret.png'
    outside.write_bytes(png())
    sheet = {'image_path': str(outside), 'image_sha256': hashlib.sha256(png()).hexdigest()}
    assert image_storage.resolve(sheet) is None
    (storage / 'images/secret.png').symlink_to(outside)
    assert image_storage.resolve(sheet) is None


def test_content_change_invalidates_digest_cache(storage):
    path = storage / 'images/scan.png'
    path.write_bytes(png())
    sheet = {'image_path': str(path), 'image_sha256': hashlib.sha256(png()).hexdigest()}
    assert image_storage.resolve(sheet) == path
    path.write_bytes(png('black'))
    assert image_storage.resolve(sheet) is None


@pytest.fixture
def api(storage, monkeypatch):
    database = storage / 'app.db'
    def connect(): return db.connect(database)
    conn = connect()
    from app.templates_spec import TEMPLATES
    uid = db.create_sheet(conn, next(iter(TEMPLATES)), '/home/old/data/images/scan.png', hashlib.sha256(png()).hexdigest())
    conn.execute("UPDATE sheets SET status='validated', raw_extraction=?, sheet_data=?, cross_check=?, revision=9 WHERE uid=?",
                 ('{"rows":[{"qtd":42}]}', '{"header":{"operador":"Human"},"rows":[{"qtd":40}]}', '{}', uid))
    conn.commit()
    before = tuple(conn.execute('SELECT * FROM sheets WHERE uid=?', (uid,)).fetchone())
    conn.close()
    app = FastAPI()
    image_routes.register(app, connect)
    monkeypatch.setattr(main, '_conn', connect)
    app.get('/sheet/{uid}/photo')(main.sheet_photo)
    with LiveTestClient(app) as client:
        yield client, uid, connect, before


def test_restore_exact_original_preserves_validated_sheet_and_audits_once(api, storage):
    client, uid, connect, before = api
    assert client.get(f'/sheet/{uid}/photo/status').json()['available'] is False
    response = client.post(f'/sheet/{uid}/photo/restore', data={'revision':9}, files={'image':('ignored-name.png', png(), 'image/png')})
    assert response.status_code == 200 and response.json()['status'] == 'restored'
    assert (storage/'images/scan.png').read_bytes() == png()
    assert client.get(f'/sheet/{uid}/photo/status').json()['available'] is True
    assert str(main.sheet_photo(uid, original=1).path) == str(storage/'images/scan.png')
    again = client.post(f'/sheet/{uid}/photo/restore', data={'revision':9}, files={'image':('scan.png', png(), 'image/png')})
    assert again.json()['status'] == 'already_available'
    conn = connect()
    assert tuple(conn.execute('SELECT * FROM sheets WHERE uid=?', (uid,)).fetchone()) == before
    events = conn.execute('SELECT field_path,source FROM edits WHERE sheet_uid=?', (uid,)).fetchall()
    assert [tuple(e) for e in events] == [('image.original_restored','system')]
    conn.close()


@pytest.mark.parametrize('revision,content,code', [(8,png(),409),(9,png('black'),422)])
def test_restore_refuses_stale_revision_or_wrong_original(api, storage, revision, content, code):
    client,uid,connect,before=api
    response=client.post(f'/sheet/{uid}/photo/restore',data={'revision':revision},files={'image':('scan.png',content,'image/png')})
    assert response.status_code==code
    assert not list((storage/'images').iterdir())


def test_restore_preserves_conflicting_file(api, storage):
    client,uid,connect,before=api
    path=storage/'images/scan.png';path.write_bytes(png('black'))
    response=client.post(f'/sheet/{uid}/photo/restore',data={'revision':9},files={'image':('scan.png',png(),'image/png')})
    assert response.status_code==409
    assert path.read_bytes()==png('black')


def test_photo_route_resolves_legacy_path_without_writing_database(api, storage):
    client,uid,connect,before=api
    (storage/'images/scan.png').write_bytes(png())
    assert str(main.sheet_photo(uid, original=1).path)==str(storage/'images/scan.png')
    conn=connect()
    assert tuple(conn.execute('SELECT * FROM sheets WHERE uid=?',(uid,)).fetchone())==before
    assert conn.execute('SELECT count(*) FROM edits').fetchone()[0]==0
    conn.close()


def test_restore_rejects_invalid_image_even_if_digest_matches(api, storage):
    client,uid,connect,before=api
    conn=connect();conn.execute('UPDATE sheets SET image_sha256=? WHERE uid=?',(hashlib.sha256(b'not an image').hexdigest(),uid));conn.commit();conn.close()
    response=client.post(f'/sheet/{uid}/photo/restore',data={'revision':9},files={'image':('scan.png',b'not an image','image/png')})
    assert response.status_code==422
    assert not list((storage/'images').iterdir())


def test_recovery_client_simulates_then_restores_and_verifies(api, storage):
    from scripts.recover_original_images import recover_one
    client, uid, connect, before = api
    source = storage / 'backup.png'
    source.write_bytes(png())
    row = (uid, str(source), hashlib.sha256(png()).hexdigest())
    assert recover_one(client, row)['status'] == 'would_restore'
    assert not (storage / 'images/scan.png').exists()
    assert recover_one(client, row, apply=True)['status'] == 'restored'
    assert recover_one(client, row, apply=True)['status'] == 'already_available'


def test_failed_audit_does_not_leave_unaudited_restoration(api, storage):
    client, uid, connect, before = api
    conn = connect()
    conn.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON edits BEGIN SELECT RAISE(ABORT, 'test audit failure'); END")
    conn.commit(); conn.close()
    response = client.post(f'/sheet/{uid}/photo/restore', data={'revision':9}, files={'image':('scan.png',png(),'image/png')})
    assert response.status_code == 500
    assert not list((storage/'images').iterdir())
