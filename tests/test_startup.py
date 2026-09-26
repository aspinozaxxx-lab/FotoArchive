import json
import os
import queue
import sqlite3
import subprocess
import sys
import time
from threading import Event

from fotoarchive.browse_reader import BrowseReader, BrowseViews
from fotoarchive.catalog import Catalog
from fotoarchive.config import Settings
from fotoarchive.startup import read_startup
from test_browse_reader import sample_catalog, stop


def command(**options):
    return dict(id=1, filters={}, presentation=dict(stacks=True, seconds=2,
        versions=True, sort='newest', **options))


def save_view(cfg, request):
    views = BrowseViews(cfg)
    try:
        _, session, _ = views.select(request, lambda: False)
        page = session.page()
        views.cache.save(session, views.cache_key, views.revision, lambda: False)
        assert views.cache.manifest.exists()
        return page
    finally:
        views.close()


def test_ui_import_does_not_load_inference_decoders_or_webengine():
    result = subprocess.run([sys.executable, '-c', '''
import sys
import fotoarchive.ui
assert not ({'numpy','lancedb','pyarrow','av','onnxruntime','pillow_heif',
    'PySide6.QtWebEngineWidgets','fotoarchive.engine'} & set(sys.modules))
'''], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_saved_catalogue_enabled_without_worker_or_source_drive(tmp_path, monkeypatch):
    writer = sample_catalog(tmp_path)
    status = dict(stats={'total': 450, 'metadata': 450}, paused=True,
                  inventory={'phase': 'ready', 'total': 460})
    writer.set_state('startup_status', status)
    writer.set_state('startup_facets', {'camera': ['saved camera']})
    events, ready = [], []
    monkeypatch.setattr(os, 'scandir', lambda *a: (_ for _ in ()).throw(AssertionError('source scan')))
    writer.db.execute('BEGIN IMMEDIATE')
    try:
        read_startup(writer.cfg, events.append, lambda: ready.append('browse'), lambda: ready.append('library'))
        assert ready == ['browse', 'library']
        assert events[0]['facets']['camera'] == ['saved camera']
        assert events[1] == status | {'type': 'status', 'saved': True,
            'local_enabled': writer.cfg.local_enabled, 'remote_enabled': writer.cfg.remote_enabled}
    finally:
        writer.db.rollback()
        writer.close()


def test_uninitialized_schema_waits_for_worker(tmp_path):
    cfg = Settings(data_dir=tmp_path)
    with sqlite3.connect(tmp_path / 'catalog.sqlite3') as db:
        db.execute('CREATE TABLE assets(id INTEGER)')
    events = []
    read_startup(cfg, events.append, lambda: events.append('browse'), lambda: events.append('library'))
    assert events == []


def test_restart_reuses_order_but_reads_current_asset_details_and_expands_stacks(tmp_path):
    writer = sample_catalog(tmp_path)
    expected = save_view(writer.cfg, command())
    with writer.db:
        writer.db.execute("UPDATE assets SET description='New caption' WHERE id=450")
    views = BrowseViews(writer.cfg)
    try:
        catalog, session, cached = views.select(command(), lambda: False)
        assert cached
        # Cold startup must not rebuild any archive-wide grouping or list.
        assert not catalog.db.execute("SELECT name FROM sqlite_temp_master WHERE type='table'").fetchall()
        page = session.page()
        assert [a['id'] for a in page['items']] == [a['id'] for a in expected['items']]
        assert page['items'][0]['description'] == 'New caption'
        stack = next(a for a in page['items'] if a.get('stack_count', 0) > 1)
        _, session, _ = views.select(command(expanded=[stack['stack_key']]), lambda: False)
        expanded = session.page()
        assert expanded['page_total'] > page['page_total']
        assert session.restore_position({'asset_id': 430, 'row': 10})['row'] >= 0
        _, session, _ = views.select(command(), lambda: False)
        assert session.page()['page_total'] == page['page_total']
    finally:
        views.close()
        writer.close()


def test_changed_membership_or_corrupt_cache_rebuilds_safely(tmp_path):
    writer = sample_catalog(tmp_path)
    save_view(writer.cfg, command())
    with writer.db:
        writer.db.execute('UPDATE assets SET present=0 WHERE id=450')
    views = BrowseViews(writer.cfg)
    try:
        _, session, cached = views.select(command(), lambda: False)
        assert not cached and session.total == 449
        assert all(a['id'] != 450 for a in session.page()['items'])
    finally:
        views.close()
    manifest = writer.cfg.data_dir / 'ui-cache/browse-current.json'
    path = manifest.parent / json.loads(manifest.read_text())['file']
    path.write_bytes(b'Interrupted or damaged disposable cache')
    views = BrowseViews(writer.cfg)
    try:
        _, session, cached = views.select(command(), lambda: False)
        assert not cached and session.page()['items']
    finally:
        views.close()
        writer.close()


def test_snapshot_cancelled_and_replaced_without_accumulating_backups(tmp_path):
    writer = sample_catalog(tmp_path)
    views = BrowseViews(writer.cfg)
    try:
        _, session, _ = views.select(command(), lambda: False)
        session.page()
        calls = []
        def cancelled():
            calls.append(True)
            return len(calls) > 2
        views.cache.save(session, views.cache_key, views.revision, cancelled)
        assert not list(views.cache.folder.glob('browse-*.sqlite3'))
        for expanded in ([], ['b:1'], []):
            _, session, _ = views.select(command(expanded=expanded), lambda: False)
            session.page()
            views.cache.save(session, views.cache_key, views.revision, lambda: False)
        assert len(list(views.cache.folder.glob('browse-*.sqlite3'))) == 1
    finally:
        views.close()
        writer.close()


def test_idle_snapshot_yields_to_new_filter(tmp_path, monkeypatch):
    from fotoarchive.browse_cache import BrowseCache
    writer = sample_catalog(tmp_path)
    entered, yielded = Event(), Event()
    def saving(self, session, key, revision, obsolete):
        entered.set()
        deadline = time.monotonic()+3
        while time.monotonic() < deadline and not obsolete():
            time.sleep(.002)
        assert obsolete()
        yielded.set()
    monkeypatch.setattr(BrowseCache, 'save', saving)
    events = queue.Queue()
    reader = BrowseReader(writer.cfg, events.put)
    try:
        reader.enable()
        reader.replace(command() | {'action': 'browse'})
        first = events.get(timeout=3)
        assert first['type'] == 'results' and not entered.is_set()
        assert entered.wait(3)
        started = time.monotonic()
        reader.replace(command() | {'action': 'browse', 'id': 2, 'filters': {'media_kind': 'video'}})
        result = events.get(timeout=1)
        assert result['id'] == 2 and result['total'] == 150
        assert yielded.is_set() and time.monotonic()-started < 1
    finally:
        stop(reader)
        writer.close()
