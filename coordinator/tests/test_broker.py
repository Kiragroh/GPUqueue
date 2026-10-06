import json
import os
from pathlib import Path
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import pytest
import requests
import psutil
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import Broker,Handler,alive

class Backend(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        body=json.dumps({'models':[]}).encode();self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        time.sleep(.1);body=b'{"response":"synthetic-result","done":true}\n'
        self.send_response(200);self.send_header('Content-Type','application/x-ndjson');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)

@pytest.fixture
def app(tmp_path,monkeypatch):
    # These are broker/HTTP tests, not an NVIDIA device acceptance test.
    monkeypatch.setattr('server.gpu_snapshot',lambda: {'free_mb':16000,'used_mb':0,'total_mb':16000,'observed_at':time.time()})
    backend=ThreadingHTTPServer(('127.0.0.1',0),Backend);threading.Thread(target=backend.serve_forever,daemon=True).start()
    broker=Broker(tmp_path,backend=f'http://127.0.0.1:{backend.server_port}')
    http=ThreadingHTTPServer(('127.0.0.1',0),Handler);http.broker=broker
    broker.worker.start();threading.Thread(target=http.serve_forever,daemon=True).start()
    yield broker,f'http://127.0.0.1:{http.server_port}'
    broker.stop.set();http.shutdown();backend.shutdown();http.server_close();backend.server_close()

def test_async_request_survives_original_connection(app):
    broker,url=app
    r=requests.post(url+'/api/generate',headers={'Prefer':'respond-async','Idempotency-Key':'synthetic'},json={'model':'openviking-embed:latest','prompt':'test'},timeout=5)
    assert r.status_code==202
    identifier=r.json()['job_id']
    r.close()
    response=requests.get(url+'/jobs/'+identifier+'/result',headers={'X-GPU-Token':broker.token},timeout=10)
    assert response.status_code==200 and 'synthetic-result' in response.text
    assert broker.store.status(identifier)['state']=='completed'

def test_no_release_for_missing_heartbeat_while_owner_lives(app):
    broker,url=app
    identifier=broker.store.submit('command','test','unit','',b'{}',pid=os.getpid(),birth=psutil.Process().create_time())
    for _ in range(100):
        row=broker.store.row(identifier)
        if row['state']=='running' and row['reason']=='gpu_granted':break
        time.sleep(.03)
    broker.store.update(identifier,heartbeat=time.time()-90)
    broker.command_tick(broker.store.row(identifier))
    assert broker.store.row(identifier)['state']=='running'
    assert broker.store.row(identifier)['reason']=='heartbeat_missing_process_still_alive'

def test_pid_reuse_does_not_count_as_original_process():
    assert alive(os.getpid(),psutil.Process().create_time())
    assert not alive(os.getpid(),psutil.Process().create_time()-10)

def test_web_origin_and_local_history_guard(app):
    broker,url=app
    assert requests.post(url+'/api/generate',json={'model':'x'},headers={'Origin':'https://example.org'}).status_code==403
    assert requests.get(url+'/history').status_code==200
    assert requests.get(url+'/history',headers={'Sec-Fetch-Site':'cross-site'}).status_code==403
    assert requests.post(url+'/api/pull',json={'model':'x'}).status_code==403

def test_supervisor_crash_does_not_assume_descendants_gone(app):
    broker,url=app
    identifier=broker.store.submit('command','test','unit','',b'{}',pid=99999999,birth=0)
    broker.store.update(identifier,state='running',child_pid=99999998,child_birth=0)
    broker.command_tick(broker.store.row(identifier))
    assert broker.store.row(identifier)['state']=='recovery_blocked'

def test_stale_tick_rereads_child_and_does_not_revive_terminal(app,monkeypatch):
    broker,url=app
    identifier=broker.store.submit('command','test','unit','',b'{}',pid=os.getpid(),birth=psutil.Process().create_time())
    broker.store.update(identifier,state='running')
    stale=broker.store.row(identifier)
    broker.store.update(identifier,child_pid=202,child_birth=1)
    monkeypatch.setattr('server.alive',lambda pid,birth:pid==202)
    broker.command_tick(stale)
    assert broker.store.row(identifier)['state']=='running'
    broker.store.finish(identifier,'completed')
    broker.command_tick(stale)
    assert broker.store.row(identifier)['state']=='completed'

