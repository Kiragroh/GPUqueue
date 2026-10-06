import importlib.util
from pathlib import Path
import sys
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

def store(tmp_path):
    assert (ROOT/'store.py').exists(), 'Durable store not implemented'
    from store import Store
    return Store(tmp_path/'jobs.sqlite3')

def test_fifo_and_persistence(tmp_path):
    s=store(tmp_path)
    a=s.submit('ollama','one','model','/api/generate',b'{"prompt":"secret"}')
    b=s.submit('ollama','two','model','/api/generate',b'{}')
    assert s.status(b)['jobs_ahead']==1
    assert s.claim()['id']==a
    from store import Store
    assert Store(tmp_path/'jobs.sqlite3').status(a)['state']=='running'
    assert b'secret' not in (tmp_path/'jobs.sqlite3').read_bytes()

def test_idempotency_checks_payload(tmp_path):
    s=store(tmp_path)
    a=s.submit('ollama','one','model','/api/generate',b'{}',key='key')
    assert s.submit('ollama','one','model','/api/generate',b'{}',key='key')==a
    with pytest.raises(ValueError):s.submit('ollama','one','model','/api/generate',b'changed',key='key')

def test_cancel_is_explicit_and_result_retained(tmp_path):
    s=store(tmp_path)
    a=s.submit('ollama','one','model','/api/generate',b'{}')
    s.claim();s.chunk(a,b'result');s.finish(a,'completed')
    assert b''.join(s.chunks(a))==b'result'
    with pytest.raises(ValueError):s.cancel(a)

def test_restart_does_not_replay_running_ollama(tmp_path):
    s=store(tmp_path)
    a=s.submit('ollama','one','model','/api/generate',b'{}');s.claim()
    s.recover()
    assert s.status(a)['state']=='recovery_blocked'
    assert s.claim() is None

def test_release_refuses_live_child(tmp_path):
    s=store(tmp_path)
    a=s.submit('command','one','model','',b'{}');s.claim()
    with pytest.raises(ValueError):s.release(a,0,child_alive=True)
    assert s.status(a)['state']=='running'

def test_case_context_is_encrypted_and_hidden_by_default(tmp_path):
    s=store(tmp_path)
    a=s.submit('command','test','model','',b'{}',context={'script':'analysis.py','case_id':'000SYNTHETIC'})
    assert 'case_id' not in str(s.status(a))
    assert s.status(a,private=True)['context']['case_id']=='000SYNTHETIC'
    assert b'SYNTHETIC' not in (tmp_path/'jobs.sqlite3').read_bytes()

def test_preparing_command_cannot_be_released(tmp_path):
    s=store(tmp_path);a=s.submit('command','one','model','',b'{}');s.claim();s.update(a,state='preparing')
    with pytest.raises(ValueError):s.release(a,0,child_alive=False)

def test_restart_preparing_command_returns_to_queue(tmp_path):
    s=store(tmp_path);a=s.submit('command','one','model','',b'{}');s.claim();s.update(a,state='preparing');s.recover()
    assert s.status(a)['state']=='queued'

def test_repeat_release_is_idempotent(tmp_path):
    s=store(tmp_path);a=s.submit('command','one','model','',b'{}');s.claim()
    s.release(a,0,child_alive=False);s.release(a,0,child_alive=False)
    assert s.status(a)['state']=='completed'

def test_two_connections_cannot_claim_one_job(tmp_path):
    import concurrent.futures
    s=store(tmp_path);a=s.submit('ollama','one','model','/api/generate',b'{}')
    from store import Store
    other=Store(tmp_path/'jobs.sqlite3')
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        rows=list(pool.map(lambda x:x.claim(),[s,other]))
    assert sum(r is not None for r in rows)==1
