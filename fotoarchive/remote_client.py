"""Bounded SSH transport. Lease renewal is independent of decoding and uploads."""
import io
import json
import os
import queue
import secrets
import socket
import subprocess
import threading
import time
from pathlib import Path
import requests
from .media import open_rgb, visual_path
from .remote_protocol import digest, pack, job_key, MAX_INPUT


def prepare(asset, cfg):
    path = Path(asset['path'])
    before = path.stat()
    if (before.st_size,before.st_mtime_ns)!=(asset['size'],asset['mtime_ns']):
        raise ValueError('Файл изменился перед отправкой')
    source = visual_path(asset,cfg)
    if source.suffix.lower() in {'.jpg','.jpeg','.png','.webp'}:
        if source.stat().st_size>MAX_INPUT:
            raise ValueError('Снимок слишком велик для серверного кэша')
        data = source.read_bytes()
    else:
        # Lossless container conversion preserves exactly the pixels used by
        # local inference, while avoiding huge BMP/RAW/video transfers.
        stream = io.BytesIO()
        open_rgb(source).save(stream,format='WEBP',lossless=True,method=1)
        data = stream.getvalue()
    after = path.stat()
    if (after.st_size,after.st_mtime_ns)!=(before.st_size,before.st_mtime_ns):
        raise ValueError('Файл изменился во время отправки')
    return pack(data)


