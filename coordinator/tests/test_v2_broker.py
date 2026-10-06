import json
import os
import sys
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import pytest
import requests
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import Broker,Handler

class Backend(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def reply(self,data):
        body=json.dumps(data).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def do_GET(self):self.reply({'models':[]})
    def do_POST(self):
        data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls.append((self.path,data))
        if self.path=='/api/show':return self.reply({'capabilities':['embedding'],'parameters':'num_gpu 0','model_info':{'general.architecture':'qwen3'}})
        if data.get('keep_alive')==0 and self.path=='/api/generate':return self.reply({})
        self.server.entered.set();self.server.allow.wait(3)
        return self.reply({'model':data['model'],'embeddings':[[1.,2.],[3.,4.]],'prompt_eval_count':7})

@pytest.fixture
def app(tmp_path,monkeypatch):
    monkeypatch.setattr('server.gpu_snapshot',lambda:{'free_mb':16000,'observed_at':time.time()})
    backend=ThreadingHTTPServer(('127.0.0.1',0),Backend);backend.calls=[];backend.entered=threading.Event();backend.allow=threading.Event();backend.allow.set()
    threading.Thread(target=backend.serve_forever,daemon=True).start()
    broker=Broker(tmp_path,backend=f'http://127.0.0.1:{backend.server_port}')
    http=ThreadingHTTPServer(('127.0.0.1',0),Handler);http.broker=broker
    broker.start();threading.Thread(target=http.serve_forever,daemon=True).start()
    yield broker,f'http://127.0.0.1:{http.server_port}',backend
    backend.allow.set();broker.stop.set();broker.worker.join(3);http.shutdown();backend.shutdown();http.server_close();backend.server_close();broker.store.db.close();broker.lockfile.close()

def token(b):return {'X-GPU-Token':b.token}
def wait_terminal(b,id):
    for _ in range(100):
        r=b.store.row(id)
        if r['state'] in {'completed','cancelled','failed','recovery_blocked'}:return r
        time.sleep(.03)
    return r

def test_cpu_v1_conversion_forces_zero_gpu_and_ordered_response(app):
    b,url,backend=app
    id=b.submit_ollama('/v1/embeddings',json.dumps({'model':'openviking-embed:latest','input':['a','b'],'options':{'num_gpu':99}}).encode(),'test')
    assert wait_terminal(b,id)['state']=='completed'
    assert b.store.row(id)['lane']=='cpu'
    call=next(body for route,body in backend.calls if route=='/api/embed')
    assert call['options']['num_gpu']==0 and call['input']==['a','b']
    result=json.loads(b''.join(b.store.chunks(id)))
    assert [x['embedding'] for x in result['data']]==[[1.,2.],[3.,4.]]
    assert [x['index'] for x in result['data']]==[0,1]
    assert result['usage']=={'prompt_tokens':7,'total_tokens':7}

def test_name_or_options_alone_never_grants_cpu(app):
    b,url,backend=app
    b.store.set_paused(True)
    a=b.submit_ollama('/api/generate',b'{"model":"openviking-embed:latest","options":{"num_gpu":0}}','test')
    c=b.submit_ollama('/api/embed',b'{"model":"other","options":{"num_gpu":0}}','test')
    assert b.store.row(a)['lane']==b.store.row(c)['lane']=='gpu'


def test_systemone_uses_durable_gpu_lane_and_targeted_cleanup(app):
    b,url,backend=app
    payload={'model':'tev1:4b','state':'synthetic','questions':{'q':{'type':'choice','instructions':'Choose','criteria':{'yes':None,'no':None}}}}
    response=requests.post(url+'/jobs/ollama',headers={**token(b),'Idempotency-Key':'decision-test'},
        json={'route':'/v1/systemone','body':payload,'owner':'STR Hub','priority':0},timeout=5)
    assert response.status_code==202
    id=response.json()['job_id']
    assert wait_terminal(b,id)['state']=='completed'
    assert b.store.row(id)['lane']=='gpu'
    assert b.store.row(id)['priority']==0
    call=next(body for route,body in backend.calls if route=='/v1/systemone')
    assert call==dict(payload,keep_alive=0)
    assert [body['model'] for route,body in backend.calls if route=='/api/generate']==['tev1:4b']
    assert 'systemone' in requests.get(url+'/health',timeout=5).json()['capabilities']

def test_cancel_ollama_drains_and_unloads_only_own_model(app):
    b,url,backend=app;backend.allow.clear()
    id=b.submit_ollama('/api/embed',b'{"model":"openviking-embed:latest","input":"a"}','test')
    assert backend.entered.wait(2)
    b.store.cancel(id)
    assert b.store.row(id)['state']=='cancelling'
    backend.allow.set()
    assert wait_terminal(b,id)['state']=='cancelled'
    unloads=[body['model'] for route,body in backend.calls if route=='/api/generate']
    assert unloads==['openviking-embed:latest']

def test_operator_ticket_requires_origin_csrf_and_only_two_actions(app):
    b,url,backend=app;b.store.set_paused(True)
    id=b.store.submit('command','test','x','',b'{}')
    ticket=requests.post(url+'/ui-ticket',headers=token(b),json={'mode':'control'}).json()['ticket']
    session=requests.Session();assert session.get(url+'/login?ticket='+ticket).status_code==200
    status=session.get(url+'/status').json()
    assert status['operator_unlocked'] and status['csrf']
    headers={'Origin':url,'X-GPU-CSRF':status['csrf']}
    assert session.post(url+'/jobs/'+id+'/priority',headers=headers,json={'priority':20}).status_code==200
    assert b.store.row(id)['priority']==20
    assert session.post(url+'/jobs/'+id+'/cancel',json={}).status_code==403
    assert session.post(url+'/jobs/'+id+'/cancel',headers={**headers,'Origin':'https://evil.test'},json={}).status_code==403
    assert session.post(url+'/jobs/command',headers=headers,json={'pid':os.getpid()}).status_code==403
    assert session.post(url+'/drain',headers=headers,json={'paused':False}).status_code==403
    assert session.post(url+'/jobs/'+id+'/cancel',headers=headers,json={}).status_code==200
    assert requests.get(url+'/status').json().get('csrf') is None
    assert b.token not in session.get(url+'/').text

def test_drain_token_only_and_health_capabilities(app):
    b,url,backend=app
    assert requests.post(url+'/drain',json={'paused':True}).status_code==403
    result=requests.post(url+'/drain',headers=token(b),json={'paused':True}).json()
    assert result=={'paused':True,'idle':True}
    assert requests.get(url+'/health').json()['version']==2

def test_budgeted_command_and_cpu_overlap_without_global_unload(app):
    import psutil
    b,url,backend=app
    id=b.store.submit('command','test','cmd','',b'{}',pid=os.getpid(),birth=psutil.Process().create_time(),vram_mb=5000)
    for _ in range(100):
        if b.store.row(id)['reason']=='gpu_granted':break
        time.sleep(.02)
    cpu=b.submit_ollama('/api/embed',b'{"model":"openviking-embed:latest","input":"a"}','test')
    assert wait_terminal(b,cpu)['state']=='completed'
    assert b.store.row(id)['state']=='running'
    assert all(body['model']=='openviking-embed:latest' for route,body in backend.calls if route=='/api/generate')

def test_alias_change_before_backend_is_known_failure_not_recovery_blocked(app,monkeypatch):
    b,url,backend=app;b.store.set_paused(True)
    id=b.submit_ollama('/api/embed',b'{"model":"openviking-embed:latest","input":"a"}','test')
    monkeypatch.setattr(b,'cpu_alias_verified',lambda:False)
    b.store.set_paused(False)
    assert wait_terminal(b,id)['state']=='failed'
    assert not backend.entered.is_set()

def test_supervisor_disappears_after_grant_before_registration_blocks_lane(app):
    b,url,backend=app;b.store.set_paused(True)
    id=b.store.submit('command','test','x','',b'{}',pid=99999999,birth=0)
    b.store.update(id,state='running',reason='gpu_granted')
    b.command_tick(b.store.row(id))
    assert b.store.row(id)['state']=='recovery_blocked'
