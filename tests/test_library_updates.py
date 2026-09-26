from concurrent.futures import Future
import os
import time

import numpy as np
import pytest
from PIL import Image

from fotoarchive.catalog import Filters
from fotoarchive.config import Settings
from fotoarchive.engine import Engine
from fotoarchive.media import sha256
from fotoarchive.preparation import prepare_photo
from fotoarchive.search_session import SearchSession
from fotoarchive.ui import MainWindow
from test_catalog import make_image, prepare
from test_ui import FakeBackend


def config(tmp_path):
    root = tmp_path / 'photos'
    (root / '2003').mkdir(parents=True)
    return Settings(data_dir=tmp_path / 'data', root=root, includes=['2003'])


def finish_scan(engine, request=True):
    if request:
        engine.pipeline.request_scan(reconcile=True)
    deadline = time.monotonic() + 10
    while engine.pipeline.scanning:
        engine.pipeline.scan_tick()
        assert time.monotonic() < deadline
        time.sleep(.001)
    return engine.catalog.state('library_check')


def test_added_replaced_deleted_and_returned_files_without_reprocessing_unchanged(tmp_path):
    cfg = config(tmp_path)
    engine = Engine(cfg)
    cat = engine.catalog
    vector = np.ones(768, dtype=np.float32) / np.sqrt(768)
    keep, replaced, removed = [make_image(cfg, name) for name in ('keep.jpg', 'replace.jpg', 'remove.jpg')]
    keep_id, replace_id, remove_id = [prepare(cat, p, vector) for p in (keep, replaced, removed)]
    cat.complete_caption({'asset_id': keep_id, 'file_version': 1}, {'description': 'Сохранённое описание'})
    engine.index.flush_all()
    unchanged_jobs = [tuple(r) for r in cat.db.execute('SELECT * FROM jobs WHERE asset_id=?', (keep_id,))]
    before = sha256(keep)
    original_bytes, original_time = removed.read_bytes(), removed.stat().st_mtime_ns
    removed.unlink()
    Image.new('RGB', (120, 70), 'blue').save(replaced)
    added = make_image(cfg, 'new.jpg')
    (cfg.root / '2003' / 'new.xmp').write_text('sidecar')
    try:
        result = finish_scan(engine)
        assert {k: result[k] for k in ('added', 'changed', 'removed', 'unchanged')} == dict(added=1, changed=1, removed=1, unchanged=1)
        assert cat.get(replace_id)['version'] == 2 and not cat.get(replace_id)['metadata_ready']
        assert cat.vector(replace_id) is None and cat.vector(remove_id) is None
        assert not cat.get(remove_id)['present']
        assert sha256(keep) == before
        assert cat.get(keep_id)['description'] == 'Сохранённое описание'
        assert unchanged_jobs == [tuple(r) for r in cat.db.execute('SELECT * FROM jobs WHERE asset_id=?', (keep_id,))]
        assert added.exists() and not removed.exists()
        # SQLite is authoritative even while the persisted index queue is pending.
        assert all(a['id'] not in (replace_id, remove_id) for a in engine.index.candidates(vector, '', Filters()))
        engine.index.flush_all()
        assert engine.index.table.count_rows() == 1
        removed.write_bytes(original_bytes)
        os.utime(removed, ns=(original_time, original_time))
        result = finish_scan(engine)
        assert result['restored'] == 1 and result['added'] == result['changed'] == result['removed'] == 0
        assert cat.get(remove_id)['present'] and cat.get(remove_id)['version'] == 1
        assert np.allclose(cat.vector(remove_id), vector)
        engine.index.flush_all()
        assert engine.index.table.count_rows() == 2
        assert cat.state('scan_queue') == []
    finally:
        engine.close()


