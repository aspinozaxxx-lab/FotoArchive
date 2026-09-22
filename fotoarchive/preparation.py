"""Bounded CPU preparation; only the coordinator owns the catalogue connection."""
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
import multiprocessing
import os
import sys
from pathlib import Path
import threading
import time

import psutil
from PIL import Image, ImageOps

from .media import read_media, save_thumbnail, sha256


def worker_count(requested=0):
    physical = psutil.cpu_count(logical=False) or os.cpu_count() or 2
    automatic = max(1, physical-2)
    # Leave room for the window, decoder buffers, and GPU model host allocations.
    memory_limit = max(1, int(psutil.virtual_memory().available / (512*1024**2)))
    return max(1, min(8, memory_limit, requested or automatic))


def initialize_preparer(parent_pid):
    os.environ.setdefault('OMP_NUM_THREADS', '2')
    # A crashed coordinator must not leave independent workers reading the disk.
    def watch_parent():
        try:
            psutil.Process(parent_pid).wait()
        except psutil.NoSuchProcess:
            pass
        os._exit(1)
    threading.Thread(target=watch_parent, daemon=True, name='owner-watch').start()


def prepare_photo(asset, thumbnail):
    """Return small records and cache paths, never decoded images or SQLite handles."""
    started, cpu_started = time.perf_counter(), time.process_time()
    path = Path(asset['path'])
    result = {'pid': os.getpid(), 'started': started, 'executable':sys.executable,
              'frozen':bool(getattr(sys,'frozen',False))}
    try:
        before = path.stat()
        if (before.st_size, before.st_mtime_ns) != (asset['size'], asset['mtime_ns']):
            return result | {'changed': True}
        metadata, preview = read_media(path)
        with preview:
            save_thumbnail(preview, Path(thumbnail))
        digest = sha256(path)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            return result | {'changed': True}
        result.update(metadata=metadata, thumbnail=thumbnail, digest=digest)
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
    result.update(elapsed=time.perf_counter()-started, cpu_seconds=time.process_time()-cpu_started,
                  finished=time.perf_counter())
    return result


class PreparationPool:
    def __init__(self, catalog, emit=lambda event:None):
        self.catalog, self.emit = catalog, emit
        self.workers = worker_count(catalog.cfg.preparation_workers)
        # A short queue alone empties while Qwen is describing a photo. These
        # bounded futures keep CPU work available during a longer GPU call.
        self.capacity = min(512, self.workers*64)
        self.executor = None
        self.pending = {}
        self.process_ids = set()
        self.executables = set()
        self.frozen_processes = set()
        self.completed = 0
        self.cpu_seconds = 0.0

    def fill(self, limit=64):
        added = 0
        while len(self.pending) < self.capacity and added < limit:
            job = self.catalog.next_job(('metadata',))
            if job is None:
                break
            asset = self.catalog.get(job['asset_id'])
            thumbnail = self.catalog.cfg.data_dir/'thumbnails'/str(asset['id']//1000)/f"{asset['id']}_{asset['version']}.webp"
            try:
                if self.executor is None:
                    self.executor = ProcessPoolExecutor(max_workers=self.workers,
                        mp_context=multiprocessing.get_context('spawn'), initializer=initialize_preparer,
                        initargs=(os.getpid(),))
                future = self.executor.submit(prepare_photo, asset, str(thumbnail))
            except Exception:
                self.catalog.requeue_jobs([job])
                raise
            self.pending[future] = (job, asset)
            added += 1
        return added

    def collect(self, limit=64):
        done = [(f,pair) for f,pair in self.pending.items() if f.done()][:limit]
        for future, (job, asset) in done:
            del self.pending[future]
            if future.cancelled():
                self.catalog.requeue_jobs([job])
                continue
            try:
                result = future.result()
            except BrokenProcessPool:
                self.catalog.requeue_jobs([job])
                self.cancel()
                raise RuntimeError('Процесс подготовки остановился. Задания сохранены; нажмите «Продолжить индексацию».')
            self.process_ids.add(result['pid'])
            self.executables.add(result['executable'])
            if result['frozen']:
                self.frozen_processes.add(result['pid'])
            self.completed += 1
            self.cpu_seconds += result.get('cpu_seconds', 0)
            current = self.catalog.get(asset['id'])
            if not current or not current['present'] or current['version'] != job['file_version']:
                continue  # Late output must not complete the job for a newer file.
            error = result.get('error')
            try:
                stat = Path(asset['path']).stat()
                if result.get('changed') or (stat.st_size,stat.st_mtime_ns) != (asset['size'],asset['mtime_ns']):
                    self.catalog.register(Path(asset['path']))
                    self.catalog.requeue_jobs([job])
                    continue
                if not error:
                    self.catalog.complete_metadata(job,result['metadata'],result['thumbnail'],result['digest'])
            except Exception as exc:
                error = f'{type(exc).__name__}: {exc}'
            self.catalog.finish_job(job,result.get('elapsed',0),error)
            if error:
                self.emit({'type':'job_error','message':f"{asset['filename']}: {error}"})
        return len(done)

    def cancel(self):
        jobs = []
        for future, (job, _) in list(self.pending.items()):
            if future.cancel():
                jobs.append(job)
                del self.pending[future]
        self.catalog.requeue_jobs(jobs)

    def recover_broken(self):
        if self.executor:
            self.executor.shutdown(wait=True, cancel_futures=True)
            self.executor = None
        self.catalog.requeue_jobs([job for job,_ in self.pending.values()])
        self.pending.clear()

    def status(self):
        return {'workers':self.workers,'in_flight':len(self.pending),'capacity':self.capacity,
                'completed':self.completed,'process_ids':sorted(self.process_ids),'cpu_seconds':self.cpu_seconds,
                'executables':sorted(self.executables),'frozen_processes':len(self.frozen_processes)}

    def close(self):
        self.cancel()
        if self.executor:
            self.executor.shutdown(wait=True, cancel_futures=True)
            self.executor = None
        while self.pending:
            self.collect()
