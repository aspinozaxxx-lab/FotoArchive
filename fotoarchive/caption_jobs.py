"""One background description, overlapping the independent DirectML stages."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import time

from .inference import GPUUnavailable


def describe_file(vision, asset):
    from .media import visual_path
    started = time.perf_counter()
    expected = asset['size'], asset['mtime_ns']
    path = Path(asset['path'])
    try:
        before = path.stat()
        if (before.st_size, before.st_mtime_ns) != expected:
            return {'changed': True}
        result = vision.describe(visual_path(asset))
        after = path.stat()
        if (after.st_size, after.st_mtime_ns) != expected:
            return {'changed': True}
        return {'description': result, 'elapsed': time.perf_counter() - started}
    except Exception as exc:
        return {'exception': exc, 'elapsed': time.perf_counter() - started}


class CaptionJobs:
    def __init__(self, engine):
        self.engine, self.catalog = engine, engine.catalog
        self.executor = None
        self.future = None
        self.job = self.asset = None

    @property
    def pending(self):
        return self.future is not None

    def fill(self):
        if self.pending:
            return False
        job = self.catalog.next_job(('caption',))
        if job is None:
            return False
        try:
            asset = self.catalog.job_asset(job)
            vision = self.engine.vision()
            if self.executor is None:
                self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='photo-description')
            self.future = self.executor.submit(describe_file, vision, asset)
            self.job, self.asset = job, asset
            self.engine.emit({'type': 'working', 'stage': 'caption', 'filename': asset['filename']})
            return True
        except Exception:
            self.catalog.requeue_jobs([job])
            raise

    def collect(self, wait=False):
        if self.future is None or (not wait and not self.future.done()):
            return False
        future, job, asset = self.future, self.job, self.asset
        self.future = self.job = self.asset = None
        result = future.result()
        current = self.catalog.get(asset['id'])
        if current is None or not current['present'] or current['version'] != job['file_version']:
            return True
        try:
            stat = Path(asset['path']).stat()
            if result.get('changed') or (stat.st_size, stat.st_mtime_ns) != (asset['size'], asset['mtime_ns']):
                self.catalog.register(Path(asset['path']))
                self.catalog.requeue_jobs([job])
                return True
            if 'exception' in result:
                raise result['exception']
            self.catalog.complete_caption(job, result['description'])
            self.catalog.finish_job(job, result['elapsed'])
        except Exception as exc:
            self.catalog.finish_job(job, result.get('elapsed', 0), f'{type(exc).__name__}: {exc}')
            self.engine.emit({'type': 'job_error', 'message': f"{asset['filename']}: {exc}"})
            if isinstance(exc, GPUUnavailable):
                raise
        self.engine.flush_progress()
        return True

    def cancel(self):
        if self.future is not None and self.future.cancel():
            self.catalog.requeue_jobs([self.job])
            self.future = self.job = self.asset = None
        # An active HTTP request finishes the current photo. No next photo can
        # start until the coordinator resumes and explicitly calls fill().

    def close(self):
        self.cancel()
        try:
            self.collect(wait=True)
        finally:
            if self.executor:
                self.executor.shutdown(wait=True, cancel_futures=True)
                self.executor = None
