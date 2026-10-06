import sys
import time
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from store import Store

def submit(s, name='test', **kwargs):
    return s.submit('command',name,'unit','',b'{}',**kwargs)

def probe(free=16000):
    return {'free_mb':free,'observed_at':time.time()}

def test_priority_aging_fifo(tmp_path):
    s=Store(tmp_path/'q.db')
    a=submit(s,'background',priority=0);b=submit(s,'high',priority=20)
    with s.db:s.db.execute('update jobs set created=? where id=?',(time.time()-1801,a))
    assert s.claim()['id']==a
    s.finish(a,'completed')
    assert s.claim()['id']==b

def test_queued_priority_can_change_but_active_cannot(tmp_path):
    s=Store(tmp_path/'q.db');a=submit(s);b=submit(s,'second')
    s.set_priority(b,20)
    assert s.claim()['id']==b
    with pytest.raises(ValueError):s.set_priority(b,0)
    with pytest.raises(ValueError):s.set_priority(a,11)

def test_budgets_reserve_conservatively_and_cap_at_two(tmp_path):
    s=Store(tmp_path/'q.db')
    ids=[submit(s,str(i),vram_mb=5000) for i in range(3)]
    assert s.claim(snapshot=probe())['id']==ids[0]
    assert s.claim(snapshot=probe(13000))['id']==ids[1]
    assert s.claim(snapshot=probe()) is None

def test_reused_free_reading_cannot_overallocate(tmp_path):
    s=Store(tmp_path/'q.db')
    a=submit(s,'one',vram_mb=7000);b=submit(s,'two',vram_mb=7000)
    snapshot=probe(14000)
    assert s.claim(snapshot=snapshot)['id']==a
    assert s.claim(snapshot=snapshot) is None
    assert s.status(b)['reason']=='vram_budget_wait'

def test_unknown_budget_exclusive_and_cpu_independent(tmp_path):
    s=Store(tmp_path/'q.db');a=submit(s)
    b=s.submit('ollama','cpu','openviking-embed:latest','/api/embed',b'{}',lane='cpu')
    c=submit(s,'second',vram_mb=1000)
    assert s.claim()['id']==a
    assert s.claim()['id']==b
    assert s.claim(snapshot=probe()) is None

def test_one_gpu_ollama_and_stale_probe_blocks_budget(tmp_path):
    s=Store(tmp_path/'q.db')
    for n in range(2):s.submit('ollama',str(n),'model','/api/generate',str(n).encode(),vram_mb=1000)
    assert s.claim(snapshot={'free_mb':16000,'observed_at':time.time()-60}) is None
    assert s.claim(snapshot=probe())
    assert s.claim(snapshot=probe()) is None

def test_active_cancel_holds_reservation_until_tree_confirmation(tmp_path):
    s=Store(tmp_path/'q.db');a=submit(s,cancel_capable=True)
    s.claim();s.cancel(a)
    assert s.row(a)['state']=='cancelling' and s.row(a)['cancel_requested']==1
    assert s.claim() is None
    with pytest.raises(ValueError):s.release(a,130,child_alive=True,tree_empty=True)
    with pytest.raises(ValueError):s.release(a,130,child_alive=False)
    s.release(a,130,child_alive=False,tree_empty=True)
    assert s.row(a)['state']=='cancelled'
    s.release(a,130,child_alive=False,tree_empty=True)

def test_legacy_active_cancel_is_honestly_unsupported(tmp_path):
    s=Store(tmp_path/'q.db');a=submit(s);s.claim()
    with pytest.raises(ValueError):s.cancel(a)
    assert s.status(a)['cancel_capable']==0
    assert s.row(a)['state']=='running'

def test_one_opt_in_oom_retry_after_tree_cleaned(tmp_path):
    s=Store(tmp_path/'q.db');a=submit(s,repeatable=True,oom_exit_code=42,cancel_capable=True,vram_mb=1000)
    s.claim(snapshot=probe())
    s.release(a,42,child_alive=False,tree_empty=True)
    assert s.row(a)['state']=='queued' and s.row(a)['retry_exclusive']==1
    assert s.claim(snapshot=probe())['attempt']==2
    s.release(a,42,child_alive=False,tree_empty=True)
    assert s.row(a)['state']=='failed'
    assert len(s.status(a,private=True)['attempts'])==2

