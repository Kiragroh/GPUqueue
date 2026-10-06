"""Loopback GPU broker. Never executes submitted shell commands or kills processes."""
import argparse
import json
import os
from pathlib import Path
import secrets
import re
import base64
import struct
import subprocess
import threading
import time
import msvcrt
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from urllib.parse import urlsplit,parse_qs

import psutil
import requests
from store import Store, ACTIVE, TERMINAL

STATE=Path(os.environ.get('LOCALAPPDATA',str(Path.home()))) / 'GPUqueue/state'
BACKEND='http://127.0.0.1:11434'
ROUTES={'/api/generate','/api/chat','/api/embed','/api/embeddings','/v1/chat/completions','/v1/completions','/v1/embeddings','/v1/responses','/v1/systemone'}
READ_ROUTES={'/api/ps','/api/tags','/api/version','/v1/models'}

class OwnerExitedBeforeStart(Exception):pass

def alive(pid,birth):
    if not pid:return False
    try:return abs(psutil.Process(pid).create_time()-float(birth))<.01
    except (psutil.NoSuchProcess,ValueError,TypeError):return False
    except psutil.AccessDenied:return True

def gpu_snapshot():
    try:
        raw=subprocess.check_output(['nvidia-smi','--query-gpu=index,name,memory.used,memory.free,memory.total,utilization.gpu','--format=csv,noheader,nounits'],text=True,timeout=8,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        cols=raw.splitlines()[0].split(', ')
        return dict(index=int(cols[0]),name=cols[1],used_mb=int(cols[2]),free_mb=int(cols[3]),total_mb=int(cols[4]),utilization=int(cols[5]),observed_at=time.time())
    except Exception:return {'error':'gpu_probe_unavailable'}

class Broker:
    def __init__(self,state,backend=BACKEND):
        self.state=Path(state);self.state.mkdir(parents=True,exist_ok=True)
        self.lockfile=(self.state/'server.lock').open('a+b')
        if self.lockfile.seek(0,2)==0:self.lockfile.write(b'0');self.lockfile.flush()
        self.lockfile.seek(0);msvcrt.locking(self.lockfile.fileno(),msvcrt.LK_NBLCK,1)
        token_path=self.state/'control.token'
        if not token_path.exists():
            with token_path.open('x',encoding='ascii') as f:f.write(secrets.token_urlsafe(40))
        self.token=token_path.read_text().strip()
        self.store=Store(self.state/'jobs.sqlite3');self.store.recover()
        self.backend=backend;self.stop=threading.Event()
        self.ui_tickets={};self.ui_sessions={}
        self.worker=threading.Thread(target=self.work,daemon=True)
        self.gpu={};self.probe=threading.Thread(target=self.probe_loop,daemon=True)

    def probe_loop(self):
        while not self.stop.is_set():
            self.gpu=gpu_snapshot();self.stop.wait(4)

    def start(self):self.worker.start();self.probe.start()

    def unload(self,model):
        # Never unload unrelated residents or another concurrent request's model.
        r=requests.post(self.backend+'/api/generate',json={'model':model,'keep_alive':0,'stream':False},timeout=120)
        r.raise_for_status()
        response=requests.get(self.backend+'/api/ps',timeout=10);response.raise_for_status()
        canonical=lambda name:name if ':' in name.rsplit('/',1)[-1] else name+':latest'
        if any(canonical(m.get('name',''))==canonical(model) for m in response.json().get('models',[])):
            raise RuntimeError('model_unload_not_confirmed')

    def prepare(self,r):
        with self.store.lock:
            row=self.store.row(r['id'])
            if row['cancel_requested']:return
            if row['kind']=='command' and not alive(row['owner_pid'],row['owner_birth']):raise OwnerExitedBeforeStart()
            self.store.update(r['id'],state='running',reason='cpu_granted' if r['lane']=='cpu' else 'gpu_granted',started=time.time())

    def command_tick(self,r):
        with self.store.lock:
            self._command_tick_locked(self.store.row(r['id']))

    def _command_tick_locked(self,r):
        if r['state'] not in ACTIVE or r['state']=='recovery_blocked':return
        child=alive(r['child_pid'],r['child_birth'])
        owner=alive(r['owner_pid'],r['owner_birth'])
        if not owner and not child:
            if r['child_pid'] or r['reason']!='starting':
                self.store.update(r['id'],state='recovery_blocked',reason='supervisor_crash_tree_exit_unconfirmed')
            else:self.store.finish(r['id'],'interrupted','supervisor_exited_before_child')
        elif not owner and child:
            self.store.update(r['id'],reason='orphan_child_still_running')
        elif time.time()-r['heartbeat']>20:
            self.store.update(r['id'],reason='heartbeat_missing_process_still_alive')

    def work(self):
        while not self.stop.is_set():
            active=self.store.active_rows()
            for r in active:
                if r['kind']=='command':self.command_tick(r)
            snapshot=gpu_snapshot();self.gpu=snapshot
            r=self.store.claim(snapshot=snapshot)
            if r is None:self.stop.wait(.2);continue
            try:
                self.prepare(r)
                if r['kind']=='ollama':threading.Thread(target=self.run_infer,args=(r,),daemon=True).start()
            except OwnerExitedBeforeStart:
                self.store.finish(r['id'],'interrupted','owner_exited_before_start')
            except Exception:
                # A network timeout does not prove that Ollama stopped computing.
                self.store.update(r['id'],state='recovery_blocked',reason='backend_or_cleanup_uncertain')

    def run_infer(self,r):
        try:
            with self.store.lock:
                if self.store.row(r['id'])['cancel_requested']:
                    self.store.finish(r['id'],'cancelled','cancelled_before_backend');return
            self.infer(r)
        except Exception:
            self.store.update(r['id'],state='recovery_blocked',reason='backend_or_cleanup_uncertain')

    def cpu_alias_verified(self):
        response=requests.post(self.backend+'/api/show',json={'model':'openviking-embed:latest'},timeout=10)
        response.raise_for_status();info=response.json()
        return ('embedding' in info.get('capabilities',[]) and
                info.get('model_info',{}).get('general.architecture')=='qwen3' and
                bool(re.search(r'(?m)^\s*num_gpu\s+0\s*$',info.get('parameters',''))))

    def infer(self,r):
        body=json.loads(self.store.payload(r['id']))
        route=r['route'];convert=r['lane']=='cpu' and route=='/v1/embeddings'
        if r['lane']=='cpu':
            try:verified=self.cpu_alias_verified()
            except Exception:verified=False
            if not verified:
                self.store.finish(r['id'],'failed','cpu_alias_not_verified_before_inference');return
            if convert:
                original=body
                body={'model':body['model'],'input':body['input'],'options':{'num_gpu':0}}
                route='/api/embed'
            else:body.setdefault('options',{})['num_gpu']=0
        if route.startswith('/api/') or route=='/v1/systemone':
            body['keep_alive']=0
        size=0;overflow=False
        collected=[]
        with requests.post(self.backend+route,json=body,stream=True,timeout=(10,600)) as response:
            self.store.update(r['id'],http_status=response.status_code,content_type=response.headers.get('Content-Type','application/json'),reason='ollama_inference')
            for chunk in response.iter_content(chunk_size=4096):
                if chunk:
                    size+=len(chunk)
                    if size>64*1024*1024:overflow=True
                    if not overflow:
                        if convert:collected.append(chunk)
                        else:self.store.chunk(r['id'],chunk)
            code=response.status_code
        if convert and not overflow:
            result=json.loads(b''.join(collected))
            if code<400:
                embeddings=result['embeddings']
                if original.get('encoding_format')=='base64':
                    embeddings=[base64.b64encode(struct.pack('<'+'f'*len(e),*e)).decode('ascii') for e in embeddings]
                count=result.get('prompt_eval_count',0)
                result={'object':'list','model':r['model'],'data':[{'object':'embedding','index':i,'embedding':e} for i,e in enumerate(embeddings)],'usage':{'prompt_tokens':count,'total_tokens':count}}
            self.store.chunk(r['id'],json.dumps(result).encode())
        with self.store.lock:
            cancelled=self.store.row(r['id'])['cancel_requested']
            self.store.update(r['id'],state='cancelling' if cancelled else 'draining',reason='cancel_pending_backend_drain' if cancelled else 'unloading_model_after_response')
        self.unload(r['model'])
        with self.store.lock:
            cancelled=self.store.row(r['id'])['cancel_requested']
            self.store.finish(r['id'],'cancelled' if cancelled else ('completed' if code<400 and not overflow else 'failed'),
                              'backend_drained_model_unloaded' if cancelled else ('response_limit_exceeded' if overflow else ('http_error' if code>=400 else 'result_saved_model_unloaded')))

    def submit_ollama(self,route,body,owner,key=None,context=None,priority=10,vram_mb=None):
        if route not in ROUTES:raise ValueError('route_not_allowed')
        payload=json.loads(body)
        if not isinstance(payload,dict) or not isinstance(payload.get('model'),str):raise ValueError('model_required')
        model=payload['model']
        if len(model)>100:raise ValueError('model_name_too_long')
        lane='gpu'
        if model=='openviking-embed:latest' and route in {'/api/embed','/api/embeddings','/v1/embeddings'} and self.cpu_alias_verified():
            lane='cpu'
            if route=='/v1/embeddings':
                inputs=payload.get('input')
                if not isinstance(inputs,str) and not (isinstance(inputs,list) and all(isinstance(i,str) for i in inputs)):raise ValueError('string_embedding_input_required')
                if payload.get('encoding_format','float') not in {'float','base64'} or payload.get('dimensions') is not None:raise ValueError('unsupported_embedding_format_or_dimensions')
            elif not isinstance(payload.get('options',{}),dict):raise ValueError('invalid_options')
        return self.store.submit('ollama',owner,model,route,body,key=key,min_free=0 if lane=='cpu' else 4000,
                                 context=context or {'script_name':owner},priority=priority,vram_mb=vram_mb,lane=lane)


class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*args):pass
    @property
    def broker(self):return self.server.broker
    def permitted(self):
        host=self.headers.get('Host','')
        if host not in {f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}:return False
        origin=self.headers.get('Origin')
        return not origin or origin==f'http://{host}'
    def auth(self):return secrets.compare_digest(self.headers.get('X-GPU-Token',''),self.broker.token)
    def read_auth(self):
        if self.auth():return True
        return self.ui_session() is not None
    def ui_session(self):
        try:
            cookie=SimpleCookie(self.headers.get('Cookie',''));value=cookie['GPU_VIEW'].value
            session=self.broker.ui_sessions.get(value)
            return session if session and session['expires']>time.time() else None
        except (KeyError,ValueError):return None
    def operator_auth(self):
        session=self.ui_session()
        return bool(session and session['mode']=='control' and self.headers.get('Origin')==f'http://{self.headers.get("Host")}' and
                    secrets.compare_digest(self.headers.get('X-GPU-CSRF',''),session['csrf']))
    def dashboard_jobs(self,**kwargs):
        jobs=self.broker.store.listing(private=True,**kwargs)
        if not self.read_auth():
            # Explicit local read permission is not operator or rich-context auth.
            # Never release arbitrary context (paths, chat IDs or future fields).
            for job in jobs:
                context=job.get('context',{})
                job['context']={'case_id':context['case_id']} if context.get('case_id') else {}
        return jobs
    def respond(self,status,body,content='application/json'):
        if not isinstance(body,bytes):body=json.dumps(body).encode()
        self.send_response(status);self.send_header('Content-Type',content);self.send_header('Content-Length',str(len(body)))
        if status>=400:
            self.close_connection=True;self.send_header('Connection','close')
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY');self.send_header('Content-Security-Policy',"frame-ancestors 'none'")
        self.send_header('Cross-Origin-Resource-Policy','same-origin');self.send_header('Referrer-Policy','no-referrer')
        self.end_headers();self.wfile.write(body)
    def do_GET(self):
        try:
            if not self.permitted():return self.respond(403,{'error':'local_origin_required'})
            route=urlsplit(self.path).path
            if route.startswith('/openviking/'):route=route[len('/openviking'):]
            if route in {'/','/status','/history'} and self.headers.get('Sec-Fetch-Site','none') not in {'none','same-origin'}:
                return self.respond(403,{'error':'local_origin_required'})
            if route=='/login':
                ticket=parse_qs(urlsplit(self.path).query).get('ticket',[''])[0]
                grant=self.broker.ui_tickets.pop(ticket,None)
                if not grant or grant['expires']<time.time():return self.respond(403,{'error':'ticket_expired'})
                cookie=secrets.token_urlsafe(32);self.broker.ui_sessions[cookie]={'expires':time.time()+1800,'mode':grant['mode'],'csrf':secrets.token_urlsafe(32)}
                self.send_response(303);self.send_header('Location','/');self.send_header('Content-Length','0')
                self.send_header('Set-Cookie',f'GPU_VIEW={cookie}; HttpOnly; SameSite=Strict; Path=/; Max-Age=1800')
                self.send_header('Referrer-Policy','no-referrer');self.end_headers();return
            if route=='/':return self.respond(200,Path(__file__).with_name('dashboard.html').read_bytes(),'text/html; charset=utf-8')
            if route=='/health':return self.respond(200,{'service':'local-gpu-coordinator','version':2,'pid':os.getpid(),'worker_alive':self.broker.worker.is_alive(), 'capabilities':['priority','active_cancel_v2_supervisor','cpu_embedding','budgeted_gpu_sharing','drain','systemone']})
            if route=='/status':
                session=self.ui_session();operator=bool(session and session['mode']=='control')
                return self.respond(200,{'gpu':self.broker.gpu,'jobs':self.dashboard_jobs(),'local_read_allowed':True,'private_unlocked':self.read_auth(),'operator_unlocked':operator,'csrf':session['csrf'] if operator else None,'paused':self.broker.store.paused(),'boundary':'Only integrated clients are coordinated; direct CUDA/backend access can bypass this broker.'})
            if route=='/history':
                query=parse_qs(urlsplit(self.path).query)
                lane=query.get('lane',[None])[0]
                if lane not in {None,'cpu','gpu'}:return self.respond(400,{'error':'invalid_lane'})
                offset=max(0,int(query.get('offset',['0'])[0]))
                return self.respond(200,{'jobs':self.dashboard_jobs(offset=offset,limit=100,lane=lane),'offset':offset,'lane':lane,'local_read_allowed':True})
            if route in READ_ROUTES:
                r=requests.get(self.broker.backend+route,timeout=10);return self.respond(r.status_code,r.content,r.headers.get('Content-Type','application/json'))
            parts=route.strip('/').split('/')
            if len(parts)>=2 and parts[0]=='jobs':
                if not self.auth():return self.respond(403,{'error':'token_required'})
                if len(parts)==2:return self.respond(200,self.broker.store.status(parts[1],private=True))
                if parts[2]=='result':return self.result(parts[1])
                if parts[2]=='specification':return self.respond(200,json.loads(self.broker.store.payload(parts[1])))
            return self.respond(404,{'error':'not_found'})
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
        except KeyError:self.respond(404,{'error':'unknown_job'})
        except Exception:self.respond(503,{'error':'request_failed'})

    def result(self,identifier):
        store=self.broker.store
        while True:
            r=store.row(identifier)
            if r['http_status'] is not None:break
            if r['state'] in TERMINAL or r['state']=='recovery_blocked':
                return self.respond(409,{'job_id':identifier,'state':r['state'],'reason':r['reason']})
            time.sleep(.2)
        self.send_response(r['http_status']);self.send_header('Content-Type',r['content_type'])
        self.send_header('X-GPU-Job-ID',identifier);self.send_header('Connection','close');self.send_header('Cache-Control','no-store');self.end_headers()
        n=0
        while True:
            state=store.row(identifier)['state']
            chunks=store.chunks(identifier,n)
            for chunk in chunks:self.wfile.write(chunk);self.wfile.flush();n+=1
            if state in TERMINAL or state=='recovery_blocked':break
            time.sleep(.1)
        self.close_connection=True

    def do_POST(self):
        try:
            if not self.permitted():return self.respond(403,{'error':'local_origin_required'})
            size=int(self.headers.get('Content-Length','0'))
            if size<=0 or size>32*1024*1024:return self.respond(413,{'error':'request_size'})
            body=self.rfile.read(size);route=urlsplit(self.path).path
            owner='ollama-client'
            if route.startswith('/openviking/'):
                owner='OpenViking';route=route[len('/openviking'):]
            key=self.headers.get('Idempotency-Key')
            if route in ROUTES:
                context={'script_name':'openviking-server.exe','script':'openviking-server.exe'} if owner=='OpenViking' else None
                identifier=self.broker.submit_ollama(route,body,owner,key,context,priority=0 if owner=='OpenViking' else 10)
                if self.headers.get('Prefer')=='respond-async':return self.respond(202,{'job_id':identifier,'status_url':'/jobs/'+identifier})
                return self.result(identifier)
            parts=route.strip('/').split('/')
            limited_action=len(parts)==3 and parts[0]=='jobs' and parts[2] in {'cancel','priority'}
            if not self.auth() and not (limited_action and self.operator_auth()):return self.respond(403,{'error':'token_or_scoped_operator_required'})
            data=json.loads(body);store=self.broker.store
            if route=='/ui-ticket':
                mode=data.get('mode','read')
                if mode not in {'read','control'}:raise ValueError('invalid_mode')
                ticket=secrets.token_urlsafe(32);self.broker.ui_tickets[ticket]={'expires':time.time()+60,'mode':mode}
                return self.respond(200,{'ticket':ticket})
            if route=='/drain':return self.respond(200,store.set_paused(data['paused']))
            if route=='/jobs/ollama':
                identifier=self.broker.submit_ollama(data['route'],json.dumps(data['body']).encode(),data.get('owner','local-client'),key,data.get('context'),data.get('priority',10),data.get('vram_mb'))
                return self.respond(202,{'job_id':identifier})
            if route=='/jobs/command':
                pid=int(data['pid']);birth=psutil.Process(pid).create_time()
                command=data.get('command');cwd=data.get('cwd')
                if command is not None and (not isinstance(command,list) or not command or not all(isinstance(v,str) for v in command)):raise ValueError('invalid_command')
                if cwd is not None and not isinstance(cwd,str):raise ValueError('invalid_cwd')
                specification=json.dumps({'command':command,'cwd':cwd}).encode()
                identifier=store.submit('command',data.get('owner','local-job'),data.get('model','command'),'',specification,key=key,pid=pid,birth=birth,min_free=int(data.get('min_free_mb',8000)),context=data.get('context'),
                                        priority=data.get('priority',10),vram_mb=data.get('vram_mb'),cancel_capable=data.get('cancel_capable',False),repeatable=data.get('repeatable',False),oom_exit_code=data.get('oom_exit_code'))
                return self.respond(202,{'job_id':identifier})
            parts=route.strip('/').split('/')
            if len(parts)==3 and parts[0]=='jobs':
                identifier,action=parts[1:];r=store.row(identifier)
                if action=='cancel':store.cancel(identifier)
                elif action=='priority':store.set_priority(identifier,data['priority'])
                elif action=='heartbeat':store.update(identifier,heartbeat=time.time())
                elif action=='child':
                    with store.lock:
                        r=store.row(identifier)
                        if r['kind']!='command' or r['state']!='running' or r['child_pid']:raise ValueError('not_available_for_child')
                        p=psutil.Process(int(data['pid']))
                        if p.ppid()!=r['owner_pid']:raise ValueError('child_parent_mismatch')
                        store.update(identifier,child_pid=p.pid,child_birth=p.create_time(),heartbeat=time.time())
                elif action=='release':
                    with store.lock:
                        r=store.row(identifier)
                        released=store.release(identifier,int(data['exit_code']),child_alive=alive(r['child_pid'],r['child_birth']),tree_empty=data.get('tree_empty',False),attempt=data.get('attempt'))
                        return self.respond(200,{**store.status(identifier),**(released or {})})
                elif action=='ack-recovery':
                    if r['state']!='recovery_blocked' or data.get('backend_idle_confirmed') is not True:raise ValueError('idle_confirmation_required')
                    if r['kind']=='ollama':self.broker.unload(r['model'])
                    store.finish(identifier,'interrupted','operator_reconciled_no_automatic_replay')
                else:return self.respond(404,{'error':'unknown_action'})
                return self.respond(200,store.status(identifier))
            return self.respond(404,{'error':'not_found'})
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
        except (ValueError,KeyError,psutil.NoSuchProcess):self.respond(409,{'error':'invalid_request_or_transition'})
        except Exception:self.respond(503,{'error':'request_failed'})


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=11436);parser.add_argument('--state',type=Path,default=STATE)
    args=parser.parse_args()
    import win32event,win32api,winerror
    gpu_mutex=win32event.CreateMutex(None,False,'Global\\GPUqueue.Coordinator.GPU0')
    if win32api.GetLastError()==winerror.ERROR_ALREADY_EXISTS:
        raise SystemExit('GPU coordinator already running; no second worker started.')
    # Bind before opening/reconciling storage: a second service cannot mutate it.
    class ExclusiveServer(ThreadingHTTPServer):
        allow_reuse_address=False
        def server_bind(self):
            self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
            super().server_bind()
    http=ExclusiveServer(('127.0.0.1',args.port),Handler)
    http.daemon_threads=True;http.broker=Broker(args.state);http.broker.start()
    print(json.dumps({'service':'local-gpu-coordinator','port':args.port,'pid':os.getpid()}),flush=True)
    http.serve_forever(poll_interval=.5)

if __name__=='__main__':main()
