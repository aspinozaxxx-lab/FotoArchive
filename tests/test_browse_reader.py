import queue
import sqlite3
from threading import Event

import pytest

from fotoarchive.browse_reader import BrowseReader, BrowseViews
from fotoarchive.catalog import Catalog, Filters
from fotoarchive.config import Settings
from fotoarchive.interactive import obsolete_command
from fotoarchive.search_session import SearchSession


def sample_catalog(tmp_path, count=450):
    catalog = Catalog(Settings(data_dir=tmp_path / 'data', root=tmp_path / 'source'))
    with catalog.db:
        catalog.db.executemany('''INSERT INTO assets(id,source_id,relative_path,path_key,path,folder,
            filename,extension,size,mtime_ns,updated_at,metadata_ready,captured_at,media_kind)
            VALUES(?,?,?,?,?,'2003','sample',?,1,0,0,1,'2003-01-01',?)''',
            [(i, catalog.source_id, str(i), str(i), str(i), '.mov' if i % 3 == 0 else '.jpg',
              'video' if i % 3 == 0 else 'photo') for i in range(1, count + 1)])
    return catalog


def stop(reader):
    reader.close()
    reader.thread.join(3)
    assert not reader.thread.is_alive()


def test_browse_reads_committed_data_while_writer_busy_and_preserves_pages(tmp_path):
    writer = sample_catalog(tmp_path)
    events = queue.Queue()
    reader = BrowseReader(writer.cfg, events.put)
    try:
        reader.enable()
        # A running write/GPU coordinator must not delay independent list reads.
        writer.db.execute('BEGIN IMMEDIATE')
        writer.db.execute('UPDATE assets SET present=0 WHERE id=450')
        reader.replace(dict(action='browse', id=1, filters={}))
        first = events.get(timeout=3)
        assert first['type'] == 'results' and first['total'] == 450
        assert [a['id'] for a in first['items']] == list(range(450, 250, -1))
        writer.db.rollback()
        reader.page(dict(action='search_page', id=1, offset=200, view=0))
        second = events.get(timeout=3)
        assert [a['id'] for a in second['items']] == list(range(250, 50, -1))
        reader.replace(dict(action='browse', id=2, filters={'media_kind': 'video'}))
        video = events.get(timeout=3)
        assert video['total'] == 150
        assert all(a['media_kind'] == 'video' for a in video['items'])
        reader.replace(dict(action='browse', id=3, filters={'media_kind': 'photo'}))
        photo = events.get(timeout=3)
        assert photo['total'] == 300
        assert all(a['media_kind'] == 'photo' for a in photo['items'])
    finally:
        stop(reader)
        writer.close()


def test_rapid_switches_interrupt_obsolete_sql_and_deliver_only_latest(tmp_path, monkeypatch):
    import fotoarchive.browse_reader as module
    writer = sample_catalog(tmp_path)
    entered = Event()
    real_session = module.SearchSession

    def slow_first(catalog, index, request_id, *args, **kwargs):
        if request_id == 1:
            entered.set()
            # SQL deliberately much longer than a UI interaction; cancellation
            # must interrupt execution rather than wait for this to finish.
            catalog.db.execute('''WITH RECURSIVE numbers(x) AS
                (SELECT 1 UNION ALL SELECT x+1 FROM numbers WHERE x<1000000000)
                SELECT sum(x) FROM numbers''').fetchone()
        return real_session(catalog, index, request_id, *args, **kwargs)

    monkeypatch.setattr(module, 'SearchSession', slow_first)
    events = queue.Queue()
    reader = BrowseReader(writer.cfg, events.put)
    try:
        reader.enable()
        reader.replace(dict(action='browse', id=1, filters={}))
        assert entered.wait(3)
        for request_id in range(2, 101):
            reader.replace(dict(action='browse', id=request_id, filters={'media_kind': 'video'}))
        result = events.get(timeout=3)
        assert result['type'] == 'results' and result['id'] == 100
        assert result['total'] == 150
        assert events.empty()
    finally:
        stop(reader)
        writer.close()


