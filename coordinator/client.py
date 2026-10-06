"""Local queue client and Windows GPU-job supervisor. No shell=True."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import uuid
import webbrowser
from scheduling import PRIORITIES

URL='http://127.0.0.1:11436'
STATE=Path(os.environ.get('LOCALAPPDATA',str(Path.home()))) / 'GPUqueue/state'

def request(path,data=None,*,key=None):
    token=(STATE/'control.token').read_text().strip()
    headers={'X-GPU-Token':token}
    if key:headers['Idempotency-Key']=key
    payload=None if data is None else json.dumps(data).encode()
    if payload is not None:headers['Content-Type']='application/json'
    with urllib.request.urlopen(urllib.request.Request(URL+path,data=payload,headers=headers),timeout=15) as r:
        return json.loads(r.read())

def supervised(args):
    import win32api,win32con,win32event,win32job,win32process
    command=args.command
    if command and command[0]=='--':command=command[1:]
    if not command:raise ValueError('command_required')
    script=args.script or next((v for v in command if v.lower().endswith('.py')),Path(command[0]).name)
    path=Path(script)
    context={'script':str(path.resolve()) if path.is_file() else script,'script_name':path.name,
             'chat_id':args.chat or os.getenv('CODEX_THREAD_ID',''),'case_id':args.case_id,
             'script_sha256':hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None}
    result=request('/jobs/command',{'pid':os.getpid(),'owner':args.owner,'model':args.model,
        'min_free_mb':args.min_free_mb,'context':context,'command':command,'cwd':os.getcwd(),
        'priority':PRIORITIES[args.priority],'vram_mb':args.vram_mb,'cancel_capable':True,
        'repeatable':args.repeatable,'oom_exit_code':args.oom_exit_code},key=uuid.uuid4().hex)
    identifier=result['job_id'];print(json.dumps({'gpu_job':identifier}),flush=True)
    child=None;job_handle=None;last=None;attempt=None
    def end_owned_tree(handle):
        # The root has exited or this user cancelled. Only this wrapper's
        # Windows Job Object is terminated; wait until all owned children exit.
        win32job.TerminateJobObject(handle,130)
        while win32job.QueryInformationJobObject(handle,win32job.JobObjectBasicAccountingInformation)['ActiveProcesses']:
            time.sleep(.1)
        handle.Close()
    def release(code):
        while True:
            try:return request('/jobs/'+identifier+'/release',{'exit_code':code,'tree_empty':True,'attempt':attempt})
            except Exception:
                # A committed release can lose its HTTP reply. Only an exact,
                # durably ended attempt plus terminal job state acknowledges
                # cleanup. Running/recovery-blocked states remain reserved.
                try:
                    current=request('/jobs/'+identifier)
                    acknowledged=any(a['number']==attempt and a['exit_code']==code and a['ended'] is not None for a in current.get('attempts',[]))
                    if current['state'] in {'completed','failed','cancelled'} and acknowledged:return current
                except Exception:pass
                print(json.dumps({'gpu_job':identifier,'state':'exit_record_pending_broker'}),flush=True);time.sleep(5)
    def launch_attempt():
        nonlocal child,job_handle
        env=os.environ.copy();env['GPU_COORDINATOR_JOB']=identifier
        job_handle=win32job.CreateJobObject(None,'GPUqueue.Job.'+identifier)
        info=win32job.QueryInformationJobObject(job_handle,win32job.JobObjectExtendedLimitInformation)
        info['BasicLimitInformation']['LimitFlags']=win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job_handle,win32job.JobObjectExtendedLimitInformation,info)
        startup=win32process.STARTUPINFO()
        startup.dwFlags|=win32process.STARTF_USESTDHANDLES
        startup.hStdInput=win32api.GetStdHandle(win32api.STD_INPUT_HANDLE)
        startup.hStdOutput=win32api.GetStdHandle(win32api.STD_OUTPUT_HANDLE)
        startup.hStdError=win32api.GetStdHandle(win32api.STD_ERROR_HANDLE)
        hp,ht,pid,tid=win32process.CreateProcess(None,subprocess.list2cmdline(command),None,None,True,
            win32process.CREATE_SUSPENDED|win32process.CREATE_NEW_PROCESS_GROUP,env,os.getcwd(),startup)
        child=hp
        try:
            win32job.AssignProcessToJobObject(job_handle,hp)
            registered=request('/jobs/'+identifier+'/child',{'pid':pid})
            if registered.get('cancel_requested'):return 130
            win32process.ResumeThread(ht)
        except BaseException as error:
            # It is our suspended child, never an unrelated process. The broker
            # may have recorded cancellation between grant and registration.
            win32process.TerminateProcess(hp,130)
            if isinstance(error,KeyboardInterrupt):raise
            current=request('/jobs/'+identifier+'/heartbeat',{})
            if current.get('cancel_requested'):return 130
            raise
        finally:ht.Close()
        while win32event.WaitForSingleObject(hp,1000)==win32event.WAIT_TIMEOUT:
            try:state=request('/jobs/'+identifier+'/heartbeat',{})
            except Exception:
                # A disconnected supervisor keeps its tree and reservation.
                continue
            if state.get('cancel_requested'):return 130
        return win32process.GetExitCodeProcess(hp)
    try:
        while True:
            try:state=request('/jobs/'+identifier+'/heartbeat',{})
            except (OSError,TimeoutError):
                print(json.dumps({'gpu_job':identifier,'state':'waiting_for_broker_reconnect'}),flush=True)
                time.sleep(5);continue
            status=(state['state'],state['jobs_ahead'],state['reason'])
            if status!=last:
                print(json.dumps({'gpu_job':identifier,'state':status[0],'jobs_ahead':status[1],'reason':status[2]}),flush=True);last=status
            attempt=state.get('attempt')
            if state['state']=='cancelling':release(130);return 130
            if state['state']=='cancelled':return 130
            if state['state']=='running' and state['reason']!='starting':
                try:code=launch_attempt()
                finally:
                    if job_handle:end_owned_tree(job_handle);job_handle=None
                    if child:
                        win32event.WaitForSingleObject(child,5000);child.Close();child=None
                outcome=release(code)
                if outcome.get('retry_scheduled') or outcome['state']=='queued':continue
                return 130 if outcome['state']=='cancelled' else code
            if state['state'] in {'failed','cancelled','interrupted','recovery_blocked'}:
                raise RuntimeError('job_not_granted')
            time.sleep(1)
    except KeyboardInterrupt:
        cancel_state=None
        try:cancel_state=request('/jobs/'+identifier+'/cancel',{})
        except Exception:pass
        if job_handle:end_owned_tree(job_handle);job_handle=None
        if child:
            win32event.WaitForSingleObject(child,5000)
        if cancel_state and cancel_state['state']=='cancelling':
            try:release(130)
            except Exception:pass
        return 130
    finally:
        if job_handle:job_handle.Close()
        if child:child.Close()

def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='action',required=True)
    sub.add_parser('status')
    opening=sub.add_parser('open');opening.add_argument('--control',action='store_true')
    h=sub.add_parser('history');h.add_argument('--offset',type=int,default=0)
    get=sub.add_parser('job');get.add_argument('id')
    cancel=sub.add_parser('cancel');cancel.add_argument('id')
    priority=sub.add_parser('priority');priority.add_argument('id');priority.add_argument('level',choices=PRIORITIES)
    drain=sub.add_parser('drain');drain.add_argument('--resume',action='store_true')
    recover=sub.add_parser('recover');recover.add_argument('id');recover.add_argument('--confirmed-idle',action='store_true',required=True)
    result=sub.add_parser('result');result.add_argument('id');result.add_argument('--output',type=Path,required=True)
    spec=sub.add_parser('specification');spec.add_argument('id');spec.add_argument('--output',type=Path,required=True)
    submit=sub.add_parser('submit');submit.add_argument('--route',default='/api/generate');submit.add_argument('--json-file',type=Path,required=True);submit.add_argument('--owner',default='local-client');submit.add_argument('--key',required=True);submit.add_argument('--script',required=True);submit.add_argument('--case-id');submit.add_argument('--chat')
    run=sub.add_parser('run');run.add_argument('--owner',default='research');run.add_argument('--model',default='GPU job');run.add_argument('--script');run.add_argument('--case-id');run.add_argument('--chat');run.add_argument('--min-free-mb',type=int,default=8000);run.add_argument('command',nargs=argparse.REMAINDER)
    for parser in [submit,run]:
        parser.add_argument('--priority',choices=PRIORITIES,default='normal');parser.add_argument('--vram-mb',type=int)
    run.add_argument('--repeatable',action='store_true');run.add_argument('--oom-exit-code',type=int)
    a=p.parse_args()
    if a.action=='run':raise SystemExit(supervised(a))
    elif a.action=='open':
        ticket=request('/ui-ticket',{'mode':'control' if a.control else 'read'})['ticket']
        webbrowser.open(URL+'/login?ticket='+ticket)
    elif a.action=='status':
        # Local status includes the bound case ID, never rich context or payloads.
        with urllib.request.urlopen(URL+'/status',timeout=15) as response:
            print(json.dumps(json.load(response),indent=2))
    elif a.action=='history':print(json.dumps(request('/history?offset='+str(a.offset)),indent=2))
    elif a.action=='job':print(json.dumps(request('/jobs/'+a.id),indent=2))
    elif a.action=='cancel':print(json.dumps(request('/jobs/'+a.id+'/cancel',{})))
    elif a.action=='priority':print(json.dumps(request('/jobs/'+a.id+'/priority',{'priority':PRIORITIES[a.level]})))
    elif a.action=='drain':print(json.dumps(request('/drain',{'paused':not a.resume})))
    elif a.action=='recover':print(json.dumps(request('/jobs/'+a.id+'/ack-recovery',{'backend_idle_confirmed':a.confirmed_idle})))
    elif a.action=='specification':
        data=request('/jobs/'+a.id+'/specification')
        with a.output.open('x',encoding='utf8') as f:json.dump(data,f,indent=2)
    elif a.action=='submit':
        path=Path(a.script)
        context={'script':str(path.resolve()) if path.is_file() else a.script,'script_name':path.name,'case_id':a.case_id,'chat_id':a.chat or os.getenv('CODEX_THREAD_ID',''),'script_sha256':hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None}
        print(json.dumps(request('/jobs/ollama',{'route':a.route,'body':json.loads(a.json_file.read_text()),'owner':a.owner,'context':context,'priority':PRIORITIES[a.priority],'vram_mb':a.vram_mb},key=a.key)))
    elif a.action=='result':
        req=urllib.request.Request(URL+'/jobs/'+a.id+'/result',headers={'X-GPU-Token':(STATE/'control.token').read_text().strip()})
        with urllib.request.urlopen(req,timeout=600) as r,a.output.open('xb') as f:
            while chunk:=r.read(65536):f.write(chunk)

if __name__=='__main__':
    try:main()
    except Exception as error:
        print(json.dumps({'error':type(error).__name__,'hint':'Check local broker status; no direct GPU fallback.'}),file=sys.stderr)
        raise SystemExit(1)