def test_cancel_never_retries_and_restart_preserves_cancel(tmp_path):
    s=Store(tmp_path/'q.db');a=submit(s,repeatable=True,oom_exit_code=42,cancel_capable=True)
    s.claim();s.cancel(a);s.recover()
    assert s.row(a)['state']=='cancelling'
    s.release(a,42,child_alive=False,tree_empty=True)
    assert s.row(a)['state']=='cancelled'

def test_drain_is_durable_and_keeps_queued_jobs(tmp_path):
    s=Store(tmp_path/'q.db');a=submit(s)
    assert s.set_paused(True)['idle']
    assert s.claim() is None
    assert Store(tmp_path/'q.db').paused()
    assert s.row(a)['state']=='queued'
    s.set_paused(False)
    assert s.claim()['id']==a

def test_additive_migration_preserves_old_payload(tmp_path):
    import sqlite3
    db=sqlite3.connect(tmp_path/'q.db')
    db.execute('create table jobs(id TEXT PRIMARY KEY,seq INTEGER,kind TEXT,owner TEXT,model TEXT,route TEXT,payload BLOB,digest TEXT,idem TEXT UNIQUE,state TEXT,reason TEXT,created REAL,started REAL,ended REAL,heartbeat REAL,owner_pid INTEGER,owner_birth REAL,child_pid INTEGER,child_birth REAL,min_free INTEGER,http_status INTEGER,content_type TEXT,exit_code INTEGER)')
    db.commit();db.close()
    s=Store(tmp_path/'q.db');a=submit(s)
    assert s.payload(a)==b'{}'
    assert s.status(a)['priority']==10
    assert s.status(a)['lane']=='gpu'

def test_lost_retry_release_reply_is_idempotent_even_after_second_claim(tmp_path):
    s=Store(tmp_path/'q.db');a=submit(s,repeatable=True,oom_exit_code=42,cancel_capable=True)
    s.claim()
    response=s.release(a,42,child_alive=False,tree_empty=True,attempt=1)
    assert response['retry_scheduled']
    s.claim()
    replay=s.release(a,42,child_alive=False,tree_empty=True,attempt=1)
    assert replay['retry_scheduled']
    assert s.row(a)['state']=='running' and s.row(a)['attempt']==2

def test_missing_and_low_probe_block_zero_minimum_gpu(tmp_path):
    s=Store(tmp_path/'q.db');submit(s,min_free=0)
    assert s.claim(snapshot={}) is None
    assert s.claim(snapshot=probe(1000)) is None

def test_lane_specific_queue_position_and_listing(tmp_path):
    s=Store(tmp_path/'q.db');gpu=submit(s,'gpu',priority=0)
    cpu=s.submit('ollama','cpu','openviking-embed:latest','/api/embed',b'{}',lane='cpu')
    fast=submit(s,'high',priority=20)
    assert s.listing()[0]['id']==fast
    assert s.status(cpu)['jobs_ahead']==0
    assert s.status(gpu)['jobs_ahead']==1

def test_same_model_default_tag_blocks_cross_lane_unload_race(tmp_path):
    s=Store(tmp_path/'q.db')
    s.submit('ollama','cpu','openviking-embed:latest','/api/embed',b'{}',lane='cpu')
    other=s.submit('ollama','gpu','openviking-embed','/api/generate',b'{}')
    assert s.claim()['lane']=='cpu'
    assert s.claim(snapshot=probe()) is None
    assert s.row(other)['reason']=='model_in_use'

def test_lost_oom_release_reply_then_queued_cancel_is_terminal_ack(tmp_path):
    s=Store(tmp_path/'q.db');a=submit(s,repeatable=True,oom_exit_code=42,cancel_capable=True)
    s.claim()
    assert s.release(a,42,child_alive=False,tree_empty=True,attempt=1)['retry_scheduled']
    s.cancel(a)
    replay=s.release(a,42,child_alive=False,tree_empty=True,attempt=1)
    assert replay['terminal_ack'] is True
    assert not replay.get('retry_scheduled')
    assert s.row(a)['state']=='cancelled'

def test_terminal_cancel_does_not_ack_unrelated_attempt(tmp_path):
    s=Store(tmp_path/'q.db');a=submit(s,repeatable=True,oom_exit_code=42,cancel_capable=True)
    s.claim();s.release(a,42,child_alive=False,tree_empty=True,attempt=1);s.cancel(a)
    with pytest.raises(ValueError):s.release(a,99,child_alive=False,tree_empty=True,attempt=1)
