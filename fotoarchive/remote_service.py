"""Loopback-only authenticated service. The watchdog never imports CUDA."""
import argparse
import hmac
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .remote_protocol import MAX_PACKED
from .remote_store import Store

LEASE_SECONDS = 20


def gpu_state(owned_group=None, passive_pids=()):
    def query(kind, columns):
        result = subprocess.run(['nvidia-smi',f'--query-{kind}={columns}','--format=csv,noheader,nounits'],
                                capture_output=True,text=True,timeout=3,check=True)
        return [line.split(', ') for line in result.stdout.strip().splitlines() if line.strip()]
    gpu = query('gpu','utilization.gpu,memory.used,memory.total')[0]
    foreign = []
    for pid, name, memory in query('compute-apps','pid,process_name,used_memory'):
        pid = int(pid)
        try:
            if owned_group and os.getpgid(pid) == owned_group:
                continue
        except ProcessLookupError:
            continue
        # Only the two explicitly inventoried small resident services may stay.
        # A different/new process always takes priority, even before high load.
        if pid in passive_pids and float(memory) < 1024:
            continue
        foreign.append(dict(pid=pid,memory_mb=float(memory)))
    return dict(utilization=int(gpu[0]),memory_mb=int(gpu[1]),total_mb=int(gpu[2]),foreign=foreign)


