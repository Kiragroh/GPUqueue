"""Per-user coordinator watchdog. Never kills jobs, unloads models or clears recovery."""
import argparse
import ctypes
from ctypes import wintypes
import json
import logging
from logging.handlers import RotatingFileHandler
import msvcrt
import os
from pathlib import Path
import socket
import subprocess
import time
from urllib.parse import urlsplit

import psutil
import requests


def decision(healthy, occupied, process_alive):
    if healthy:
        return 'healthy'
    if occupied:
        return 'port_occupied'
    if process_alive:
        return 'process_unhealthy'
    return 'start'


def retry_delay(failures):
    return min(60, 2 ** min(6, max(0, failures) + 1))


def python_pids():
    # Toolhelp returns the executable basename without resolving remote image
    # paths. psutil.name() can hang here on unrelated disconnected UNC programs.
    class Entry(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('usage', wintypes.DWORD),
                    ('pid', wintypes.DWORD), ('heap', ctypes.c_size_t),
                    ('module', wintypes.DWORD), ('threads', wintypes.DWORD),
                    ('parent', wintypes.DWORD), ('priority', wintypes.LONG),
                    ('flags', wintypes.DWORD), ('exe', wintypes.WCHAR * 260)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = Entry()
        entry.size = ctypes.sizeof(entry)
        present = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while present:
            if entry.exe.lower() in ('python.exe', 'pythonw.exe'):
                yield entry.pid
            present = kernel.Process32NextW(handle, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(handle)


class Supervisor:
    def __init__(self, config):
        self.config = config
        self.root = Path(config['coordinator_root']).resolve()
        self.state = Path(config['state_dir']).resolve()
        self.state.mkdir(parents=True, exist_ok=True)
        self.url = config['url'].rstrip('/')
        parsed = urlsplit(self.url)
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.path or parsed.query or parsed.fragment or parsed.username:
            raise ValueError('Only a loopback HTTP endpoint is supported')
        self.port = parsed.port
        if not self.port:
            raise ValueError('Explicit port required')
        self.entries = {self.root/'server.py'}
        self.entries.update(Path(p).resolve()/'server.py' for p in config.get('legacy_roots', []))
        self.http_timeout = 2
        self.session = requests.Session()
        self.session.trust_env = False
        self.child = None
        self.lock = None
        self.starts = 0
        self.failures = 0
        self.next_start = 0
        self.last_status = None
        self.logger = logging.getLogger('watchdog.'+str(id(self)))
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.log_handler = RotatingFileHandler(self.state/'watchdog.log', maxBytes=1024*1024, backupCount=3, encoding='utf-8')
        self.log_handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        self.logger.addHandler(self.log_handler)

    def acquire(self):
        self.lock = (self.state/'watchdog.lock').open('a+b')
        if self.lock.seek(0, 2) == 0:
            self.lock.write(b'0')
            self.lock.flush()
        self.lock.seek(0)
        msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)

    def expected_process(self, pid):
        try:
            cmd = psutil.Process(int(pid)).cmdline()
            return len(cmd) > 1 and Path(cmd[1]).resolve() in self.entries
        except (psutil.Error, ValueError, TypeError, OSError):
            return False

    def existing_process(self):
        if self.child and self.child.poll() is None:
            return self.child.pid
        for pid in python_pids():
            try:
                cmd = psutil.Process(pid).cmdline()
                if len(cmd) > 1 and Path(cmd[1]).absolute() in self.entries:
                    return pid
            except (psutil.Error, OSError, ValueError):
                continue
        return None

    def health(self):
        try:
            response = self.session.get(self.url+'/health', timeout=self.http_timeout)
            response.raise_for_status()
            data = response.json()
            if (isinstance(data, dict) and data.get('service') == 'local-gpu-coordinator'
                    and data.get('worker_alive') is True and self.expected_process(data.get('pid'))):
                return data
        except (requests.RequestException, ValueError):
            pass
        return None

    def occupied(self):
        try:
            with socket.create_connection(('127.0.0.1', self.port), timeout=.5):
                return True
        except OSError:
            return False

    def launch(self):
        for name in ('server.stdout.log', 'server.stderr.log'):
            path = self.state/name
            if path.exists() and path.stat().st_size > 1024*1024:
                path.replace(self.state/(name+'.previous'))
        with (self.state/'server.stdout.log').open('ab') as out, (self.state/'server.stderr.log').open('ab') as err:
            self.child = subprocess.Popen(
                [self.config['python'], str(self.root/'server.py'), '--port', str(self.port), '--state', str(self.state)],
                cwd=self.root, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
        self.starts += 1

    def write_status(self, status, reason, broker_pid=None):
        value = dict(observed_at=time.time(), pid=os.getpid(), status=status, reason=reason,
                     broker_pid=broker_pid, starts=self.starts,
                     retry_in_seconds=max(0, round(self.next_start-time.monotonic())) if status in ('backoff', 'start_failed') else 0)
        temp = self.state/('watchdog.'+str(os.getpid())+'.tmp')
        temp.write_text(json.dumps(value), encoding='utf-8')
        os.replace(temp, self.state/'watchdog.json')
        if status != self.last_status:
            self.logger.info('status=%s reason=%s broker_pid=%s', status, reason, broker_pid)
            self.last_status = status
        return value

    def tick(self):
        health = self.health()
        if health:
            self.failures = 0
            self.next_start = 0
            return self.write_status('healthy', 'worker_responding', health['pid'])
        occupied = self.occupied()
        existing = None if occupied else self.existing_process()
        action = decision(False, occupied, bool(existing))
        if action != 'start':
            return self.write_status(action, 'no_automatic_termination', existing)
        if time.monotonic() < self.next_start:
            return self.write_status('backoff', 'restart_delayed')
        self.next_start = time.monotonic()+retry_delay(self.failures)
        self.failures += 1
        try:
            self.launch()
            return self.write_status('starting', 'waiting_for_health', self.child.pid)
        except (OSError, ValueError):
            self.logger.warning('Broker start failed; check installed interpreter and files')
            return self.write_status('start_failed', 'check_installation_and_server_log')

    def close(self):
        self.session.close()
        if self.lock:
            self.lock.close()
        self.log_handler.close()
        self.logger.removeHandler(self.log_handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('config.json'))
    args = parser.parse_args()
    sup = Supervisor(json.loads(args.config.read_text(encoding='utf-8-sig')))
    try:
        try:
            sup.acquire()
        except OSError:
            return 0
        while True:
            try:
                sup.tick()
            except Exception as exc:
                # Error type only: exceptions may embed sensitive response bodies.
                sup.logger.error('Watchdog tick failed: %s', type(exc).__name__)
            time.sleep(5)
    finally:
        sup.close()


if __name__ == '__main__':
    raise SystemExit(main())
