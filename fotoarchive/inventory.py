"""Count the selected source independently of the bounded processing queues."""
from dataclasses import replace
from collections import Counter
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from threading import Event, Thread
import time

from .catalog import enumerate_source
from .media_types import APPLEDOUBLE, SOURCE_RULES_VERSION, format_key
from .config import SUPPORTED, EMBED_VERSION, CAPTION_VERSION, FACE_VERSION, GEO_VERSION, RAW_FORMATS, IMAGE_FORMATS, VIDEO_FORMATS


JPEG_FORMATS = {'.jpg', '.jpeg', '.bmp'}
SIDECAR_FORMATS = {'.xmp', '.aae', '.thm', '.dop', '.pp3', APPLEDOUBLE}


def format_group(extension):
    extension = extension.lower()
    for group, formats in (('jpeg', JPEG_FORMATS), ('raw', RAW_FORMATS),
                           ('other_images', IMAGE_FORMATS), ('video', VIDEO_FORMATS),
                           ('sidecars', SIDECAR_FORMATS)):
        if extension in formats:
            return group
    return 'other'


def format_totals(formats):
    groups = Counter()
    for extension, count in formats.items():
        groups[format_group(extension)] += count
    groups['images'] = groups['jpeg'] + groups['raw'] + groups['other_images']
    groups['supported'] = sum(count for extension, count in formats.items() if extension in SUPPORTED)
    groups['files'] = sum(formats.values())
    return groups


def selection_key(cfg):
    return {'root': os.path.normcase(str(cfg.root.resolve())),
            'includes': sorted({item.replace('\\', '/').strip('/').casefold() or '.' for item in cfg.includes}),
            'formats': sorted(SUPPORTED), 'rules': SOURCE_RULES_VERSION}


class InventoryCounter:
    """A filename manifest on SSD, with at most 512 paths buffered in memory."""
    def __init__(self, cfg):
        descriptor, name = tempfile.mkstemp(prefix='inventory-', suffix='.sqlite3', dir=cfg.data_dir)
        os.close(descriptor)
        self.path = Path(name)
        self.stopped = Event()
        self.result = None
        snapshot = replace(cfg, includes=list(cfg.includes))
        self.thread = Thread(target=self._count, args=(snapshot,), name='source-inventory', daemon=True)
        self.thread.start()

    def _count(self, cfg):
        started = time.perf_counter()
        try:
            db = sqlite3.connect(self.path)
            try:
                # The temporary manifest is rebuilt after interruption; only its
                # atomic installation into the main catalogue needs durability.
                db.execute('PRAGMA synchronous=OFF')
                db.execute('CREATE TABLE inventory(path_key TEXT PRIMARY KEY) WITHOUT ROWID')
                batch, skipped, formats = [], 0, Counter()
                with db:
                    for path, supported in enumerate_source(cfg):
                        if self.stopped.is_set():
                            return
                        formats[format_key(path)] += 1
                        if supported:
                            batch.append((os.path.normcase(str(path)),))
                        else:
                            skipped += 1
                        if len(batch) >= 512:
                            db.executemany('INSERT OR IGNORE INTO inventory VALUES(?)', batch)
                            batch.clear()
                    db.executemany('INSERT OR IGNORE INTO inventory VALUES(?)', batch)
                total = db.execute('SELECT count(*) FROM inventory').fetchone()[0]
            finally:
                db.close()
            self.result = {'total': total, 'skipped': skipped, 'format_counts': dict(formats),
                           'seconds': time.perf_counter() - started}
        except Exception as exc:
            self.result = {'error': str(exc)}
        finally:
            if self.stopped.is_set():
                self.path.unlink(missing_ok=True)

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=1)
        if not self.thread.is_alive():
            self.path.unlink(missing_ok=True)


class SourceInventory:
    def __init__(self, catalog, emit=lambda event: None):
        self.catalog, self.cfg, self.emit = catalog, catalog.cfg, emit
        with catalog.db:
            catalog.db.execute('CREATE TABLE IF NOT EXISTS source_inventory(path_key TEXT PRIMARY KEY) WITHOUT ROWID')
        self.saved = catalog.state('source_inventory', {})
        self.phase = ('ready' if self.saved.get('selection') == selection_key(self.cfg)
                      and 'format_counts' in self.saved else 'counting')
        self.error = None
        self.counter = None
        self.counting_key = None

    def ensure(self):
        if self.phase != 'ready' and self.counter is None:
            self.start()

    def start(self, force=False):
        key = selection_key(self.cfg)
        if not force and self.counter is not None and self.counting_key == key:
            return
        self.close()
        self.phase, self.error, self.counting_key = 'counting', None, key
        self.counter = InventoryCounter(self.cfg)
        self.emit({'type': 'source_inventory', 'inventory': self.status()})

    def collect(self):
        if self.counter is None or self.counter.thread.is_alive():
            return False
        counter, self.counter = self.counter, None
        try:
            result = counter.result or {'error': 'Подсчёт снимков прерван'}
            if 'error' in result:
                # A missing disk or failed directory read cannot replace a good
                # manifest with a smaller partial result or an empty catalogue.
                self.phase, self.error = 'error', result['error']
            else:
                summary = result | {'selection': self.counting_key, 'completed_at': time.time()}
                db = self.catalog.db
                db.execute('ATTACH DATABASE ? AS inventory_incoming', (str(counter.path),))
                try:
                    with db:
                        db.execute('DELETE FROM source_inventory')
                        db.execute('INSERT INTO source_inventory SELECT path_key FROM inventory_incoming.inventory')
                        db.execute('INSERT OR REPLACE INTO app_state VALUES(?,?)',
                                   ('source_inventory', json.dumps(summary, ensure_ascii=False)))
                finally:
                    db.execute('DETACH DATABASE inventory_incoming')
                self.saved, self.phase = summary, 'ready'
        except Exception as exc:
            self.phase, self.error = 'error', str(exc)
        finally:
            counter.close()
        self.emit({'type': 'source_inventory', 'inventory': self.status()})
        return True

    def status(self):
        counts = {}
        if self.phase == 'ready':
            row = self.catalog.db.execute('''SELECT
                count(a.id) registered,
                sum(a.metadata_ready=1) metadata,
                sum(a.metadata_ready=1 OR j.status='error') scanned,
                sum(a.metadata_ready=0 AND j.status='error') unreadable,
                sum(a.embed_version=?) embeddings, sum(a.caption_version=?) captions,
                sum(a.face_version=?) faces, sum(a.geo_version=?) locations
                FROM source_inventory i
                LEFT JOIN assets a ON a.path_key=i.path_key AND a.present=1
                LEFT JOIN jobs j ON j.asset_id=a.id AND j.stage='metadata' AND j.file_version=a.version''',
                (EMBED_VERSION, CAPTION_VERSION, FACE_VERSION, GEO_VERSION)).fetchone()
            counts = {key: value or 0 for key, value in dict(row).items()}
        return {'phase': self.phase, 'total': self.saved.get('total') if self.phase == 'ready' else None,
                'last_total': self.saved.get('total'), 'counts': counts, 'error': self.error,
                'format_counts': self.saved.get('format_counts') if self.phase == 'ready' else None,
                'root': str(self.cfg.root), 'includes': list(self.cfg.includes)}

    def close(self):
        if self.counter is not None:
            self.counter.close()
            self.counter = None