class Transport:
    def __init__(self, cfg, alive, emit):
        self.cfg, self.alive, self.emit = cfg, alive, emit
        self.active = False
        self.closed = threading.Event()
        self.connected = threading.Event()
        self.queue = queue.Queue(maxsize=128)
        self.results = queue.Queue(maxsize=256)
        self.session_id = secrets.token_hex(16)
        self.base = ''
        self.tunnel = None
        self.tunnel_job = None
        self.sent = self.received = 0
        self.hits = 0
        self.generation = 0
        self.stats = dict(state='disabled')
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self.pulse,daemon=True,name='remote-lease')
        self.upload_thread = threading.Thread(target=self.transfer,daemon=True,name='remote-transfer')
        self.thread.start()
        self.upload_thread.start()

    def http(self):
        token = (self.cfg.data_dir/'remote-token').read_text().strip()
        if len(token)<32:
            raise ValueError('Не настроен ключ сервера')
        session = requests.Session()
        session.trust_env = False
        session.headers['Authorization'] = 'Bearer '+token
        return session

    def request(self, session, method, path, **kwargs):
        response = session.request(method,self.base+path,timeout=(3,15),**kwargs)
        self.received += len(response.content)
        if method!='HEAD':
            response.raise_for_status()
        return response

    def open_tunnel(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0))
            port = sock.getsockname()[1]
        self.base = f'http://127.0.0.1:{port}'
        self.tunnel = subprocess.Popen(['ssh','-N','-T','-o','BatchMode=yes','-o','ExitOnForwardFailure=yes',
            '-o','ServerAliveInterval=10','-o','ServerAliveCountMax=2','-o','ConnectTimeout=8',
            '-L',f'127.0.0.1:{port}:127.0.0.1:{self.cfg.remote_port}',self.cfg.remote_host],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if os.name=='nt':
            from .windows_job import ModelJob
            self.tunnel_job = ModelJob(self.tunnel)
        deadline = time.monotonic()+10
        while time.monotonic()<deadline and not self.closed.is_set():
            if self.tunnel.poll() is not None:
                raise ConnectionError('SSH connection failed')
            try:
                with socket.create_connection(('127.0.0.1',port),timeout=.2):
                    return
            except OSError:
                self.closed.wait(.1)
        raise TimeoutError('SSH connection timed out')

    def close_tunnel(self):
        self.connected.clear()
        if self.tunnel:
            self.tunnel.terminate()
            try:
                self.tunnel.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.tunnel.kill()
            self.tunnel = None
        if self.tunnel_job:
            self.tunnel_job.close()
            self.tunnel_job = None

    def pulse(self):
        http = None
        next_retry = 0
        previous = (time.monotonic(),0,0)
        try:
            while not self.closed.is_set():
                enabled = self.active and self.cfg.remote_enabled and self.alive()
                try:
                    if not enabled:
                        if self.connected.is_set() and http:
                            self.request(http,'POST','/lease',json=dict(session=self.session_id,active=False))
                        self.close_tunnel()
                        self.stats = dict(state='paused' if self.cfg.remote_enabled else 'disabled')
                    elif time.monotonic()>=next_retry:
                        http = http or self.http()
                        if not self.tunnel or self.tunnel.poll() is not None:
                            self.close_tunnel()
                            self.open_tunnel()
                            self.closed.wait(.5)
                        if self.closed.is_set() or not self.active or not self.cfg.remote_enabled or not self.alive():
                            continue
                        self.stats = self.request(http,'POST','/lease',json=dict(session=self.session_id,active=True)).json()
                        self.connected.set()
                except Exception as exc:
                    self.close_tunnel()
                    self.stats = dict(state='disconnected',error=type(exc).__name__)
                    next_retry = time.monotonic()+5
                now = time.monotonic()
                self.stats.update(sent=self.sent,received=self.received,cache_hits=self.hits,
                    upload_bps=(self.sent-previous[1])/max(.1,now-previous[0]),
                    download_bps=(self.received-previous[2])/max(.1,now-previous[0]))
                previous = now,self.sent,self.received
                self.emit(dict(type='remote_status',remote=dict(self.stats)))
                self.closed.wait(2)
        finally:
            if http and self.connected.is_set():
                try:
                    self.request(http,'POST','/lease',json=dict(session=self.session_id,active=False))
                except Exception:
                    pass
            self.close_tunnel()
            if http:
                http.close()

    def transfer(self):
        http = None
        pending = {}
        cursor = 0
        generation = self.generation
        try:
            while not self.closed.is_set():
                if generation != self.generation:
                    pending.clear()
                    generation = self.generation
                if not self.active or not self.cfg.remote_enabled or not self.alive() or not self.connected.wait(.25):
                    self.closed.wait(.1)
                    continue
                item = None
                try:
                    http = http or self.http()
                    try:
                        item = self.queue.get_nowait()
                    except queue.Empty:
                        item = None
                    if item:
                        serial,asset,stages = item
                        try:
                            blob = prepare(asset,self.cfg)
                            context = {}
                            if 'location' in stages:
                                from .location import Places,extract_location
                                places = Places(self.cfg)
                                try:
                                    context['location'] = places.enrich(extract_location(Path(asset['path'])))
                                finally:
                                    places.close()
                            if generation != self.generation or not self.cfg.remote_enabled or not self.alive():
                                raise ValueError('Отправка отменена')
                            key = digest(blob)
                            response = self.request(http,'HEAD','/blobs/'+key)
                            if response.status_code==404:
                                self.request(http,'PUT','/blobs/'+key,data=blob)
                                self.sent += len(blob)
                            else:
                                response.raise_for_status()
                                self.hits += 1
                            if generation != self.generation or not self.active or not self.cfg.remote_enabled:
                                raise ValueError('Отправка отменена')
                            self.request(http,'POST','/jobs',json=dict(session=self.session_id,blob=key,stages=stages,context=context))
                            pending[serial] = job_key(key,stages,context)
                        except Exception as exc:
                            self.results.put((serial,None,str(exc)[:300]))
                    keys = list(pending)
                    # Interleave result collection with uploads; never wait for
                    # GPU availability to fill the opaque server input cache.
                    for serial in (keys+keys)[cursor:cursor+min(8,len(keys))]:
                        try:
                            result = self.request(http,'GET','/jobs/'+pending[serial]).json()
                            if result['status']=='done':
                                result['result']['key'] = pending[serial]
                                self.results.put((serial,result['result'],''))
                                pending.pop(serial,None)
                        except Exception as exc:
                            self.results.put((serial,None,str(exc)[:300]))
                            pending.pop(serial,None)
                    cursor = (cursor+8)%max(1,len(pending))
                except Exception:
                    self.closed.wait(1)
                if not item:
                    self.closed.wait(.3)
        finally:
            if http:
                http.close()

    def close(self):
        self.active = False
        self.closed.set()
        self.thread.join(3)
        # Watchdog expiry remains the fallback if the network is gone.

    def cancel(self):
        self.generation += 1
        while True:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break