def test_bad_context_rejected_before_durable_acceptance(app):
    broker,url=app
    r=requests.post(url+'/jobs/command',headers={'X-GPU-Token':broker.token},json={'pid':os.getpid(),'context':['invalid']})
    assert r.status_code==409
    assert broker.store.listing()==[]
    assert broker.worker.is_alive()

def test_cli_status_shows_only_bound_case_context(app,monkeypatch,capsys):
    import client
    broker,url=app
    broker.store.submit('command','synthetic','unit','',b'{}',pid=os.getpid(),birth=psutil.Process().create_time(),context={'case_id':'SYNTHETIC_SECRET','script_name':'test.py'})
    monkeypatch.setattr(client,'URL',url)
    monkeypatch.setattr(sys,'argv',['client.py','status'])
    client.main()
    output=capsys.readouterr().out
    assert 'SYNTHETIC_SECRET' in output
    assert all(j['context']=={'case_id':'SYNTHETIC_SECRET'} for j in json.loads(output)['jobs'])


def test_local_dashboard_patient_read_does_not_grant_control_or_payload_access(app):
    broker,url=app
    broker.store.set_paused(True)
    identifier=broker.store.submit('command','synthetic','unit','',b'{"private":"PAYLOAD_SECRET"}',
        context={'case_id':'SYNTHETIC_PATIENT','script':'C:/PRIVATE_PATH/run.py','chat_id':'PRIVATE_CHAT','other':'PRIVATE_EXTRA'})
    for route in ['/status','/history']:
        response=requests.get(url+route)
        assert response.status_code==200
        assert 'SYNTHETIC_PATIENT' in response.text
        assert all(secret not in response.text for secret in ['PRIVATE_PATH','PRIVATE_CHAT','PRIVATE_EXTRA','PAYLOAD_SECRET',broker.token])
        assert response.headers['X-Frame-Options']=='DENY'
        assert "frame-ancestors 'none'" in response.headers['Content-Security-Policy']
        assert 'Access-Control-Allow-Origin' not in response.headers
    status=requests.get(url+'/status').json()
    assert status['local_read_allowed'] is True
    assert status['private_unlocked'] is False and status['operator_unlocked'] is False and status['csrf'] is None
    for suffix in ['', '/result', '/specification']:
        assert requests.get(url+'/jobs/'+identifier+suffix).status_code==403
    for suffix,body in [('/priority',{'priority':20}),('/cancel',{})]:
        assert requests.post(url+'/jobs/'+identifier+suffix,json=body).status_code==403
    assert requests.post(url+'/ui-ticket',json={}).status_code==403
    rich=requests.get(url+'/status',headers={'X-GPU-Token':broker.token}).json()
    assert rich['jobs'][0]['context']['chat_id']=='PRIVATE_CHAT'


@pytest.mark.parametrize('route',['/','/status','/history'])
@pytest.mark.parametrize('headers',[
    {'Sec-Fetch-Site':'cross-site'}, {'Sec-Fetch-Site':'same-site'},
    {'Origin':'https://evil.test'}, {'Host':'evil.test'},
])
def test_sensitive_local_read_rejects_foreign_browser_context(app,route,headers):
    _,url=app
    assert requests.get(url+route,headers=headers).status_code==403


def test_history_lane_filter_is_applied_before_pagination(app):
    broker,url=app
    broker.store.set_paused(True)
    cpu=broker.store.submit('ollama','test','unit','',b'{}',lane='cpu')
    broker.store.finish(cpu,'completed')
    for _ in range(101):
        gpu=broker.store.submit('command','test','unit','',b'{}',lane='gpu')
        broker.store.finish(gpu,'completed')
    response=requests.get(url+'/history?lane=cpu&offset=0')
    assert [j['id'] for j in response.json()['jobs']]==[cpu]
    assert requests.get(url+'/history?lane=cpu&offset=100').json()['jobs']==[]
    assert len(requests.get(url+'/history?lane=gpu&offset=100').json()['jobs'])==1
    assert requests.get(url+'/history?lane=invalid').status_code==400

def test_readonly_ui_ticket_one_use_and_private_cookie(app):
    broker,url=app
    ticket=requests.post(url+'/ui-ticket',headers={'X-GPU-Token':broker.token},json={}).json()['ticket']
    session=requests.Session()
    assert session.get(url+'/login?ticket='+ticket).status_code==200
    assert session.get(url+'/status').json()['private_unlocked']
    assert requests.get(url+'/login?ticket='+ticket).status_code==403
    assert session.post(url+'/jobs/command',json={'pid':os.getpid()}).status_code==403
