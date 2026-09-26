"""Read saved catalogue state without loading models or visiting source folders."""
import sqlite3


def record_startup(window, started):
    """Small local timing report. Contains durations only, never photo contents."""
    import json
    import time
    from PySide6.QtCore import QTimer
    from . import __version__
    report = dict(version=__version__, window_seconds=time.perf_counter()-started)
    gaps = []
    previous = time.perf_counter()
    heartbeat = QTimer(window)
    heartbeat.setInterval(16)
    def tick():
        nonlocal previous
        now = time.perf_counter()
        gaps.append((now-previous)*1000)
        previous = now
    def event(value):
        name = {'results': 'first_results_seconds', 'ready': 'worker_ready_seconds'}.get(value.get('type'))
        if name and name not in report:
            report[name] = time.perf_counter()-started
        if value.get('type') == 'results':
            report.setdefault('cached_layout', value.get('cached', False))
    def thumbnail(key, image):
        if not image.isNull():
            report.setdefault('first_thumbnail_seconds', time.perf_counter()-started)
    def finish():
        heartbeat.stop()
        if gaps:
            report.update(ui_tick_p95_ms=sorted(gaps)[round((len(gaps)-1)*.95)], max_ui_tick_ms=max(gaps))
        try:
            target = window.cfg.data_dir / 'reports/startup.json'
            target.write_text(json.dumps(report, indent=2), encoding='utf-8')
        except OSError:
            pass
        window.backend.event.disconnect(event)
        window.model.signals.ready.disconnect(thumbnail)
    heartbeat.timeout.connect(tick)
    window.backend.event.connect(event)
    window.model.signals.ready.connect(thumbnail)
    heartbeat.start()
    QTimer.singleShot(20000, window, finish)


def read_startup(cfg, emit, enable_browse, enable_library):
    from .catalog import Catalog
    catalog = None
    try:
        catalog = Catalog.open_reader(cfg)
        # Older/new databases wait for the writer's migrations. These probes read
        # schema only and deliberately do not stat even one original file.
        for table, columns in {
            'assets': 'version,media_kind,user_place,user_latitude,user_longitude,geo_text',
            'app_state': 'key,value', 'people': 'id,name,examples,rejected,cover',
            'stack_exclusions': 'asset_id', 'stack_covers': 'stack_key,asset_id',
            'unit_details': 'unit_id,file_version,thumbnail', 'face_units': 'face_id,unit_id',
        }.items():
            catalog.db.execute(f'SELECT {columns} FROM {table} LIMIT 0')
        enable_browse()
        enable_library()
        emit(dict(type='catalog_ready', facets=catalog.state('startup_facets', {})))
        saved = catalog.state('startup_status')
        if saved:
            emit(saved | {'type': 'status', 'saved': True,
                'paused': catalog.state('paused', True),
                'local_enabled': cfg.local_enabled, 'remote_enabled': cfg.remote_enabled})
        else:
            # Upgrade path: the last manifest is already in SQLite. It remains
            # useful even when the source drive is unplugged.
            inventory = catalog.state('source_inventory', {})
            stats = catalog.stats()
            emit(dict(type='status', saved=True, stats=stats,
                paused=catalog.state('paused', True), inventory=dict(
                    phase='ready', total=inventory.get('total', stats['total']),
                    counts=stats | {'scanned': stats['metadata']},
                    format_counts=inventory.get('format_counts'),
                    root=str(cfg.root), includes=list(cfg.includes))))
    except (OSError, sqlite3.Error, ValueError):
        # Missing or migrating database: the normal worker reports readiness or
        # an actionable error. An optional fast path must not clear the gallery.
        pass
    finally:
        if catalog:
            catalog.close()
