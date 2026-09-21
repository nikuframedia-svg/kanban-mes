"""Restore only the exact original identified in SQLite; never rerun OCR."""
import hashlib
import io
import json
import os
import tempfile
from fastapi import Form, HTTPException, UploadFile
from PIL import Image
from .. import db, image_storage

MAX_IMAGE_BYTES = 30 * 1024 * 1024


def register(app, connect):
    @app.get('/sheet/{uid}/photo/status')
    def status(uid: str):
        conn = connect()
        try:
            sheet = db.get_sheet(conn, uid)
            if not sheet:
                raise HTTPException(404)
            available = image_storage.resolve(sheet) is not None
            return {'available': available, 'revision': sheet['revision'], 'sheet_no': sheet['sheet_no'],
                    'sha256': image_storage.expected_hash(sheet),
                    'filename': image_storage.basename(sheet),
                    'can_restore': bool(image_storage.expected_hash(sheet) and image_storage.destination(sheet))}
        finally:
            conn.close()

    @app.post('/sheet/{uid}/photo/restore')
    async def restore(uid: str, image: UploadFile, revision: int = Form(...)):
        content = await image.read(MAX_IMAGE_BYTES + 1)
        if len(content) > MAX_IMAGE_BYTES:
            raise HTTPException(413, 'Imagem demasiado grande.')
        sha = hashlib.sha256(content).hexdigest()
        conn = connect()
        tmp = None
        try:
            conn.execute('BEGIN IMMEDIATE')
            sheet = db.get_sheet(conn, uid)
            if not sheet:
                raise HTTPException(404)
            if sheet['revision'] != revision:
                raise HTTPException(409, 'A folha mudou; voltar a verificar antes de recuperar a imagem.')
            if sha != image_storage.expected_hash(sheet):
                raise HTTPException(422, 'A imagem não corresponde ao original desta folha.')
            target = image_storage.destination(sheet)
            if target is None:
                raise HTTPException(422, 'O registo não identifica um ficheiro original seguro.')
            if image_storage.resolve(sheet) is not None:
                return {'status': 'already_available', 'sha256': sha}
            try:
                with Image.open(io.BytesIO(content)) as probe:
                    probe.verify()
            except Exception:
                raise HTTPException(422, 'O original não é uma imagem legível.')
            target.parent.mkdir(parents=True, exist_ok=True)
            root = image_storage.settings.images_dir.resolve()
            if not target.resolve().is_relative_to(root) or target.is_symlink() or target.exists():
                raise HTTPException(409, 'Existe um ficheiro diferente neste destino; preservado para diagnóstico.')
            fd, tmp = tempfile.mkstemp(prefix='.restore-', dir=root)
            with os.fdopen(fd, 'wb') as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            # Atomic and exclusive on NTFS/Linux: never overwrite another file.
            try:
                os.link(tmp, target)
            except FileExistsError:
                raise HTTPException(409, 'O destino mudou durante a recuperação.')
            conn.execute(
                'INSERT INTO edits (sheet_uid,field_path,old_value,new_value,source,actor,edited_at) VALUES (?,?,?,?,?,?,?)',
                (uid, 'image.original_restored', None, json.dumps({'sha256': sha, 'filename': target.name}),
                 'system', 'recovery:original-image', db.now_iso()))
            conn.commit()
            return {'status': 'restored', 'sha256': sha}
        finally:
            if conn.in_transaction:
                conn.rollback()
            conn.close()
            if tmp:
                os.unlink(tmp)
