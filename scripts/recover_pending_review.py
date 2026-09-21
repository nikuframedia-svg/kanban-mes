"""Serial HTTP recovery. Dry run by default; journal every completed sheet.

Never validates sheets. Uses the same revision-guarded services as the browser.
"""
import argparse
import hashlib
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path


def request(base, path, data=None):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    with urllib.request.urlopen(base.rstrip('/') + path, body, timeout=30) as response:
        content = response.read()
        return json.loads(content) if response.headers.get_content_type() == 'application/json' else {}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def run(base, report, *, apply=False, uids=(), restore=(), limit=20):
    inventory = request(base, '/recovery/inventory')['sheets']
    selected = [item for item in inventory if item['uid'] in uids] if uids else [
        item for item in inventory if item['automatic_needed']]
    if uids and set(uids) - {s['uid'] for s in selected}:
        raise ValueError('Uma folha pedida não está pendente. O lote não começou.')
    if uids:
        selected.sort(key=lambda item: list(uids).index(item['uid']))
    report = Path(report)
    report.parent.mkdir(parents=True, exist_ok=True)
    inventory_path = report.with_suffix('.inventory.json')
    inventory_path.write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding='utf-8')
    summary = {'mode': 'apply' if apply else 'simulation', 'candidates': len(selected),
               'recovered': 0, 'review': 0, 'conflicts_or_failures': 0}
    with report.open('a', encoding='utf-8') as journal:
        for item in selected[:limit]:
            record = {'uid': item['uid'], 'sheet_no': item['sheet_no'], 'before_revision': item['revision'],
                      'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'mode': summary['mode']}
            try:
                if not apply:
                    record['status'] = 'candidate'
                else:
                    uid = item['uid']
                    before = request(base, f'/sheet/{uid}/recovery-state')
                    if before['revision'] != item['revision'] or before['status'] not in {'extracted','in_review'}:
                        raise ValueError('A revisão ou o estado mudou desde o inventário.')
                    original_hash = digest(before['raw_extraction'])
                    for target in restore:
                        target_uid, index = target.split(':')
                        if target_uid != uid:
                            continue
                        index = int(index)
                        row = before['sheet_data']['rows'][index]
                        record['restored_line_audit'] = request(base, f'/sheet/{uid}/rows/{index}/audit')
                        if row.get('_deleted') is True:
                            request(base, f'/sheet/{uid}/rows/{index}/restore', {'revision': before['revision']})
                            before = request(base, f'/sheet/{uid}/recovery-state')
                            if before['sheet_data']['rows'][index].get('_deleted') is True:
                                raise ValueError('O restauro não foi confirmado.')
                    raw_hash = original_hash
                    job = request(base, f'/sheet/{uid}/automatic-review', {'revision': before['revision']})
                    deadline = time.monotonic() + 900
                    while job['status'] in {'queued','running'}:
                        if time.monotonic() > deadline:
                            raise TimeoutError('A recuperação continua em curso; o lote foi interrompido.')
                        time.sleep(2)
                        job = request(base, f'/sheet/{uid}/automatic-review')
                    if job['status'] == 'error':
                        raise ValueError(job.get('error'))
                    after = request(base, f'/sheet/{uid}/recovery-state')
                    if digest(after['raw_extraction']) != raw_hash or after['status'] == 'validated':
                        raise ValueError('Falhou a verificação de integridade; interromper o lote.')
                    record.update(after_revision=after['revision'], job=job,
                                  raw_sha256=raw_hash, after_sha256=digest(after))
                    data = after['sheet_data']
                    recovery = data.get('_coverage_recovery') or {}
                    if recovery.get('status') == 'failed':
                        raise ValueError('OCR indisponível; tentativa auditada. O lote foi interrompido.')
                    coverage = data.get('_ocr_coverage') or {}
                    active = sum(row.get('_deleted') is not True and any(v is not None and str(v).strip() for k,v in row.items() if not k.startswith('_')) for row in data.get('rows', []))
                    outside = sum(row.get('_deleted') is True and (row.get('_exclusion') or {}).get('reason') == 'out_of_scope' for row in data.get('rows', []))
                    doubt = recovery.get('status') == 'review' or coverage.get('expected_rows') != active + outside
                    doubt |= any(r.get('_identity_unresolved') or
                                 (r.get('_deleted') is True and not r.get('_exclusion')) for r in data.get('rows', []))
                    doubt |= any((r.get('quantity_basis') or {}).get('status') == 'unavailable'
                                 for r in (after.get('cross_check') or {}).get('rows', []))
                    record['status'] = 'review' if doubt else 'recovered'
                    summary[record['status']] += 1
            except Exception as exc:
                record.update(status='stopped', error=str(exc))
                summary['conflicts_or_failures'] += 1
            journal.write(json.dumps(record, ensure_ascii=False) + '\n')
            journal.flush()
            if record['status'] == 'stopped':
                break
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--uid', action='append', default=[])
    parser.add_argument('--restore', action='append', default=[], help='Explicit uid:original_zero_based_index')
    parser.add_argument('--limit', type=int, default=20)
    args = parser.parse_args()
    print(json.dumps(run(args.base_url, args.report, apply=args.apply,
        uids=args.uid, restore=args.restore, limit=args.limit), ensure_ascii=False, indent=2))
