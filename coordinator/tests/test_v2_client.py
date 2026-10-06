import argparse
import os
from pathlib import Path
import sys
import threading
import time
import psutil
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import client
from store import Store

def args(command,**extra):
    data=dict(command=command,script='synthetic.py',chat='synthetic',case_id=None,owner='test',model='synthetic',min_free_mb=0,priority='high',vram_mb=2000,repeatable=False,oom_exit_code=None)
    data.update(extra);return argparse.Namespace(**data)

def bridge(monkeypatch,tmp_path,*,cancel_at=None):
    store=Store(tmp_path/'q.db');seen=[]
    def request(path,data=None,**kwargs):
        if path=='/jobs/command':
            seen.append(data)
            id=store.submit('command',data['owner'],data['model'],'',b'{}',pid=data['pid'],birth=psutil.Process().create_time(),cancel_capable=data.get('cancel_capable',False),repeatable=data.get('repeatable',False),oom_exit_code=data.get('oom_exit_code'),vram_mb=data.get('vram_mb'))
            return {'job_id':id}
        _,_,id,action=path.split('/')
        if action=='heartbeat':
            if store.row(id)['state']=='queued':
                store.claim(snapshot={'free_mb':16000,'observed_at':time.time()});store.update(id,reason='gpu_granted')
            if cancel_at=='before_child' and store.row(id)['state']=='running':store.cancel(id)
            if cancel_at=='running' and store.row(id)['child_pid'] and store.row(id)['state']=='running':store.cancel(id)
        elif action=='child':
            if cancel_at=='registration':store.cancel(id);raise ValueError('not_available_for_child')
            store.update(id,child_pid=data['pid'],child_birth=psutil.Process(data['pid']).create_time())
        elif action=='release':
            store.release(id,data['exit_code'],child_alive=False,tree_empty=data.get('tree_empty',False),attempt=data.get('attempt'))
        elif action=='cancel':store.cancel(id)
        return store.status(id)
    monkeypatch.setattr(client,'request',request)
    return store,seen

def test_cli_submission_carries_capabilities_and_scheduling(tmp_path,monkeypatch):
    store,seen=bridge(monkeypatch,tmp_path)
    assert client.supervised(args([sys.executable,'-c','pass']))==0
    assert seen[0]['priority']==20 and seen[0]['vram_mb']==2000
    assert seen[0]['cancel_capable'] is True
    assert store.listing()[0]['state']=='completed'

@pytest.mark.parametrize('where',['before_child','running','registration'])
def test_supervisor_cancellation_races_end_owned_tree(tmp_path,monkeypatch,where):
    store,seen=bridge(monkeypatch,tmp_path,cancel_at=where)
    assert client.supervised(args([sys.executable,'-c','import time; time.sleep(4)']))==130
    assert store.listing()[0]['state']=='cancelled'
    assert store.listing()[0]['reason']=='owned_tree_exit_confirmed'

def test_retry_runs_same_job_exactly_twice(tmp_path,monkeypatch):
    store,seen=bridge(monkeypatch,tmp_path)
    assert client.supervised(args([sys.executable,'-c','raise SystemExit(42)'],repeatable=True,oom_exit_code=42))==42
    assert len(seen)==1
    row=store.listing(private=True)[0]
    assert row['attempt']==2 and row['state']=='failed'
    assert [a['exit_code'] for a in row['attempts']]==[42,42]

def test_open_control_requests_only_scoped_ticket(monkeypatch):
    sent=[];opened=[]
    monkeypatch.setattr(sys,'argv',['client.py','open','--control'])
    monkeypatch.setattr(client,'request',lambda path,data:sent.append((path,data)) or {'ticket':'one-use'})
    monkeypatch.setattr(client.webbrowser,'open',opened.append)
    client.main()
    assert sent==[('/ui-ticket',{'mode':'control'})]
    assert opened==[client.URL+'/login?ticket=one-use']

def test_keyboard_interrupt_after_registration_releases_confirmed_tree(tmp_path,monkeypatch):
    import win32process
    store,seen=bridge(monkeypatch,tmp_path)
    def interrupt(handle):raise KeyboardInterrupt()
    monkeypatch.setattr(win32process,'ResumeThread',interrupt)
    assert client.supervised(args([sys.executable,'-c','import time; time.sleep(4)']))==130
    assert store.listing()[0]['state']=='cancelled'

def test_owned_descendants_are_gone_before_release(tmp_path,monkeypatch):
    store,seen=bridge(monkeypatch,tmp_path)
    original=client.request;descendants=[]
    def checking(path,data=None,**kwargs):
        if path.endswith('/heartbeat'):
            for r in store.active_rows():
                if r['child_pid']:
                    try:descendants.extend(psutil.Process(r['child_pid']).children(recursive=True))
                    except psutil.NoSuchProcess:pass
        if path.endswith('/release'):
            assert descendants
            assert all(not p.is_running() for p in descendants)
        return original(path,data,**kwargs)
    monkeypatch.setattr(client,'request',checking)
    script="import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(8)']); time.sleep(1.8)"
    assert client.supervised(args([sys.executable,'-c',script]))==0

def test_lost_release_reply_recovers_definitive_terminal_cancellation(tmp_path,monkeypatch):
    store,seen=bridge(monkeypatch,tmp_path)
    original=client.request;release_calls=[]
    def flaky(path,data=None,**kwargs):
        if data is None and path.startswith('/jobs/') and len(path.split('/'))==3:
            return store.status(path.split('/')[-1],private=True)
        if path.endswith('/release'):
            release_calls.append(data)
            if len(release_calls)>1:raise AssertionError('definitive terminal acknowledgement must stop retries')
            response=original(path,data,**kwargs)
            store.cancel(path.split('/')[2])
            raise OSError('reply_lost')
        return original(path,data,**kwargs)
    monkeypatch.setattr(client,'request',flaky)
    def no_sleep(_):raise AssertionError('terminal cancellation should be acknowledged without reconnect loop')
    monkeypatch.setattr(client.time,'sleep',no_sleep)
    assert client.supervised(args([sys.executable,'-c','raise SystemExit(42)'],repeatable=True,oom_exit_code=42))==130
