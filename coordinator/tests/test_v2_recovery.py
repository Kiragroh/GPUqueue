import sys
from pathlib import Path
from types import SimpleNamespace
import time
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from store import Store
from server import Broker

def blocked(tmp_path,reason='backend_or_cleanup_uncertain',lane='gpu',kind='ollama'):
    b=Broker.__new__(Broker);b.store=Store(tmp_path/'q.db');b.backend='http://127.0.0.1:11434'
    b.recovery_checks={};b.unloaded=[];b.resident=set()
    b.resident_models=lambda:b.resident;b.unload=b.unloaded.append
    identifier=b.store.submit(kind,'app','gemma4:12b','/api/chat',b'{}',lane=lane)
    b.store.update(identifier,state='recovery_blocked',reason=reason,started=time.time())
    return b,identifier

def tick(b,monkeypatch,now):
    monkeypatch.setattr('server.time.monotonic',lambda:now);b.reconcile_blocked_inference()

@pytest.mark.parametrize('reason',['backend_or_cleanup_uncertain','broker_restart_backend_state_uncertain'])
@pytest.mark.parametrize('lane',['gpu','cpu'])
def test_two_idle_proofs_release_blocked_lane_without_replay(tmp_path,monkeypatch,reason,lane):
    b,i=blocked(tmp_path,reason,lane);queued=b.store.submit('ollama','x','gemma4:12b','/api/chat',b'{}')
    b.idle_backend=lambda:(7,200)
    tick(b,monkeypatch,10);assert b.store.row(i)['state']=='recovery_blocked'
    tick(b,monkeypatch,15)
    assert (b.store.row(i)['state'],b.store.row(i)['reason'])==('failed','backend_verified_idle_no_replay')
    assert b.store.row(queued)['state']=='queued'

def test_resident_model_of_blocked_job_is_unloaded_first(tmp_path,monkeypatch):
    b,i=blocked(tmp_path);b.resident={'gemma4:12b'};b.idle_backend=lambda:None
    tick(b,monkeypatch,10)
    assert b.unloaded==['gemma4:12b'] and b.store.row(i)['state']=='recovery_blocked'

@pytest.mark.parametrize('kind,reason',[('command','backend_or_cleanup_uncertain'),('ollama','supervisor_crash_tree_exit_unconfirmed')])
def test_other_blockades_are_never_acknowledged(tmp_path,monkeypatch,kind,reason):
    b,i=blocked(tmp_path,reason,kind=kind);b.idle_backend=lambda:pytest.fail('outside eligible scope')
    tick(b,monkeypatch,10);tick(b,monkeypatch,15);assert b.store.row(i)['state']=='recovery_blocked'

def test_other_active_inference_prevents_release(tmp_path,monkeypatch):
    b,i=blocked(tmp_path)
    other=b.store.submit('ollama','other','other-model','/api/embed',b'{}',lane='cpu')
    b.store.update(other,state='running',started=110)
    b.idle_backend=lambda:pytest.fail('backend is in use')
    tick(b,monkeypatch,10);tick(b,monkeypatch,15)
    assert b.store.row(i)['state']=='recovery_blocked'

def test_busy_or_changed_backend_resets_confirmation(tmp_path,monkeypatch):
    b,i=blocked(tmp_path);identities=iter([(7,200),None,(7,200),(8,300),(8,300)])
    b.idle_backend=lambda:next(identities)
    for now in (10,15,20,25):
        tick(b,monkeypatch,now);assert b.store.row(i)['state']=='recovery_blocked'
    tick(b,monkeypatch,30);assert b.store.row(i)['state']=='failed'

@pytest.mark.parametrize('name,command,models,listener,expected',[
    ('ollama.exe',['ollama.exe','serve'],[],7,(7,200)),
    ('ollama.exe',['ollama.exe','serve'],[{'name':'busy'}],7,None),
    ('ollama.exe',['ollama.exe','serve'],[],8,None),
    ('other.exe',['other.exe'],[],7,None),
])
def test_idle_proof_checks_listener_identity_runners_and_models(tmp_path,monkeypatch,name,command,models,listener,expected):
    b,i=blocked(tmp_path)
    server=SimpleNamespace(pid=7,name=lambda:name,cmdline=lambda:command,create_time=lambda:200,children=lambda recursive:[])
    monkeypatch.setattr('server.psutil.Process',lambda pid:server if pid==7 else pytest.fail('foreign listener inspected'))
    monkeypatch.setattr('server.psutil.net_connections',lambda kind:[SimpleNamespace(status='LISTEN',laddr=SimpleNamespace(port=11434),pid=listener)])
    monkeypatch.setattr('server.requests.get',lambda *a,**k:SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'models':models}))
    if listener==8:monkeypatch.setattr('server.psutil.Process',lambda pid:SimpleNamespace(pid=8,name=lambda:'other.exe',cmdline=lambda:[],create_time=lambda:1,children=lambda recursive:[]))
    assert b.idle_backend()==expected

def test_live_runner_is_not_idle(tmp_path,monkeypatch):
    b,i=blocked(tmp_path)
    runner=SimpleNamespace(name=lambda:'llama-server.exe',cmdline=lambda:['llama-server.exe'])
    server=SimpleNamespace(pid=7,name=lambda:'ollama.exe',cmdline=lambda:['ollama.exe','serve'],create_time=lambda:200,children=lambda recursive:[runner])
    monkeypatch.setattr('server.psutil.Process',lambda pid:server)
    monkeypatch.setattr('server.psutil.net_connections',lambda kind:[SimpleNamespace(status='LISTEN',laddr=SimpleNamespace(port=11434),pid=7)])
    monkeypatch.setattr('server.requests.get',lambda *a,**k:SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'models':[]}))
    assert b.idle_backend() is None
