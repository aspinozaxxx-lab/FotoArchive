"""Bounded CPU read-ahead for the GPU. No database or GPU access in threads."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import time


def prepare_embedding(prepare, asset):
    from .media import visual_path
    started = time.perf_counter()
    path = Path(asset['path'])
    expected = (asset['size'], asset['mtime_ns'])
    try:
        before = path.stat()
        if (before.st_size, before.st_mtime_ns) != expected:
            return {'changed': True}
        pixels = prepare(visual_path(asset))
        after = path.stat()
        if (after.st_size, after.st_mtime_ns) != expected:
            return {'changed': True}
        return {'pixels': pixels, 'elapsed': time.perf_counter()-started}
    except Exception as exc:
        return {'error': f'{type(exc).__name__}: {exc}', 'elapsed': time.perf_counter()-started}


class EmbeddingInputs:
    def __init__(self, engine, capacity=16, workers=2):
        self.engine, self.catalog = engine, engine.catalog
        self.capacity, self.workers = capacity, workers
        self.pending = {}
        self.executor = None

    def fill(self):
        while len(self.pending) < self.capacity:
            job = self.catalog.next_job(('embedding',))
            if job is None:
                break
            try:
                prepare = self.engine.embeddings().prepare_image
                asset = self.catalog.job_asset(job)
                if self.executor is None:
                    self.executor = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix='gpu-input')
                future = self.executor.submit(prepare_embedding, prepare, asset)
                self.pending[future] = (job, asset)
            except Exception:
                self.catalog.requeue_jobs([job])
                raise

    def take(self, limit=4):
        ready = [f for f in self.pending if f.done()][:limit]
        return [(*self.pending.pop(f), f.result()) for f in ready]

    def cancel(self):
        for future in self.pending:
            future.cancel()
        self.catalog.requeue_jobs([job for job, _ in self.pending.values()])
        self.pending.clear()

    def close(self):
        self.cancel()
        if self.executor:
            self.executor.shutdown(wait=True, cancel_futures=True)
            self.executor = None
