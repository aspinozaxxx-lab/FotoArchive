"""Filesystem producer with a bounded queue; database writes stay in the owner."""
from dataclasses import replace
from queue import Queue, Empty, Full
from threading import Event, Thread

from .catalog import enumerate_source


class SourceScanner:
    def __init__(self, cfg, includes=None, skip_includes=(), capacity=256):
        self.queue = Queue(maxsize=capacity)
        self.stopped = Event()
        snapshot = replace(cfg, includes=list(cfg.includes if includes is None else includes))
        self.thread = Thread(target=self._produce,args=(snapshot,list(skip_includes)),daemon=True,name='source-scan')
        self.thread.start()

    def _put(self, value):
        while not self.stopped.is_set():
            try:
                self.queue.put(value, timeout=.1)
                return
            except Full:
                continue

    def _produce(self, cfg, skip):
        try:
            for path, supported in enumerate_source(cfg, skip):
                if self.stopped.is_set():
                    return
                self._put(('file',path,supported))
        except Exception as exc:
            self._put(('error',str(exc)))
        finally:
            self._put(('done',))

    def take(self):
        try:
            return self.queue.get_nowait()
        except Empty:
            return None

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=1)