class Supervisor:
    def __init__(self, directory, passive_pids=(), clock=time.monotonic, probe=gpu_state):
        self.directory = Path(directory)
        self.store = Store(self.directory/'cache')
        self.passive_pids, self.clock, self.probe = passive_pids, clock, probe
        self.lock = threading.RLock()
        self.session = ''
        self.deadline = 0
        self.state = 'disconnected'
        self.gpu = {}
        self.worker = None
        self.messages = queue.Queue(maxsize=4)
        self.current = None
        self.idle_since = self.clock()
        self.completed = 0
        self.started = self.clock()
        self.last_error = ''
        self.retry_at = 0

    def lease(self, session, active):
        if not isinstance(session,str) or not 16 <= len(session) <= 80 or not isinstance(active,bool):
            raise ValueError('Invalid lease')
        with self.lock:
            if self.session != session and self.clock() < self.deadline:
                raise ValueError('Another app session is connected')
            self.session = session
            self.deadline = self.clock()+LEASE_SECONDS if active else 0
            if not active:
                self.stop()
                self.state = 'paused'
            return self.status()

    def require_lease(self, session):
        if session != self.session or self.clock() >= self.deadline:
            raise ValueError('App lease expired')

    def status(self):
        return dict(state=self.state,gpu=self.gpu,lease_seconds=LEASE_SECONDS,error=self.last_error,
                    rate=self.completed*60/max(1,self.clock()-self.started),**self.store.stats(self.session))

    def start(self):
        env = dict(os.environ, FOTOARCHIVE_REMOTE_DATA=str(self.directory))
        log = (self.directory/'worker.log').open('wb')
        try:
            self.worker = subprocess.Popen([sys.executable,'-m','fotoarchive.remote_worker'],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log,text=True,bufsize=1,
                start_new_session=True,env=env)
        finally:
            log.close()
        process = self.worker
        def read():
            for line in process.stdout:
                if line.startswith('RESULT\t'):
                    try:
                        self.messages.put((process.pid,json.loads(line[7:])),timeout=1)
                    except (ValueError,queue.Full):
                        pass
        threading.Thread(target=read,daemon=True).start()

    def stop(self):
        if self.worker:
            # Only our freshly created process group is addressed. Kill also
            # removes the llama child and every CUDA allocation in the group.
            try:
                os.killpg(self.worker.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.worker.wait(timeout=5)
            self.worker.stdin.close()
            self.worker = None
        self.current = None
        self.store.reset()

    def tick(self):
        with self.lock:
            now = self.clock()
            if now >= self.deadline:
                if self.worker:
                    self.stop()
                self.state = 'disconnected'
                return
            try:
                self.gpu = self.probe(self.worker.pid if self.worker else None,self.passive_pids)
            except Exception:
                self.stop()
                self.state = 'gpu_unavailable'
                return
            if self.gpu['foreign'] or (not self.worker and self.gpu['utilization'] > 20):
                self.stop()
                self.state = 'waiting_training'
                return
            if self.worker and self.worker.poll() is not None:
                self.stop()
                self.retry_at = now+60
                self.last_error = 'Модуль GPU остановился; повтор через минуту'
            while not self.messages.empty():
                pid, result = self.messages.get_nowait()
                if self.worker and pid == self.worker.pid and self.current and result['id'] == self.current['id']:
                    self.store.finish(result['id'], result)
                    self.current = None
                    self.completed += bool(result.get('stages'))
                    self.idle_since = now
                    self.last_error = '; '.join(result.get('errors',{}).values())[:500]
                    if result.get('errors') and not result.get('stages'):
                        self.stop()
                        self.retry_at = now+60
            if now < self.retry_at:
                self.state = 'retrying'
                return
            if not self.current:
                job = self.store.take(self.session)
                if job:
                    if not self.worker:
                        self.start()
                        self.started = now
                        self.completed = 0
                    self.current = job
                    self.worker.stdin.write(json.dumps(job)+'\n')
                    self.worker.stdin.flush()
                elif self.worker and now-self.idle_since > 10:
                    self.stop()
            self.state = 'working' if self.current else 'ready'


def serve(supervisor, token, host='127.0.0.1', port=18765):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # No tokens, photo contents or original filenames in logs.

        def handle_request(self):
            if not hmac.compare_digest(self.headers.get('Authorization',''), 'Bearer '+token):
                self.send_error(401)
                return
            try:
                self.connection.settimeout(15)
                path = self.path.split('?')[0].strip('/').split('/')
                length = int(self.headers.get('Content-Length','0'))
                maximum = MAX_PACKED if path[0]=='blobs' else 16384
                if not 0 <= length <= maximum:
                    raise ValueError('Request too large')
                data = self.rfile.read(length) if length else b''
                if len(data) != length:
                    raise ValueError('Incomplete request')
                body = json.loads(data) if data and path[0]!='blobs' else {}
                if self.command=='POST' and path==['lease']:
                    result = supervisor.lease(body['session'],body['active'])
                elif self.command=='GET' and path==['status']:
                    with supervisor.lock:
                        result = supervisor.status()
                elif len(path)==2 and path[0]=='blobs' and self.command in ('HEAD','PUT'):
                    if self.command=='PUT':
                        supervisor.store.put(path[1],data)
                    if not supervisor.store.has(path[1]):
                        raise FileNotFoundError()
                    result = {'cached':True}
                elif self.command=='POST' and path==['jobs']:
                    with supervisor.lock:
                        supervisor.require_lease(body['session'])
                        key = supervisor.store.submit(body['session'],body['blob'],body['stages'],body.get('context'))
                    result = dict(key=key)
                elif self.command=='GET' and len(path)==2 and path[0]=='jobs':
                    result = supervisor.store.result(path[1])
                else:
                    self.send_error(404)
                    return
                encoded = json.dumps(result,ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(encoded)))
                self.end_headers()
                if self.command!='HEAD':
                    self.wfile.write(encoded)
            except FileNotFoundError:
                self.send_error(404)
            except (ValueError,KeyError,TypeError):
                self.send_error(400)
            except (OSError,TimeoutError):
                self.close_connection = True
        do_GET = do_POST = do_PUT = do_HEAD = handle_request

    server = ThreadingHTTPServer((host,port),Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data',required=True)
    parser.add_argument('--token-file',required=True)
    parser.add_argument('--passive-pids',default='')
    args = parser.parse_args()
    supervisor = Supervisor(args.data,tuple(int(x) for x in args.passive_pids.split(',') if x))
    server = serve(supervisor,Path(args.token_file).read_text().strip())
    def watchdog():
        while True:
            try:
                supervisor.tick()
            except Exception as exc:
                with supervisor.lock:
                    supervisor.stop()
                    supervisor.retry_at = time.monotonic()+60
                    supervisor.last_error = type(exc).__name__
            time.sleep(1)
    threading.Thread(target=watchdog,daemon=True).start()
    try:
        server.serve_forever(poll_interval=.5)
    finally:
        supervisor.stop()


if __name__=='__main__':
    main()
