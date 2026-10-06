"""Conservative, non-preemptive admission policy. All values are MiB."""
import time

PRIORITIES={'background':0,'normal':10,'high':20}
RESERVE_MB=2048
MAX_GPU=2
MAX_CPU=1

def effective_priority(row, now=None):
    now=time.time() if now is None else now
    return min(20,row['priority']+10*max(0,int((now-row['created'])//900)))

def queue_order(row):
    return (-effective_priority(row),row['seq'])

def admission(row,active,snapshot):
    # A single model must not be unloaded while another request uses it.
    canonical=lambda name:name if ':' in name.rsplit('/',1)[-1] else name+':latest'
    if row['kind']=='ollama' and any(a['kind']=='ollama' and canonical(a['model'])==canonical(row['model']) for a in active):
        return 'model_in_use'
    same=[a for a in active if a['lane']==row['lane']]
    if any(a['state']=='recovery_blocked' for a in same):return 'lane_recovery_blocked'
    if row['lane']=='cpu':return 'cpu_lane_busy' if len(same)>=MAX_CPU else None
    if len(same)>=MAX_GPU:return 'gpu_slot_limit'
    exclusive=not row['vram_mb'] or row['retry_exclusive']
    if same and (exclusive or any(not a['vram_mb'] or a['retry_exclusive'] for a in same)):
        return 'exclusive_job_wait'
    if row['kind']=='ollama' and any(a['kind']=='ollama' for a in same):return 'ollama_gpu_limit'
    # The broker always supplies a snapshot (including a probe error). None is
    # retained only for legacy serial Store clients with no declared budget.
    if snapshot is not None and (not 0<=time.time()-snapshot.get('observed_at',0)<=8 or 'free_mb' not in snapshot):
        return 'fresh_gpu_probe_required'
    requirement=max(row['min_free'],(row['vram_mb'] or 0)+RESERVE_MB if row['vram_mb'] else 0)
    if snapshot is not None:requirement=max(requirement,RESERVE_MB)
    if requirement:
        if not snapshot or not 0<=time.time()-snapshot.get('observed_at',0)<=8 or 'free_mb' not in snapshot:
            return 'fresh_gpu_probe_required'
        # Deliberately subtract full reservations even if already visible in the
        # probe: double-accounting is safe; optimistic overlap is not.
        reserved=sum(a['vram_mb'] or 0 for a in same)
        if snapshot['free_mb']-reserved<requirement:return 'vram_budget_wait'
    return None