def test_only_selected_folders_and_folder_name_boundaries_are_reconciled(tmp_path):
    cfg = config(tmp_path)
    engine = Engine(cfg)
    inside = make_image(cfg)
    outside = make_image(cfg, folder='20030')
    nested = make_image(cfg, folder='2003/deleted-subfolder')
    ids = [prepare(engine.catalog, p) for p in (inside, outside, nested)]
    for p in (inside, outside, nested):
        p.unlink()
    nested.parent.rmdir()
    try:
        state = finish_scan(engine)
        assert state['removed'] == 2
        assert [engine.catalog.get(i)['present'] for i in ids] == [0, 1, 0]
        assert engine.catalog.stats()['total'] == 1
    finally:
        engine.close()


@pytest.mark.parametrize('disconnect_root', [False, True])
def test_unavailable_disk_or_selected_folder_never_removes_entries(tmp_path, disconnect_root):
    cfg = config(tmp_path)
    engine = Engine(cfg)
    asset_id = prepare(engine.catalog, make_image(cfg))
    folder = cfg.root if disconnect_root else cfg.root / '2003'
    offline = folder.with_name('offline')
    folder.rename(offline)
    try:
        with pytest.raises(OSError):
            finish_scan(engine)
        assert engine.catalog.get(asset_id)['present']
        assert engine.catalog.state('library_check')['phase'] == 'error'
        assert engine.catalog.state('scan_queue')[0]['reconcile']
        offline.rename(folder)
        assert finish_scan(engine, request=False)['removed'] == 0
    finally:
        engine.close()


def test_disk_disappearing_after_enumeration_aborts_removal(tmp_path):
    cfg = config(tmp_path)
    engine = Engine(cfg)
    photo = make_image(cfg)
    asset_id = prepare(engine.catalog, photo)
    photo.unlink()
    engine.pipeline.request_scan(reconcile=True)
    deadline = time.monotonic() + 5
    try:
        while engine.pipeline.check_finalizer is None:
            engine.pipeline.scan_tick()
            assert time.monotonic() < deadline
            time.sleep(.001)
        (cfg.root / '2003').rename(cfg.root / 'offline')
        with pytest.raises(OSError):
            engine.pipeline.scan_tick()
        assert engine.catalog.get(asset_id)['present']
        assert engine.catalog.stats()['outbox'] == 0
    finally:
        engine.close()


def test_interrupted_check_restarts_and_no_partial_walk_hides_files(tmp_path):
    cfg = config(tmp_path)
    engine = Engine(cfg)
    removed = make_image(cfg, 'removed.jpg')
    removed_id = prepare(engine.catalog, removed)
    removed.unlink()
    for i in range(8):
        make_image(cfg, f'{i}.jpg')
    engine.pipeline.request_scan(reconcile=True)
    deadline = time.monotonic() + 5
    while not engine.pipeline.scan_state or not engine.pipeline.scan_state['files']:
        engine.pipeline.scan_tick(limit=1)
        assert time.monotonic() < deadline
        time.sleep(.001)
    assert engine.catalog.get(removed_id)['present']
    engine.close()
    engine = Engine(cfg)
    try:
        assert engine.pipeline.scanning
        assert finish_scan(engine, request=False)['removed'] == 1
        assert engine.catalog.stats()['total'] == 8
    finally:
        engine.close()


def test_deleted_file_rejects_late_cpu_caption_and_embedding_results(tmp_path):
    cfg = config(tmp_path)
    engine = Engine(cfg)
    path = make_image(cfg)
    asset_id, _ = engine.catalog.register(path)
    job = engine.catalog.next_job(('metadata',))
    asset = engine.catalog.get(asset_id)
    future = Future()
    future.set_result(prepare_photo(asset, str(cfg.data_dir / 'late.webp')))
    engine.pipeline.cpu.pending[future] = (job, asset)
    path.unlink()
    try:
        assert finish_scan(engine)['removed'] == 1
        engine.pipeline.cpu.collect()
        engine.catalog.complete_embedding(job, np.ones(768, dtype=np.float32))
        engine.catalog.complete_caption(job, {'description': 'Late description'})
        engine.catalog.finish_job(job, 1, 'Late failure')
        assert engine.catalog.get(asset_id)['metadata_ready'] == 0
        assert engine.catalog.get(asset_id)['description'] == ''
        assert engine.catalog.db.execute('SELECT count(*) FROM embeddings').fetchone()[0] == 0
        assert engine.catalog.stats()['errors'] == engine.catalog.stats()['pending'] == 0
    finally:
        engine.close()


