"""Bounded decoded previews; foreground reads never queue behind five RAW files."""
from collections import OrderedDict
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtGui import QImage
from .media import PreviewCache


def preview_key(asset):
    return (asset['id'], asset['version'], asset.get('timestamp_ms', 0), asset.get('path'))


class Signals(QObject):
    ready = Signal(object, QImage, str)


class Read(QRunnable):
    def __init__(self, cfg, asset, signals):
        super().__init__()
        self.cfg, self.asset, self.signals = cfg, dict(asset), signals

    def run(self):
        try:
            path = PreviewCache(self.cfg.data_dir / 'previews', self.cfg.preview_budget).get(self.asset)
            image = QImage(str(path))
            if image.isNull():
                raise ValueError('Не удалось прочитать превью')
            self.signals.ready.emit(preview_key(self.asset), image, '')
        except Exception as exc:
            self.signals.ready.emit(preview_key(self.asset), QImage(), str(exc))


class PreviewBuffer(QObject):
    ready = Signal(object, QImage, str)
    BUDGET = 240 * 1024**2

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.cache = OrderedDict()
        self.pending = set()
        self.wanted = []
        self.current = None
        self.closed = False
        self.signals = Signals()
        self.signals.ready.connect(self.loaded)
        # The application owns the pool: closing a viewer never waits for a RAW
        # decoder. At most two reads from this viewer can be active at once.
        self.pool = QThreadPool.globalInstance()

    def get(self, asset):
        key = preview_key(asset)
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        return None

    def prepare(self, current, neighbours):
        self.current = preview_key(current)
        self.wanted = [current, *neighbours]
        wanted = {preview_key(asset) for asset in self.wanted}
        for key in list(self.cache):
            if key not in wanted:
                self.cache.pop(key)
        self.fill()

    def fill(self):
        if self.closed:
            return
        for asset in self.wanted:
            key = preview_key(asset)
            if key in self.cache or key in self.pending:
                continue
            # One background read leaves a lane for an unexpected jump/back.
            limit = 2 if key == self.current else 1
            if len(self.pending) >= limit:
                continue
            self.pending.add(key)
            self.pool.start(Read(self.cfg, asset, self.signals), 100 if key == self.current else 0)

    def loaded(self, key, image, error):
        self.pending.discard(key)
        if self.closed:
            return
        wanted = {preview_key(a) for a in self.wanted}
        if key in wanted and not image.isNull():
            self.cache[key] = image
            self.cache.move_to_end(key)
            while len(self.cache) > 11 or sum(im.sizeInBytes() for im in self.cache.values()) > self.BUDGET:
                self.cache.popitem(last=False)
        # Remove failed speculative reads from this window, to avoid a retry loop.
        if error:
            self.wanted = [a for a in self.wanted if preview_key(a) != key]
        self.ready.emit(key, image, error)
        self.fill()

    def close(self):
        self.closed = True
        self.wanted.clear()
        self.cache.clear()
