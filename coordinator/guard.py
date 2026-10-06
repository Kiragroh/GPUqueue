"""Import before CUDA initialization in research entry points."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

CLIENT=Path(__file__).with_name('client.py')
PYTHON=Path(os.environ.get('GPUQUEUE_PYTHON', sys.executable))

def ensure_managed(model,*,min_free_mb=8000,case_id=None):
    identifier=os.getenv('GPU_COORDINATOR_JOB')
    if identifier:
        state=Path(os.environ['LOCALAPPDATA'])/'GPUqueue/state'
        req=urllib.request.Request('http://127.0.0.1:11436/jobs/'+identifier,headers={'X-GPU-Token':(state/'control.token').read_text().strip()})
        with urllib.request.urlopen(req,timeout=10) as r:job=json.loads(r.read())
        if job['state']!='running' or job['child_pid']!=os.getpid():raise RuntimeError('GPU lease does not belong to this process')
        return
    command=[str(PYTHON),str(CLIENT),'run','--owner','local-worker','--model',model,'--script',str(Path(sys.argv[0]).resolve()),'--min-free-mb',str(min_free_mb)]
    if case_id is not None:command+=['--case-id',str(case_id)]
    command+=['--',sys.executable,*sys.argv]
    raise SystemExit(subprocess.call(command))