def test_video_replacement_and_removal_delete_every_frame_from_search(tmp_path):
    cfg = config(tmp_path)
    engine = Engine(cfg)
    cat = engine.catalog
    path = cfg.root / '2003' / 'clip.mp4'
    path.write_bytes(b'video fixture')
    asset_id, _ = cat.register(path)
    metadata = dict(width=80, height=60, captured_at=None, date_raw=None, date_offset=None,
                    camera='', orientation='landscape', metadata_json='{}', media_kind='video', duration_ms=21000)
    job = cat.next_job(('metadata',))
    cat.complete_metadata(job, metadata, path, 'test')
    cat.finish_job(job, .1)
    for _ in range(3):
        job = cat.next_job(('embedding',))
        cat.complete_embedding(job, np.ones(768, dtype=np.float32))
        cat.finish_job(job, .1)
    engine.index.flush_all()
    assert engine.index.table.count_rows() == 3
    path.write_bytes(b'replaced video fixture')
    try:
        assert finish_scan(engine)['changed'] == 1
        cat.complete_embedding(job, np.zeros(768, dtype=np.float32))  # Obsolete frame cannot be inserted.
        engine.index.flush_all()
        assert engine.index.table.count_rows() == 0
        assert cat.db.execute('SELECT count(*) FROM units').fetchone()[0] == 0
        metadata['duration_ms'] = 1000
        job = cat.next_job(('metadata',))
        cat.complete_metadata(job, metadata, path, 'new')
        cat.finish_job(job, .1)
        job = cat.next_job(('embedding',))
        cat.complete_embedding(job, np.ones(768, dtype=np.float32))
        cat.finish_job(job, .1)
        engine.index.flush_all()
        assert engine.index.table.count_rows() == 1
        path.unlink()
        assert finish_scan(engine)['removed'] == 1
        engine.index.flush_all()
        assert engine.index.table.count_rows() == 0
        assert cat.stats()['video_frames']['total'] == 0
    finally:
        engine.close()


def test_check_updates_button_progress_and_unchanged_check_preserve_view(qtbot, tmp_path):
    backend = FakeBackend()
    window = MainWindow(Settings(data_dir=tmp_path), backend)
    qtbot.addWidget(window)
    action = next(a for a in window.library_menu.menu().actions() if a.text() == 'Проверить обновления')
    assert window.library_menu.menu().toolTipsVisible()
    assert action.toolTip() == window.check_updates_button.toolTip()
    assert 'а не обновления программы' in action.toolTip()
    assert 'подпапки' in action.toolTip() and 'При запуске' in action.toolTip()
    window.check_updates_button.click()
    assert backend.sent[-1] == {'action': 'check_updates'}
    assert not window.check_updates_button.isEnabled()
    window.on_event({'type': 'library_check', 'check': {'phase': 'checking', 'checked': 1234}})
    assert '1 234' in window.updates_label.text()
    count = len(backend.sent)
    window.on_event({'type': 'library_check', 'check': {'phase': 'complete', 'unchanged': 1234}})
    assert len(backend.sent) == count and window.check_updates_button.isEnabled()
    window.on_event({'type': 'library_check', 'check': {'phase': 'error', 'error': 'Диск недоступен'}})
    assert window.check_updates_button.isEnabled() and 'Диск недоступен' in window.updates_label.text()
    window.close()


