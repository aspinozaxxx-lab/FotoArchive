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
from collections import deque
from pathlib import Path
import requests
from .media import open_rgb, visual_path
from .remote_protocol import digest, pack, job_key, MAX_INPUT, UPLOAD_AHEAD

PREPARE_WORKERS = 3
UPLOAD_WORKERS = 2
PREPARED_AHEAD = 4
DIRECT_IMAGES = {'.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif', '.avif'}


class Traffic:
    def __init__(self, clock=time.monotonic):
        self.clock, self.lock = clock, threading.Lock()
        self.sent = self.received = 0
        self.samples = deque([(clock(),0,0)],maxlen=32)

    def add(self, sent=0, received=0):
        with self.lock:
            self.sent += sent
            self.received += received

    def snapshot(self):
        with self.lock:
            now = self.clock()
            while len(self.samples)>1 and self.samples[1][0]<=now-10:
                self.samples.popleft()
            old = self.samples[0]
            rates = dict(sent=self.sent,received=self.received,
                upload_bps=(self.sent-old[1])/max(.1,now-old[0]),
                download_bps=(self.received-old[2])/max(.1,now-old[0]))
            self.samples.append((now,self.sent,self.received))
            return rates


class UploadBody(io.BytesIO):
    def __init__(self, data, traffic):
        super().__init__(data)
        self.traffic = traffic

    def read(self, size=-1):
        data = super().read(size)
        self.traffic.add(sent=len(data))
        return data


