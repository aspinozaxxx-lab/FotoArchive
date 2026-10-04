"""Bounded asynchronous video indexing; SQLite writes stay on the coordinator."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import time

from .video_runtime import VideoOwner


def decode_result(result):
    import base64
    import io
    from PIL import Image
    from .remote_jobs import decode_faces
    for frame in result['frames']:
        frame['faces'] = decode_faces(frame['faces'])
        with Image.open(io.BytesIO(base64.b64decode(frame['thumbnail'], validate=True))) as image:
            if max(image.size) > 320:
                raise ValueError('Некорректная миниатюра видео')
            frame['thumbnail'] = image.convert('RGB')
    return result


class VideoJobs:
    def __init__(self, engine):
        self.engine, self.catalog = engine, engine.catalog
        self.owner = VideoOwner(engine.cfg)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='video-index')
        self.future = self.job = self.asset = None
        self.cancelled = threading.Event()
        self.notice = ''
        self.retry_at = 0

    @property
    def pending(self):
        return self.future is not None

    def ready(self):
        from .video_native import NativeOmni
        if self.engine.cfg.video_model != 'omni':
            self.notice = 'InternVideo2.5: видео обрабатываются на сервере'
            return False
        if not NativeOmni(self.engine.cfg).available():
            self.notice = 'Локальная модель видео ещё не установлена'
            return False
        self.notice = ''
        return True

    def fill(self):
        if self.pending or time.monotonic() < self.retry_at or self.engine.pipeline.captions.pending or not self.ready():
            return False
        from .video_archive import claim
        from .video_people import people_snapshot
        bundle = claim(self.catalog)
        if not bundle:
            self.retry_at = time.monotonic()+3
            return False
        self.asset, self.job = bundle
        people = people_snapshot(self.catalog)
        if self.engine.vlm:
            self.engine.vlm.close(); self.engine.vlm = None
        self.cancelled.clear()
        self.future = self.executor.submit(self.process, self.asset, people)
        self.engine.emit(dict(type='working', stage='video', filename=self.asset['filename']))
        return True

    def process(self, asset, people):
        from .remote_protocol import pack
        from .video_content import decode_segment
        started = time.perf_counter()
        try:
            data = decode_segment(asset, self.cancelled.is_set)
            if self.cancelled.is_set():
                raise InterruptedError('Обработка видео отменена')
            with tempfile.TemporaryDirectory(prefix='video-input-', dir=self.engine.cfg.data_dir/'previews') as temporary:
                blob = Path(temporary)/'input.bin'
                blob.write_bytes(pack(data.encode()))
                del data
                result = self.owner.run(dict(blob=str(blob), people=people, model=asset['video_model']), self.cancelled.is_set)
                if self.cancelled.is_set():
                    raise InterruptedError('Обработка видео отменена')
                result = decode_result(result)
            return dict(result=result, elapsed=time.perf_counter()-started)
        except Exception as exc:
            return dict(exception=exc, elapsed=time.perf_counter()-started)

    def collect(self):
        if not self.future or not self.future.done():
            return False
        future, job, asset = self.future, self.job, self.asset
        self.future = self.job = self.asset = None
        value = future.result()
        if self.cancelled.is_set():
            self.catalog.requeue_jobs([job])
            return True
        try:
            stat = Path(asset['path']).stat()
            if (stat.st_size, stat.st_mtime_ns) != (asset['size'], asset['mtime_ns']):
                self.catalog.register(Path(asset['path']))
                return True
            if value.get('exception'):
                raise value['exception']
            from .video_archive import complete
            if complete(self.catalog, job, value['result']):
                self.catalog.finish_job(job, value['elapsed'])
                self.engine.flush_progress(len(value['result']['frames']))
        except Exception as exc:
            self.catalog.finish_job(job, value['elapsed'], str(exc))
            self.engine.emit(dict(type='job_error', message=asset['filename']+': '+str(exc)))
            self.notice = str(exc)[:300]
            self.owner.close()
        return True

    def cancel(self):
        self.cancelled.set()
        self.owner.close()
        self.retry_at = 0

    def release(self):
        if not self.pending:
            self.owner.close()

    def close(self):
        self.cancel()
        self.executor.shutdown(wait=True, cancel_futures=True)
        if self.job:
            self.catalog.requeue_jobs([self.job])
