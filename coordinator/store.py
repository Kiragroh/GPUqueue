"""Durable metadata; bodies and response chunks use current-user Windows DPAPI."""
import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
import win32crypt
from scheduling import admission, effective_priority, queue_order

TERMINAL={'completed','failed','cancelled','interrupted'}
ACTIVE={'running','preparing','draining','cancelling','recovery_blocked'}

def protect(value):
    return win32crypt.CryptProtectData(value,'GPUCoordinator',None,None,None,1)

def unprotect(value):
    return win32crypt.CryptUnprotectData(value,None,None,None,1)[1]

def label(value):
    return re.sub(r'[^A-Za-z0-9._ :/@+-]','_',str(value))[:100]

class Store:
    def __init__(self,path):
        self.lock=threading.RLock()
        self._context_cache=OrderedDict()
        self.db=sqlite3.connect(path,check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, seq INTEGER, kind TEXT, owner TEXT, model TEXT, route TEXT,
            payload BLOB, digest TEXT, idem TEXT UNIQUE, state TEXT, reason TEXT,
            created REAL, started REAL, ended REAL, heartbeat REAL,
            owner_pid INTEGER, owner_birth REAL, child_pid INTEGER, child_birth REAL,
            min_free INTEGER, http_status INTEGER, content_type TEXT, exit_code INTEGER);
          CREATE TABLE IF NOT EXISTS chunks (job TEXT,n INTEGER,body BLOB,PRIMARY KEY(job,n));''')
        if 'context' not in {r[1] for r in self.db.execute('pragma table_info(jobs)')}:
            self.db.execute('alter table jobs add column context BLOB');self.db.commit()
        self.db.execute('create table if not exists events(job TEXT,at REAL,state TEXT,reason TEXT)');self.db.commit()
        columns={r[1] for r in self.db.execute('pragma table_info(jobs)')}
        for name,definition in {
            'priority':'INTEGER NOT NULL DEFAULT 10','lane':"TEXT NOT NULL DEFAULT 'gpu'",
            'vram_mb':'INTEGER','cancel_requested':'INTEGER NOT NULL DEFAULT 0',
            'cancel_capable':'INTEGER NOT NULL DEFAULT 0','repeatable':'INTEGER NOT NULL DEFAULT 0',
            'oom_exit_code':'INTEGER','attempt':'INTEGER NOT NULL DEFAULT 0',
            'retry_exclusive':'INTEGER NOT NULL DEFAULT 0'}.items():
            if name not in columns:self.db.execute(f'alter table jobs add column {name} {definition}')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS attempts (
            job TEXT,number INTEGER,started REAL,ended REAL,exit_code INTEGER,state TEXT,
            PRIMARY KEY(job,number));
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
            INSERT OR IGNORE INTO settings VALUES('paused','0');
            CREATE INDEX IF NOT EXISTS events_job_at ON events(job,at);
            CREATE INDEX IF NOT EXISTS jobs_state_seq ON jobs(state,seq);''')
        self.db.commit()
        # Payloads may contain many MiB of encrypted images. Status/scheduling
        # must never materialize them; only payload()/specification need them.
        self._metadata_columns=','.join('"'+r[1]+'"' for r in self.db.execute('pragma table_info(jobs)') if r[1] not in {'payload','digest','idem'})

    def submit(self,kind,owner,model,route,payload,*,key=None,pid=0,birth=0,min_free=0,context=None,
               priority=10,lane='gpu',vram_mb=None,cancel_capable=False,repeatable=False,oom_exit_code=None):
        if kind not in {'ollama','command'}:raise ValueError('invalid_kind')
        if context is not None and (not isinstance(context,dict) or any(not isinstance(v,(str,type(None))) for v in context.values())):raise ValueError('invalid_context')
        if not isinstance(min_free,int) or min_free<0:raise ValueError('invalid_memory_requirement')
        if type(priority) is not int or priority not in {0,10,20}:raise ValueError('invalid_priority')
        if lane not in {'cpu','gpu'} or (lane=='cpu' and kind!='ollama'):raise ValueError('invalid_lane')
        if vram_mb is not None and (type(vram_mb) is not int or vram_mb<=0):raise ValueError('invalid_budget')
        if type(repeatable) is not bool or type(cancel_capable) is not bool:raise ValueError('invalid_capability')
        if repeatable and (kind!='command' or not cancel_capable or type(oom_exit_code) is not int or oom_exit_code==0):raise ValueError('invalid_retry_policy')
        if oom_exit_code is not None and not repeatable:raise ValueError('repeatable_required')
        digest=hashlib.sha256(route.encode()+b'\0'+payload).hexdigest()
        # Hash keys: callers may accidentally put identifying text in them.
        idem=hashlib.sha256(key.encode()).hexdigest() if key else None
        with self.lock,self.db:
            if idem:
                old=self.db.execute('select id,digest from jobs where idem=?',(idem,)).fetchone()
                if old:
                    if old['digest']!=digest:raise ValueError('idempotency_conflict')
                    return old['id']
            elif kind=='ollama':
                old=self.db.execute("select id from jobs where digest=? and owner=? and state in ('queued','running','preparing','draining') order by seq limit 1",(digest,label(owner))).fetchone()
                if old:return old['id']
            identifier=uuid.uuid4().hex
            seq=self.db.execute('select coalesce(max(seq),0)+1 from jobs').fetchone()[0]
            self.db.execute('''insert into jobs(id,seq,kind,owner,model,route,payload,digest,idem,state,reason,
                created,heartbeat,owner_pid,owner_birth,min_free) values(?,?,?,?,?,?,?,?,?,'queued','waiting_turn',?,?,?,?,?)''',
                (identifier,seq,kind,label(owner),label(model),route,protect(payload),digest,idem,time.time(),time.time(),pid,birth,min_free))
            self.db.execute('update jobs set context=? where id=?',(protect(json.dumps(context or {}).encode()),identifier))
            self.db.execute('update jobs set priority=?,lane=?,vram_mb=?,cancel_capable=?,repeatable=?,oom_exit_code=? where id=?',
                            (priority,lane,vram_mb,int(cancel_capable or kind=='ollama'),int(repeatable),oom_exit_code,identifier))
            self.db.execute('insert into events values(?,?,?,?)',(identifier,time.time(),'queued','accepted_durably'))
            return identifier

    def row(self,identifier):
        with self.lock:
            r=self.db.execute('select * from jobs where id=?',(identifier,)).fetchone()
            if r is None:raise KeyError('unknown_job')
            return dict(r)

    def update(self,identifier,**values):
        allowed={'state','reason','started','ended','heartbeat','child_pid','child_birth','http_status','content_type','exit_code',
                 'priority','cancel_requested','attempt','retry_exclusive'}
        if not values or not set(values)<=allowed:raise ValueError('invalid_update')
        with self.lock,self.db:
            before=self.row(identifier)
            self.db.execute('update jobs set '+','.join(k+'=?' for k in values)+' where id=?',(*values.values(),identifier))
            if any(k in values and values[k]!=before[k] for k in ['state','reason']):
                self.db.execute('insert into events values(?,?,?,?)',(identifier,time.time(),values.get('state',before['state']),values.get('reason',before['reason'])))

    def claim(self,snapshot=None):
        with self.lock,self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.paused():return None
            active=self.active_rows()
            queued=sorted([dict(r) for r in self.db.execute(f"select {self._metadata_columns} from jobs where state='queued'")],key=queue_order)
            for r in queued:
                reason=admission(r,active,snapshot)
                if reason:
                    self.update(r['id'],reason=reason);continue
                self.update(r['id'],state='running',reason='starting',attempt=r['attempt']+1)
                self.db.execute('insert into attempts values(?,?,?,NULL,NULL,?)',(r['id'],r['attempt']+1,time.time(),'running'))
                return self.row(r['id'])
            return None

    def set_priority(self,identifier,priority):
        if type(priority) is not int or priority not in {0,10,20}:raise ValueError('invalid_priority')
        with self.lock,self.db:
            if self.row(identifier)['state']!='queued':raise ValueError('only_queued_priority')
            self.update(identifier,priority=priority,reason='priority_changed')

    def paused(self):
        with self.lock:return self.db.execute("select value from settings where key='paused'").fetchone()[0]=='1'

    def set_paused(self,paused):
        if type(paused) is not bool:raise ValueError('boolean_required')
        with self.lock,self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute("update settings set value=? where key='paused'",('1' if paused else '0',))
            return {'paused':paused,'idle':not self.active_rows()}

    def payload(self,identifier):return unprotect(self.row(identifier)['payload'])

    def chunk(self,identifier,body):
        with self.lock,self.db:
            n=self.db.execute('select coalesce(max(n),-1)+1 from chunks where job=?',(identifier,)).fetchone()[0]
            self.db.execute('insert into chunks values(?,?,?)',(identifier,n,protect(body)))

    def chunks(self,identifier,start=0):
        with self.lock:
            rows=self.db.execute('select body from chunks where job=? and n>=? order by n',(identifier,start)).fetchall()
        return [unprotect(r[0]) for r in rows]

    def finish(self,identifier,state,reason='',code=None):
        if state not in TERMINAL:raise ValueError('invalid_terminal_state')
        with self.lock,self.db:
            self.update(identifier,state=state,reason=reason,ended=time.time(),exit_code=code)
            self.db.execute('update attempts set ended=?,exit_code=?,state=? where job=? and ended is NULL',(time.time(),code,state,identifier))

    def cancel(self,identifier):
        with self.lock,self.db:
            row=self.row(identifier)
            if row['state']=='queued':
                self.update(identifier,cancel_requested=1)
                self.finish(identifier,'cancelled','cancelled_by_owner')
            elif row['state'] in ACTIVE and row['state']!='recovery_blocked' and row['cancel_capable']:
                self.update(identifier,cancel_requested=1,state='cancelling',reason='cancel_pending_tree_exit' if row['kind']=='command' else 'cancel_pending_backend_drain')
            else:raise ValueError('active_cancellation_unsupported_or_terminal')

    def release(self,identifier,code,*,child_alive,tree_empty=False,attempt=None):
        with self.lock:
            row=self.row(identifier)
            if row['kind']=='command' and row['state']=='cancelled' and attempt is not None:
                previous=self.db.execute('select state,exit_code,ended from attempts where job=? and number=?',(identifier,attempt)).fetchone()
                if previous and previous['state']=='oom_failed' and previous['exit_code']==code and previous['ended'] is not None:
                    return {'terminal_ack':True}
            if attempt is not None and attempt<row['attempt']:
                previous=self.db.execute('select state,exit_code from attempts where job=? and number=?',(identifier,attempt)).fetchone()
                if previous and previous['state']=='oom_failed' and previous['exit_code']==code:return {'retry_scheduled':True}
                raise ValueError('stale_attempt')
            if row['kind']=='command' and row['state'] in {'completed','failed','cancelled'} and row['exit_code']==code:
                return
            if attempt is not None and attempt!=row['attempt']:raise ValueError('stale_attempt')
            if row['kind']=='command' and row['state']=='queued' and row['retry_exclusive'] and row['exit_code']==code:return {'retry_scheduled':True}
            if row['kind']!='command' or row['state'] not in {'running','cancelling'}:raise ValueError('not_active_command')
            if child_alive:raise ValueError('child_still_alive')
            if row['cancel_capable'] and tree_empty is not True:raise ValueError('tree_exit_confirmation_required')
            if row['cancel_requested']:
                self.finish(identifier,'cancelled','owned_tree_exit_confirmed',code);return
            if row['repeatable'] and code==row['oom_exit_code'] and row['attempt']==1 and tree_empty:
                with self.db:
                    self.db.execute('update attempts set ended=?,exit_code=?,state=? where job=? and number=?',(time.time(),code,'oom_failed',identifier,row['attempt']))
                    self.update(identifier,state='queued',reason='oom_retry_exclusive',retry_exclusive=1,child_pid=None,child_birth=None,started=None,exit_code=code)
                return {'retry_scheduled':True}
            self.finish(identifier,'completed' if code==0 else 'failed','command_exit',code)

    def recover(self):
        with self.lock,self.db:
            rows=list(self.db.execute("select * from jobs where state in ('running','preparing','draining','cancelling')"))
            for r in rows:
                if r['kind']=='ollama':self.update(r['id'],state='recovery_blocked',reason='broker_restart_backend_state_uncertain')
                elif r['child_pid'] is None and (r['state']=='preparing' or r['reason']=='starting'):
                    self.update(r['id'],state='queued',reason='restart_before_child_launch',started=None)

    def status(self,identifier,private=False):
        with self.lock:
            r=self.db.execute(f'select {self._metadata_columns} from jobs where id=?',(identifier,)).fetchone()
            if r is None:raise KeyError('unknown_job')
            waiting=sorted([dict(x) for x in self.db.execute(f"select {self._metadata_columns} from jobs where state='queued'")],key=queue_order)
            events=[dict(e) for e in self.db.execute('select at,state,reason from events where job=? order by at',(identifier,))] if private else []
            attempts=[dict(e) for e in self.db.execute('select number,started,ended,exit_code,state from attempts where job=? order by number',(identifier,))] if private else []
            return self._render_status(dict(r),private,waiting,self.active_rows(),events,attempts)

    def _context(self,encrypted):
        if not encrypted:return {}
        if encrypted not in self._context_cache:
            self._context_cache[encrypted]=json.loads(unprotect(encrypted))
            if len(self._context_cache)>512:self._context_cache.popitem(last=False)
        self._context_cache.move_to_end(encrypted)
        # Never let a caller alter cached private context. Cache is process-only,
        # bounded, and keyed by ciphertext so changed records cannot reuse it.
        return dict(self._context_cache[encrypted])

    def _render_status(self,r,private,waiting,active,events,attempts):
        visible={k:v for k,v in r.items() if k not in {'payload','digest','idem','owner_birth','child_birth','route','context'}}
        context=self._context(r['context'])
        visible['script']=context.get('script_name','not_supplied')
        visible['effective_priority']=effective_priority(r, now=(r['started'] or r['ended']) if r['state']!='queued' else None)
        visible['budget_source']='declared' if r['vram_mb'] else 'unknown_exclusive'
        if private:visible.update(context=context,events=events,attempts=attempts)
        visible['wait_seconds']=round((r['started'] or r['ended'] or time.time())-r['created'],1)
        visible['run_seconds']=round((r['ended'] or time.time())-r['started'],1) if r['started'] else None
        before=[x for x in active if x['lane']==r['lane'] or (x['kind']=='ollama' and x['model']==r['model'])]
        before += [x for x in waiting if x['lane']==r['lane'] and queue_order(x)<queue_order(r)]
        visible['jobs_ahead']=len(before) if r['state']=='queued' else 0
        visible['queue_position']=len(before)+1 if r['state']=='queued' else None
        visible['waiting_for']=[{k:x[k] for k in ('id','owner','model','state','reason','lane')} for x in before if x['id']!=r['id']] if r['state']=='queued' else []
        visible['heartbeat_age_s']=round(time.time()-r['heartbeat'],1)
        return visible

    def listing(self,*,private=False,offset=0,limit=30,lane=None):
        if lane not in {None,'cpu','gpu'}:raise ValueError('invalid_lane')
        with self.lock:
            active=self.active_rows()
            waiting=sorted([dict(r) for r in self.db.execute(f"select {self._metadata_columns} from jobs where state='queued'")],key=queue_order)
            lane_sql=" and coalesce(lane,'gpu')=?" if lane else ''
            parameters=((lane,) if lane else ())+(min(200,limit),offset)
            history=[dict(r) for r in self.db.execute(f"select {self._metadata_columns} from jobs where state in ('completed','failed','cancelled','interrupted'){lane_sql} order by seq desc limit ? offset ?",parameters)]
            rows=[r for r in active+waiting+history if lane is None or (r.get('lane') or 'gpu')==lane];events={};attempts={}
            if private and rows:
                ids=[r['id'] for r in rows];marks=','.join('?' for _ in ids)
                for event in self.db.execute(f'select job,at,state,reason from events where job in ({marks}) order by at',ids):
                    event=dict(event);events.setdefault(event.pop('job'),[]).append(event)
                for attempt in self.db.execute(f'select job,number,started,ended,exit_code,state from attempts where job in ({marks}) order by number',ids):
                    attempt=dict(attempt);attempts.setdefault(attempt.pop('job'),[]).append(attempt)
            return [self._render_status(r,private,waiting,active,events.get(r['id'],[]),attempts.get(r['id'],[])) for r in rows]

    def active_rows(self):
        with self.lock:
            return [dict(r) for r in self.db.execute(f"select {self._metadata_columns} from jobs where state in ('running','preparing','draining','cancelling','recovery_blocked') order by seq")]
