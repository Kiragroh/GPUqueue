"""Minimal integration example. No CUDA dependency or inference by default."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'coordinator'))
from guard import ensure_managed

ensure_managed('example-worker', min_free_mb=4000)
# Import torch / initialize CUDA here, after the lease is confirmed.
print('Lease verified. Add your actual workload here.')