def test_reader_waits_for_schema_and_replacement_keeps_only_last_request(tmp_path):
    cfg = Settings(data_dir=tmp_path / 'data', root=tmp_path / 'source')
    events = queue.Queue()
    reader = BrowseReader(cfg, events.put)
    try:
        reader.replace(dict(action='browse', id=1, filters={}))
        reader.replace(dict(action='browse', id=2, filters={'media_kind': 'video'}))
        assert events.empty()
        writer = sample_catalog(tmp_path)
        reader.enable()
        event = events.get(timeout=3)
        assert event['id'] == 2 and event['total'] == 150
        writer.close()
    finally:
        stop(reader)


def test_cancelled_page_keeps_completed_stack_view(tmp_path, monkeypatch):
    writer = sample_catalog(tmp_path, 900)
    entered = Event()
    original_page = SearchSession.page

    def slow_page(session, offset=None, *args, **kwargs):
        if session.request_id == 1 and offset == 200:
            entered.set()
            session.catalog.db.execute('''WITH RECURSIVE numbers(x) AS
                (SELECT 1 UNION ALL SELECT x+1 FROM numbers WHERE x<1000000000)
                SELECT sum(x) FROM numbers''').fetchone()
        return original_page(session, offset, *args, **kwargs)

    monkeypatch.setattr(SearchSession, 'page', slow_page)
    events = queue.Queue()
    reader = BrowseReader(writer.cfg, events.put)
    command = dict(action='browse', id=1, filters={}, presentation={'stacks':True,'seconds':2})
    try:
        reader.enable()
        reader.replace(command)
        first = events.get(timeout=5)
        assert len(first['items']) == 200
        reader.page(dict(action='search_page', id=1, offset=200, view=0))
        assert entered.wait(3)
        reader.replace(command | {'id':2})
        restored = events.get(timeout=5)
        assert restored['type'] == 'results' and restored['cached']
        assert [a['id'] for a in restored['items']] == [a['id'] for a in first['items']]
        reader.page(dict(action='search_page', id=2, offset=200, view=0))
        deep = events.get(timeout=5)
        assert len(deep['items']) == 101 and deep['page_total'] == 301
    finally:
        stop(reader)
        writer.close()


def test_read_connection_cannot_modify_catalogue(tmp_path):
    writer = sample_catalog(tmp_path, 3)
    reader = Catalog.open_reader(writer.cfg)
    try:
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            reader.db.execute('UPDATE assets SET present=0')
        session = SearchSession(reader, None, 1, Filters(), '', None, [], mode='browse')
        assert session.total == 3
        assert writer.db.execute('SELECT count(*) FROM assets WHERE present=1').fetchone()[0] == 3
    finally:
        reader.close()
        writer.close()


def test_cached_filters_reuse_order_but_read_current_details(tmp_path):
    writer = sample_catalog(tmp_path, 30)
    views = BrowseViews(writer.cfg)
    try:
        photo = dict(action='browse', id=1, filters={'media_kind': 'photo'})
        _, first, cached = views.select(photo, lambda: False)
        assert not cached and first.total == 20
        views.select(dict(action='browse', id=2, filters={'media_kind': 'video'}), lambda: False)
        # Model output must neither discard a valid list nor freeze its details.
        with writer.db:
            writer.db.execute("UPDATE assets SET description='updated',embed_version='new' WHERE id=29")
        _, same, cached = views.select(photo | {'id': 3}, lambda: False)
        assert cached and same is first and same.request_id == 3
        assert same.page()['items'][0]['description'] == 'updated'
        # A move between media kinds preserves the total catalogue count but
        # must invalidate both lists, including on a separate reader connection.
        with writer.db:
            writer.db.execute("UPDATE assets SET extension='.mov',media_kind='video' WHERE id=29")
        _, fresh, cached = views.select(photo | {'id': 4}, lambda: False)
        assert not cached and fresh.total == 19
        assert all(a['id'] != 29 for a in fresh.page()['items'])
        with writer.db:
            writer.db.execute('UPDATE assets SET present=0 WHERE id=28')
        _, fresh, cached = views.select(photo | {'id': 5}, lambda: False)
        assert not cached and fresh.total == 18
    finally:
        views.close()
        writer.close()


