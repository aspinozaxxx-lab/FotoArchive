"""An infinite Qt gallery with bounded record, image and IO caches."""
from collections import OrderedDict
from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QImage, QPixmap


class ImageSignals(QObject):
    ready = Signal(str, QImage)


class ImageTask(QRunnable):
    def __init__(self, key, signals):
        super().__init__()
        self.key, self.signals = key, signals

    def run(self):
        self.signals.ready.emit(self.key, QImage(self.key))


class PhotoModel(QAbstractListModel):
    AssetRole = Qt.UserRole + 1
    PixmapRole = Qt.UserRole + 2
    pageRequested = Signal(int)
    assetsReady = Signal(int, int)
    countChanged = Signal()
    PAGE_SIZE = 200
    MAX_PAGES = 8

    def __init__(self):
        super().__init__()
        self.blocks = OrderedDict()
        self.pending = set()
        self.count = self.known_total = 0
        self.has_more = False
        self.cache = OrderedDict()
        self.failed = OrderedDict()
        self.geometry = {}
        self.hover_thumbnail = None
        self.requested = set()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(3)
        self.signals = ImageSignals()
        self.signals.ready.connect(self.loaded)

    def set_items(self, items):
        self.reset_result(items, len(items), False)

    def reset_result(self, items, total, has_more, offset=0, loaded_count=0, geometry_prefix=None):
        self.pool.clear()
        self.requested.clear()
        self.failed.clear()
        self.beginResetModel()
        self.blocks.clear()
        self.geometry = {row: size for row, size in self.geometry.items()
                         if geometry_prefix is not None and row < geometry_prefix}
        self.hover_thumbnail = None
        self.pending.clear()
        self.count = min(total, max(offset + len(items), loaded_count))
        self.known_total, self.has_more = total, has_more
        if items:
            self.blocks[offset] = items
            self.geometry.update((offset+i,(a.get('width',4),a.get('height',3))) for i,a in enumerate(items))
        self.endResetModel()
        self.countChanged.emit()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else self.count

    def request_page(self, offset):
        offset = offset // self.PAGE_SIZE * self.PAGE_SIZE
        if offset not in self.pending and len(self.pending) < 4:
            self.pending.add(offset)
            self.pageRequested.emit(offset)

    def canFetchMore(self, parent=QModelIndex()):
        offset = self.count // self.PAGE_SIZE * self.PAGE_SIZE
        return not parent.isValid() and offset not in self.pending and (self.count < self.known_total or self.has_more)

    def fetchMore(self, parent=QModelIndex()):
        if self.canFetchMore(parent):
            self.request_page(self.count)

    def accept_page(self, offset, items, total, has_more):
        self.pending.discard(offset)
        self.known_total, self.has_more = total, has_more
        new_count = max(self.count, offset + len(items))
        if new_count > self.count:
            self.beginInsertRows(QModelIndex(), self.count, new_count - 1)
            self.count = new_count
            self.endInsertRows()
        self.blocks[offset] = items
        self.geometry.update((offset+i,(a.get('width',4),a.get('height',3))) for i,a in enumerate(items))
        self.blocks.move_to_end(offset)
        while len(self.blocks) > self.MAX_PAGES:
            self.blocks.popitem(last=False)
        if items:
            self.dataChanged.emit(self.index(offset), self.index(offset + len(items) - 1))
            self.assetsReady.emit(offset, offset + len(items) - 1)
        self.countChanged.emit()

    def update_verification(self, asset, total, has_more, regroup=False):
        self.known_total, self.has_more = total, has_more
        if regroup:
            self.blocks.clear()
            self.pending.clear()
            if self.count > total:
                self.beginRemoveRows(QModelIndex(), total, self.count - 1)
                self.count = total
                self.endRemoveRows()
            if self.count:
                self.dataChanged.emit(self.index(0), self.index(self.count - 1))
        else:
            for offset, items in self.blocks.items():
                for i, old in enumerate(items):
                    if old["id"] == asset["id"]:
                        items[i] = old | {"verification": asset["verification"]}
                        self.dataChanged.emit(self.index(offset+i), self.index(offset+i))
        self.countChanged.emit()

    def asset(self, row):
        if not 0 <= row < self.count:
            return None
        offset = row // self.PAGE_SIZE * self.PAGE_SIZE
        if offset not in self.blocks:
            self.request_page(offset)
            return None
        self.blocks.move_to_end(offset)
        position = row - offset
        return self.blocks[offset][position] if position < len(self.blocks[offset]) else None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        asset = self.asset(index.row())
        if not asset:
            return "Загрузка…" if role == Qt.DisplayRole else None
        if role == self.AssetRole:
            return asset
        if role == Qt.DisplayRole:
            return asset["filename"]
        if role == Qt.ToolTipRole:
            from datetime import datetime
            try:
                date = datetime.fromisoformat(asset['captured_at']).strftime('%d.%m.%Y %H:%M:%S') if asset.get('captured_at') else 'Дата неизвестна'
            except ValueError:
                date = 'Дата неизвестна'
            stack = f"\nВ стопке совпало: {asset['stack_count']} из {asset.get('stack_total',asset['stack_count'])}. Нажмите число, чтобы раскрыть." if asset.get('stack_count',0)>1 else ''
            video = ''
            if asset.get('media_kind') == 'video':
                from .video import timestamp_text, search_coverage_text
                moments = asset.get('matched_moments',[])
                if moments:
                    video = '\nНайденные моменты: '+', '.join(timestamp_text(m['timestamp_ms']) for m in moments[:4])+' · нажмите время на карточке'
                video += '\n'+search_coverage_text(asset)
            return asset.get('filename','') + '\n' + date + '\n' + asset.get('relative_path','') + stack + video
        if role == self.PixmapRole:
            key = str(asset.get("thumbnail") or "")
            if self.hover_thumbnail and self.hover_thumbnail[0] == asset['id']:
                if self.hover_thumbnail[1] not in self.failed:
                    key = self.hover_thumbnail[1]
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
            if key and key not in self.failed and key not in self.requested and len(self.requested) < 64:
                self.requested.add(key)
                self.pool.start(ImageTask(key, self.signals))
        return None

    def loaded(self, key, image):
        self.requested.discard(key)
        if not image.isNull():
            self.cache[key] = QPixmap.fromImage(image)
            while len(self.cache) > 256:
                self.cache.popitem(last=False)
        else:
            self.failed[key] = True
            while len(self.failed)>256:
                self.failed.popitem(last=False)
        # Only visible cells repaint; this also wakes slots whose thumbnail request was throttled.
        if self.count:
            self.dataChanged.emit(self.index(0), self.index(self.count - 1), [self.PixmapRole])
