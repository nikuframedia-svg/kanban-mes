"""Repor originais exatos por HTTP; simulação por defeito, sem reprocessar folhas."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import httpx


def recover_one(client, row, apply=False):
    uid, image_path, sha = row
    response = client.get(f'/sheet/{uid}/photo/status')
    if response.status_code == 404:
        return {'uid': uid, 'status': 'unavailable_endpoint_or_sheet'}
    response.raise_for_status()
    remote = response.json()
    result = {'uid': uid, 'sheet_no': remote.get('sheet_no')}
    if remote['available']:
        return result | {'status': 'already_available'}
    if not sha or remote['sha256'] != sha:
        return result | {'status': 'identity_conflict'}
    path = Path(image_path)
    if not path.is_file():
        return result | {'status': 'local_original_missing'}
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != sha:
        return result | {'status': 'local_hash_mismatch'}
    if not apply:
        return result | {'status': 'would_restore', 'sha256': sha}
    response = client.post(f'/sheet/{uid}/photo/restore',
                          data={'revision': remote['revision']},
                          files={'image': (path.name, content, 'application/octet-stream')})
    response.raise_for_status()
    verification = client.get(f'/sheet/{uid}/photo?original=1')
    verification.raise_for_status()
    if hashlib.sha256(verification.content).hexdigest() != sha:
        raise RuntimeError('O original servido não corresponde à imagem reposta.')
    return result | response.json()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-db', type=Path, required=True)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--uid', action='append')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    with sqlite3.connect(args.source_db.resolve().as_uri()+'?mode=ro', uri=True) as conn:
        rows = conn.execute('SELECT uid,image_path,image_sha256 FROM sheets WHERE image_path IS NOT NULL').fetchall()
    if args.uid:
        rows = [r for r in rows if r[0] in args.uid]
    results = []
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(base_url=args.base_url.rstrip('/'), timeout=60) as client:
        for row in rows:
            try:
                result = recover_one(client, row, args.apply)
            except Exception as exc:
                result = {'uid': row[0], 'status': 'failed', 'error': str(exc)}
            results.append(result)
            args.report.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if result['status'] in {'failed', 'identity_conflict', 'local_hash_mismatch', 'unavailable_endpoint_or_sheet'}:
                raise SystemExit('Recuperação interrompida; consultar o relatório. A repetição preserva o progresso.')


if __name__ == '__main__':
    main()
