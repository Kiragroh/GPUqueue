import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from supervisor import Supervisor, decision, retry_delay


def test_decision_preserves_existing_work():
    assert decision(True, True, True) == 'healthy'
    assert decision(False, True, False) == 'port_occupied'
    assert decision(False, False, True) == 'process_unhealthy'
    assert decision(False, False, False) == 'start'


def test_backoff_is_bounded():
    assert [retry_delay(i) for i in (0, 1, 2, 8, 100)] == [2, 4, 8, 60, 60]


@pytest.fixture
def isolated(tmp_path):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    root = tmp_path/'broker'
    root.mkdir()
    (root/'server.py').write_text('''import argparse,json,os
from http.server import BaseHTTPRequestHandler,HTTPServer
p=argparse.ArgumentParser();p.add_argument('--port',type=int);p.add_argument('--state');a=p.parse_args()
class H(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_GET(self):
  body=json.dumps(dict(service='local-gpu-coordinator',worker_alive=True,pid=os.getpid())).encode()
  self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
HTTPServer(('127.0.0.1',a.port),H).serve_forever()
''')
    config = dict(python=sys.executable, coordinator_root=str(root), state_dir=str(tmp_path/'private'), url=f'http://127.0.0.1:{port}')
    sup = Supervisor(config)
    yield sup
    if sup.child and sup.child.poll() is None:
        sup.child.terminate()
        sup.child.wait(timeout=5)
    sup.close()


def wait_healthy(sup):
    deadline = time.monotonic()+10
    while time.monotonic() < deadline:
        result = sup.tick()
        if result['status'] == 'healthy':
            return result
        time.sleep(.1)
    pytest.fail('isolated broker did not become healthy')


def test_real_crash_restarts_only_isolated_child(isolated):
    sup = isolated
    first = wait_healthy(sup)
    old = sup.child
    old.terminate()
    old.wait(timeout=5)
    second = wait_healthy(sup)
    assert second['broker_pid'] != first['broker_pid']
    assert second['starts'] == 2
    status = json.loads((sup.state/'watchdog.json').read_text())
    assert status['status'] == 'healthy'
    assert 'token' not in json.dumps(status)


def test_occupied_unknown_port_is_never_replaced(isolated):
    sup = isolated
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', sup.port))
        sock.listen()
        sup.http_timeout = .1
        result = sup.tick()
        assert result['status'] == 'port_occupied'
        assert sup.child is None


def test_live_expected_process_without_http_is_not_killed(isolated):
    sup = isolated
    (sup.root/'server.py').write_text('import time; time.sleep(30)')
    assert sup.tick()['status'] == 'starting'
    time.sleep(.1)
    pid = sup.child.pid
    assert sup.tick()['status'] == 'process_unhealthy'
    assert sup.child.poll() is None
    assert sup.child.pid == pid


def test_unhealthy_worker_does_not_trigger_restart(isolated):
    sup = isolated
    script = sup.root/'server.py'
    script.write_text(script.read_text().replace('worker_alive=True', 'worker_alive=False'))
    sup.tick()
    time.sleep(.5)
    assert sup.tick()['status'] == 'port_occupied'
    assert sup.child.poll() is None
    assert sup.starts == 1


def test_duplicate_supervisor_lock(isolated):
    sup = isolated
    sup.acquire()
    other = Supervisor(sup.config)
    try:
        with pytest.raises(OSError):
            other.acquire()
    finally:
        other.close()