def test_browse_anchor_survives_updates_and_deleted_anchor_falls_back(tmp_path):
    cfg = config(tmp_path)
    engine = Engine(cfg)
    cat = engine.catalog
    paths = [make_image(cfg, f'{i}.jpg') for i in range(4)]
    ids = [prepare(cat, p) for p in paths]
    try:
        paths[1].unlink()
        finish_scan(engine)
        session = SearchSession(cat, None, 1, Filters(), '', None, [], mode='browse')
        state = session.restore_position(dict(asset_id=ids[2], selected_id=ids[0], row=1, loaded_count=200))
        assert state['row'] == 1 and state['selected_row'] == 2 and state['loaded_count'] == 3
        state = session.restore_position(dict(asset_id=ids[1], selected_id=ids[1], row=200))
        assert state['row'] == state['selected_row'] == 2
        assert ids[1] not in [a['id'] for a in session.page()['items']]
    finally:
        engine.close()


def test_removal_queue_survives_restart_and_removes_faces_and_places(tmp_path):
    cfg = config(tmp_path)
    engine = Engine(cfg)
    path = make_image(cfg)
    asset_id = prepare(engine.catalog, path, np.ones(768, dtype=np.float32))
    job = dict(asset_id=asset_id, file_version=1)
    engine.catalog.complete_faces(job, [dict(portrait=Image.new('RGB', (32, 32)), box=[0, 0, 32, 32],
        landmarks=[[1, 1]] * 5, confidence=.99, vector=np.ones(128, dtype=np.float32))])
    face_id = engine.catalog.faces_for(asset_id)[0]['id']
    engine.catalog.complete_location(job, dict(latitude=50., longitude=40., altitude=None,
        geo_text='Test place', geo_source='EXIF', geo_json='{}'), np.ones(768, dtype=np.float32))
    engine.index.flush_all()
    assert engine.index.faces.count_rows() == engine.index.contexts.count_rows() == 1
    path.unlink()
    assert finish_scan(engine)['removed'] == 1
    assert engine.catalog.face_vector(face_id) is None
    assert engine.catalog.faces_for(asset_id) == []
    assert engine.catalog.stats()['outbox'] > 0
    engine.close()
    engine = Engine(cfg)
    try:
        engine.index.flush_all()
        assert engine.index.faces.count_rows() == engine.index.contexts.count_rows() == engine.index.table.count_rows() == 0
        assert engine.catalog.stats()['outbox'] == 0
    finally:
        engine.close()


def test_refresh_updates_loaded_gallery_without_jumping_to_first_page(qtbot, tmp_path):
    backend = FakeBackend()
    window = MainWindow(Settings(data_dir=tmp_path), backend)
    qtbot.addWidget(window)
    window.show()
    items = [dict(id=i, filename=f'{i}.jpg', relative_path=f'2003/{i}.jpg', version=1,
                  width=64, height=48, size=100, extension='.jpg') for i in range(400)]
    window.on_event(dict(type='results', id=0, items=items[:200], total=400, mode='browse'))
    window.model.accept_page(200, items[200:], 400, False)
    qtbot.waitUntil(lambda: window.gallery.visualRect(window.model.index(250)).isValid())
    window.gallery.setCurrentIndex(window.model.index(250))
    window.gallery.scrollTo(window.model.index(250))
    qtbot.waitUntil(lambda: window.gallery.verticalScrollBar().value() > 0)
    window.on_event(dict(type='library_check', check=dict(phase='complete', removed=1)))
    command = backend.sent[-1]
    assert command['action'] == 'browse' and command['refresh_anchor']['selected_id'] == 250
    window.on_event(dict(type='results', id=window.request_id, items=items[200:399], total=399,
        page_total=399, offset=200, mode='browse', restore=dict(row=248, selected_row=250, loaded_count=399, offset_y=0)))
    qtbot.waitUntil(lambda: window.gallery.verticalScrollBar().value() > 0)
    assert window.gallery.currentIndex().row() == 250
    assert window.selected_asset['id'] == 250
    assert window.model.rowCount() == 399
    assert window.model.asset(250)['id'] == 250
    window.close()