def prepare(asset, cfg):
    path = Path(asset['path'])
    before = path.stat()
    if (before.st_size,before.st_mtime_ns)!=(asset['size'],asset['mtime_ns']):
        raise ValueError('Файл изменился перед отправкой')
    source = visual_path(asset,cfg)
    # The server has the same HEIF/AVIF decoder. Sending its original container
    # preserves pixels and avoids seconds of lossless WebP re-encoding, often
    # expanding a 2 MB HEIC into a 10 MB upload in the process.
    if source.suffix.lower() in DIRECT_IMAGES:
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
        self.queue = queue.Queue(maxsize=UPLOAD_AHEAD)
        self.prepared = queue.Queue(maxsize=PREPARED_AHEAD)
        self.results = queue.Queue(maxsize=1024)
        self.pending = {}
        self.receipts = {}
        self.pending_lock = threading.Lock()
        self.traffic = Traffic()
        self.activity_lock = threading.Lock()
        self.preparing = {}
        self.uploading = {}
        self.session_id = secrets.token_hex(16)
        self.base = ''
        self.tunnel = None
        self.tunnel_job = None
        self.hits = 0
        self.generation = 0
        self.stats = dict(state='disabled')
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self.pulse,daemon=True,name='remote-lease')
        self.prepare_threads = [threading.Thread(target=self.prepare_inputs,daemon=True,name=f'remote-prepare-{i}')
                                for i in range(PREPARE_WORKERS)]
        self.upload_threads = [threading.Thread(target=self.transfer,daemon=True,name=f'remote-transfer-{i}')
                               for i in range(UPLOAD_WORKERS)]
        self.upload_thread = self.upload_threads[0]
        self.result_thread = threading.Thread(target=self.receive,daemon=True,name='remote-results')
        self.thread.start()
        for thread in self.prepare_threads+self.upload_threads:thread.start()
        self.result_thread.start()

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
        self.traffic.add(received=len(response.content))
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
                self.stats.update(self.traffic.snapshot(),cache_hits=self.hits,
                    **self.transfer_status(),received_queue=self.results.qsize())
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

    def enabled(self):
        return self.active and self.cfg.remote_enabled and self.alive() and self.connected.is_set()

    def cache_blocked(self):
        return self.stats.get('cache_pending_bytes',0) >= self.stats.get('cache_limit',20*1024**3)*.9

    def transfer_status(self):
        with self.activity_lock:
            preparing,uploading = len(self.preparing),len(self.uploading)
            current = next(iter(self.uploading.values()),None) or next(iter(self.preparing.values()),None)
        ready = self.prepared.qsize()
        stage = ('cache_full' if self.cache_blocked() else 'uploading' if uploading else
                 'preparing' if preparing else 'idle')
        return dict(uploading=stage,upload_queue=self.queue.qsize(),preparing=preparing,
                    prepared=ready,uploading_count=uploading,transfer_file=current or '')

    def deliver(self,generation,item):
        while not self.closed.is_set() and generation==self.generation:
            try:
                self.results.put(item,timeout=.1)
                return
            except queue.Full:pass

    def prepare_inputs(self):
        """Bounded decode/pack workers keep network and GPU work independent."""
        while not self.closed.is_set():
            if not self.enabled() or self.cache_blocked():
                self.closed.wait(.1)
                continue
            try:
                serial,asset,stages = self.queue.get(timeout=.1)
            except queue.Empty:
                continue
            generation = self.generation
            with self.activity_lock:self.preparing[serial] = asset.get('filename',Path(asset.get('path','')).name)
            try:
                blob = prepare(asset,self.cfg)
                context = {}
                if 'location' in stages:
                    from .location import Places,extract_location
                    places = Places(self.cfg)
                    try:context['location'] = places.enrich(extract_location(Path(asset['path'])))
                    finally:places.close()
                prepared = (generation,serial,asset,stages,blob,context)
                while not self.closed.is_set() and generation==self.generation:
                    try:
                        self.prepared.put(prepared,timeout=.1)
                        break
                    except queue.Full:pass
            except Exception as exc:
                self.deliver(generation,(serial,None,str(exc)[:300]))
            finally:
                with self.activity_lock:self.preparing.pop(serial,None)

    def transfer(self):
        http = None
        try:
            while not self.closed.is_set():
                if not self.enabled():
                    self.closed.wait(.1)
                    continue
                if self.cache_blocked():
                    self.closed.wait(.5)
                    continue
                item = None
                try:
                    http = http or self.http()
                    try:
                        item = self.prepared.get_nowait()
                    except queue.Empty:
                        item = None
                    if item:
                        generation,serial,asset,stages,blob,context = item
                        if generation!=self.generation:continue
                        with self.activity_lock:self.uploading[serial] = asset.get('filename',Path(asset.get('path','')).name)
                        try:
                            if generation != self.generation or not self.cfg.remote_enabled or not self.alive():
                                raise ValueError('Отправка отменена')
                            # A file may change while waiting in the ready queue.
                            # Do not submit stale work even before receipt checks.
                            if 'path' in asset and 'size' in asset:
                                info=Path(asset['path']).stat()
                                if (info.st_size,info.st_mtime_ns)!=(asset['size'],asset['mtime_ns']):
                                    raise ValueError('Файл изменился перед отправкой')
                            key = digest(blob)
                            response = self.request(http,'HEAD','/blobs/'+key)
                            if response.status_code==404:
                                with UploadBody(blob,self.traffic) as body:
                                    self.request(http,'PUT','/blobs/'+key,data=body)
                            else:
                                response.raise_for_status()
                                with self.activity_lock:self.hits += 1
                            if generation != self.generation or not self.active or not self.cfg.remote_enabled:
                                raise ValueError('Отправка отменена')
                            self.request(http,'POST','/jobs',json=dict(session=self.session_id,blob=key,stages=stages,context=context))
                            with self.pending_lock:
                                if generation==self.generation:
                                    self.pending[serial] = job_key(key,stages,context)
                        except Exception as exc:
                            self.deliver(generation,(serial,None,str(exc)[:300]))
                        finally:
                            with self.activity_lock:self.uploading.pop(serial,None)
                except Exception:
                    self.closed.wait(1)
                if not item:
                    self.closed.wait(.05)
        finally:
            if http:
                http.close()

    def receive(self):
        """Results and commit receipts never wait for a RAW decode or upload."""
        http = None
        try:
            while not self.closed.is_set():
                if not self.active or not self.cfg.remote_enabled or not self.alive() or not self.connected.wait(.25):
                    self.closed.wait(.2)
                    continue
                try:
                    http = http or self.http()
                    with self.pending_lock:
                        receipts = dict(list(self.receipts.items())[:128])
                        # Server takes FIFO jobs. Older outstanding inputs are
                        # sufficient, without polling thousands of future jobs.
                        keys = list(dict.fromkeys(self.pending.values()))[:128]
                        generation = self.generation
                    if receipts:
                        self.request(http,'POST','/receipts',json=dict(session=self.session_id,receipts=receipts))
                        with self.pending_lock:
                            for key,value in receipts.items():
                                if self.receipts.get(key)==value:
                                    self.receipts.pop(key)
                    if keys:
                        response = self.request(http,'POST','/results',json=dict(keys=keys)).json()
                        for item in response['items']:
                            result = item['result'] | dict(key=item['key'])
                            with self.pending_lock:
                                serials = [s for s,k in self.pending.items() if k==item['key']] if generation==self.generation else []
                                for serial in serials:
                                    self.pending.pop(serial)
                            for serial in serials:
                                self.results.put((serial,result,''))
                        for key in response.get('missing',[]):
                            with self.pending_lock:
                                serials = [s for s,k in self.pending.items() if k==key] if generation==self.generation else []
                                for serial in serials:
                                    self.pending.pop(serial)
                            for serial in serials:
                                self.results.put((serial,None,'Вход вытеснен из серверного кэша; будет отправлен снова'))
                except Exception:
                    self.closed.wait(1)
                self.closed.wait(.3)
        finally:
            if http:
                http.close()

    def acknowledge(self, key, receipt):
        with self.pending_lock:
            self.receipts[key] = receipt

    def close(self):
        self.active = False
        self.closed.set()
        self.thread.join(3)
        deadline=time.monotonic()+3
        for thread in self.prepare_threads+self.upload_threads+[self.result_thread]:
            thread.join(max(0,deadline-time.monotonic()))
        # Watchdog expiry remains the fallback if the network is gone.

    def cancel(self):
        with self.pending_lock:
            self.generation += 1
            self.pending.clear()
            self.receipts.clear()
        for channel in (self.queue,self.prepared):
            while True:
                try:channel.get_nowait()
                except queue.Empty:break
