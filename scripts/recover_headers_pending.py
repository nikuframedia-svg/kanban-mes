"""Inventory/recover missing headers; one worker, original OCR and rows preserved.

Default is read-only. Use --apply --backup NEW_PATH to read and fill eligible
headers. Run the incident, a pilot of 10, then batches of 20. Repeated runs skip
completed attempts; failures stop the batch and remain visible/retryable.
"""
from contextlib import closing
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_environment():
    path = ROOT/'.env'
    if path.is_file():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                key,value=line.split('=',1)
                os.environ.setdefault(key.strip(),value.strip().strip('"').strip("'"))


def inventory(conn, uids=None):
    from app import db, header_recovery
    result=[]
    for row in conn.execute("SELECT uid FROM sheets WHERE status IN ('extracted','in_review') AND image_path IS NOT NULL ORDER BY CASE WHEN uid='745a6fadc3f4' THEN 0 ELSE 1 END,created_at,uid"):
        sheet=db.get_sheet(conn,row['uid'])
        if uids and sheet['uid'] not in uids:
            continue
        data=sheet.get('sheet_data') or {}
        if data.get('_blank_page') or (sheet.get('raw_extraction') or {}).get('_blank_page'):
            continue
        protected=db.human_header_fields(conn,sheet['uid'])
        missing=[f for f in header_recovery.FIELDS if not str((data.get('header') or {}).get(f) or '').strip()]
        previous=header_recovery.current_recovery(sheet)
        if not missing or not (set(missing)-protected):
            continue
        if previous.get('version')==header_recovery.VERSION and previous.get('status') not in {None,'failed'}:
            continue
        result.append({'uid':sheet['uid'],'sheet_no':sheet['sheet_no'],'revision':sheet['revision'],
                       'status':sheet['status'],'missing':missing,'human_fields':sorted(protected),
                       'context':header_recovery.identity(sheet),
                       'human_header_decisions':[e for e in db.evidence_edits(conn,sheet) if e['field_path'].startswith('header.')],
                       'excluded_rows':[{'original_index':i+1,'row':row} for i,row in enumerate(data.get('rows') or []) if row.get('_deleted') is True]})
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,default=ROOT/'data/app.db')
    parser.add_argument('--uid',action='append')
    parser.add_argument('--limit',type=int,default=20)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--backup',type=Path)
    args=parser.parse_args()
    load_environment()
    from app import db, header_recovery
    conn=sqlite3.connect(args.database.resolve().as_uri()+'?mode=ro',uri=True)
    conn.row_factory=sqlite3.Row
    try:
        candidates=inventory(conn,args.uid)
        selected=candidates[:max(0,args.limit)]
        if args.apply:
            if not args.backup or args.backup.exists():
                raise SystemExit('Indica --backup com um caminho novo antes de aplicar.')
            if conn.execute("SELECT count(*) FROM sheets WHERE status='pending'").fetchone()[0]:
                raise SystemExit('Existe OCR em curso; aguarda antes de recuperar cabeçalhos.')
            args.backup.parent.mkdir(parents=True,exist_ok=True)
            with sqlite3.connect(args.backup) as backup:
                conn.backup(backup)
                if backup.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                    raise SystemExit('Cópia de segurança inválida.')
    finally:
        conn.close()
    results=[]
    if args.apply and selected:
        from app.web.main import get_provider,get_employees,_load_header_machines,_assumed_sheet_date
        provider=get_provider()
        try:
            employees=get_employees()
        except Exception:
            employees={}
        machines=_load_header_machines()
        for item in selected:
            with closing(db.connect(args.database)) as conn:
                try:
                    sheet=db.get_sheet(conn,item['uid'])
                    result=header_recovery.recover(conn,item['uid'],item['revision'],provider,employees,machines,_assumed_sheet_date(sheet))
                except Exception as exc:
                    result={'uid':item['uid'],'status':'conflict' if isinstance(exc,ValueError) else 'failed','error':str(exc)}
                results.append(result)
                print(json.dumps({'progress':result},ensure_ascii=False),file=sys.stderr,flush=True)
                if result['status'] in {'failed','conflict'}:
                    from collections import Counter
                    print(json.dumps({'mode':'apply','stopped':True,'candidates':len(candidates),'selected':selected,'results':results,'summary':dict(Counter(r['status'] for r in results))},ensure_ascii=False,indent=2))
                    return 1
    from collections import Counter
    print(json.dumps({'mode':'apply' if args.apply else 'inventory','candidates':len(candidates),
                      'selected':selected,'results':results,'summary':dict(Counter(r['status'] for r in results))},ensure_ascii=False,indent=2))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