def test_cached_filter_connections_are_bounded_and_evicted_are_closed(tmp_path):
    writer = sample_catalog(tmp_path, 3)
    views = BrowseViews(writer.cfg)
    try:
        first, _, _ = views.select(dict(id=1, filters={}), lambda: False)
        for i in range(2, 9):
            views.select(dict(id=i, filters={'camera': str(i)}), lambda: False)
            assert len(views.views) <= 4
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            first.db.execute('SELECT 1')
    finally:
        views.close()
        writer.close()


def test_first_page_work_is_bounded_not_an_archive_metadata_sort(tmp_path):
    catalog = sample_catalog(tmp_path, 12000)
    session = SearchSession(catalog, None, 1, Filters(), '', None, [], mode='browse')
    steps = 0

    def progress():
        nonlocal steps
        steps += 100
        return 0

    catalog.db.set_progress_handler(progress, 100)
    try:
        page = session.page()
        assert [a['id'] for a in page['items']] == list(range(12000, 11800, -1))
        assert steps < 50000
    finally:
        catalog.db.set_progress_handler(None, 0)
        catalog.close()


def test_stale_interactive_commands_are_skipped_but_jobs_and_edits_are_retained():
    state = [10, 25]
    for action in ('browse', 'search', 'similar', 'face_search', 'search_page', 'verify_more', 'clear_search'):
        assert obsolete_command(dict(action=action, id=9), state)
        assert not obsolete_command(dict(action=action, id=10), state)
    assert obsolete_command(dict(action='faces', request_id=10, selection_serial=24), state)
    assert obsolete_command(dict(action='faces', request_id=9, selection_serial=25), state)
    assert not obsolete_command(dict(action='faces', request_id=10, selection_serial=25), state)
    for action in ('pause', 'resume', 'check_updates', 'add_folders', 'orientation_apply', 'retry', 'stop'):
        assert not obsolete_command(dict(action=action, id=0), state)


def test_backend_filter_switch_does_not_wait_for_coordinator(qtbot, tmp_path, monkeypatch):
    from fotoarchive import ui

    class BusyProcess:
        def __init__(self, **kwargs):
            pass
        def start(self):
            pass
        def is_alive(self):
            return True

    class Context:
        Queue = staticmethod(queue.Queue)
        Event = staticmethod(Event)
        Array = staticmethod(lambda code, values, lock=False: list(values))
        Process = BusyProcess

    writer = sample_catalog(tmp_path)
    monkeypatch.setattr(ui.mp, 'get_context', lambda _: Context())
    backend = ui.Backend(writer.cfg)
    received = []
    backend.event.connect(received.append)
    try:
        backend.events.put({'type': 'ready', 'facets': {}})
        qtbot.waitUntil(lambda: backend.reader.ready)
        # The coordinator deliberately never consumes any command in this test.
        backend.send(action='browse', id=1, filters={'media_kind': 'video'})
        qtbot.waitUntil(lambda: any(e.get('id') == 1 and e['type'] == 'results' for e in received), timeout=3000)
        assert next(e for e in received if e.get('id') == 1)['total'] == 150
        backend.send(action='browse', id=2, filters={'media_kind': 'photo'})
        qtbot.waitUntil(lambda: any(e.get('id') == 2 and e['type'] == 'results' for e in received), timeout=3000)
        assert next(e for e in received if e.get('id') == 2)['total'] == 300
        backend.send(action='search_page', id=2, view=0, offset=200)
        qtbot.waitUntil(lambda: any(e['type'] == 'search_page' for e in received), timeout=3000)
        page = next(e for e in received if e['type'] == 'search_page')
        assert len(page['items']) == 100
        backend.send(action='faces', asset_id=1)
        backend.send(action='faces', asset_id=2)
        queued = []
        while not backend.commands.empty():
            queued.append(backend.commands.get_nowait())
        current = [c for c in queued if not obsolete_command(c, backend.interactive_state)]
        assert [c['asset_id'] for c in current if c['action'] == 'faces'] == [2]
    finally:
        backend.close()
        stop(backend.reader)
        writer.close()
