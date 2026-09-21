import importlib.util
import json
from pathlib import Path

spec=importlib.util.spec_from_file_location('batch',Path(__file__).parents[1]/'scripts/recover_pending_review.py')
batch=importlib.util.module_from_spec(spec);spec.loader.exec_module(batch)


def test_dry_run_does_not_write_and_failure_stops_batch(tmp_path,monkeypatch):
    calls=[]
    def request(base,path,data=None):
        calls.append((path,data))
        if path=='/recovery/inventory':
            return {'sheets':[{'uid':uid,'sheet_no':i,'revision':1,'automatic_needed':True} for i,uid in enumerate(('one','two','three'))]}
        if path.endswith('/recovery-state'):
            return {'uid':'one','revision':1,'status':'in_review','raw_extraction':{'rows':[]}}
        if path.endswith('/automatic-review'):
            return {'status':'error','error':'OCR unavailable'}
        raise AssertionError(path)
    monkeypatch.setattr(batch,'request',request)
    report=tmp_path/'report.jsonl'
    result=batch.run('http://test',report)
    assert result['mode']=='simulation' and all(data is None for _,data in calls)
    assert len(report.read_text().splitlines())==3
    calls.clear()
    result=batch.run('http://test',report,apply=True)
    assert result['conflicts_or_failures']==1
    assert not any('/two/' in path or '/three/' in path for path,_ in calls)
    assert json.loads(report.read_text().splitlines()[-1])['status']=='stopped'


def test_inventory_conflict_prevents_write(tmp_path,monkeypatch):
    def request(base,path,data=None):
        assert data is None
        if path=='/recovery/inventory':return {'sheets':[{'uid':'one','sheet_no':1,'revision':1,'automatic_needed':True}]}
        return {'uid':'one','revision':2,'status':'in_review'}
    monkeypatch.setattr(batch,'request',request)
    result=batch.run('http://test',tmp_path/'report.jsonl',apply=True)
    assert result['conflicts_or_failures']==1
