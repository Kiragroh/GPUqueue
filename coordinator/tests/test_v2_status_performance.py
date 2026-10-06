import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import store

def fixture(tmp_path):
    s=store.Store(tmp_path/'q.db')
    for i in range(12):
        id=s.submit('command','test','synthetic','',b'large encrypted payload',context={'script_name':f'test{i}.py','case_id':'SYNTHETIC_PRIVATE'})
        s.finish(id,'completed')
    return s

def test_listing_batches_metadata_without_reading_request_payloads(tmp_path):
    s=fixture(tmp_path);queries=[];s.db.set_trace_callback(queries.append)
    result=s.listing(private=True)
    selects=[q.lower() for q in queries if q.lower().startswith('select')]
    assert len(result)==12
    assert len(selects)<=5
    assert all('select *' not in q and 'payload' not in q for q in selects)
    assert all(r['context']['case_id']=='SYNTHETIC_PRIVATE' and len(r['events'])==2 for r in result)

def test_repeated_public_private_listing_reuses_bounded_context_cache(tmp_path,monkeypatch):
    s=fixture(tmp_path);s.db.close();s=store.Store(tmp_path/'q.db')
    original=store.unprotect;calls=[]
    def counting(value):calls.append(value);return original(value)
    monkeypatch.setattr(store,'unprotect',counting)
    first=s.listing();assert len(calls)==12
    s.listing(private=True);public=s.listing()
    assert len(calls)==12
    assert 'SYNTHETIC_PRIVATE' not in str(public)
    assert [r['script'] for r in public]==[r['script'] for r in first]

def test_single_status_and_scheduler_metadata_exclude_large_payload(tmp_path):
    s=fixture(tmp_path);id=s.listing()[0]['id'];queries=[];s.db.set_trace_callback(queries.append)
    s.status(id);s.active_rows()
    assert all('select *' not in q.lower() and 'payload' not in q.lower() for q in queries if q.lower().startswith('select'))


def test_history_does_not_acquire_new_wait_bonus_or_current_blockers(tmp_path):
    import time
    s=store.Store(tmp_path/'history.db')
    old=s.submit('command','old','test','',b'{}',priority=0)
    now=time.time()
    with s.db:s.db.execute('update jobs set created=?,started=? where id=?',(now-4000,now-3999,old))
    s.finish(old,'completed')
    current=s.submit('command','current','test','',b'{}')
    s.claim()
    history=s.status(old)
    assert history['effective_priority']==0
    assert history['waiting_for']==[]
    assert s.status(current)['waiting_for']==[]
