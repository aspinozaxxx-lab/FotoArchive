from pathlib import Path
import threading
import time

import numpy as np
from PIL import Image

from fotoarchive.catalog import Catalog, Filters
from fotoarchive.config import Settings
from fotoarchive.engine import Engine
from fotoarchive.search import SearchIndex


def make_engine(tmp_path, count=20):
    root = tmp_path / 'source'
    root.mkdir()
    engine = Engine(Settings(data_dir=tmp_path / 'data', root=root, includes=['.']))
    for i in range(count):
        path = root / f'{i}.jpg'
        Image.new('RGB', (64, 48), 'blue').save(path)
        asset_id, _ = engine.catalog.register(path)
        engine.process_one(('metadata',), asset_id=asset_id)
    return engine


class PreparedEmbedder:
    def __init__(self, gate=None):
        self.gate = gate
        self.batches = []

    def prepare_image(self, path):
        if self.gate:
            assert self.gate.wait(5)
        with Image.open(path) as image:
            image.load()
        return np.ones((3, 224, 224), np.float32)

    def prepared_images(self, images):
        self.batches.append(len(images))
        return [np.ones(768, np.float32)/np.sqrt(768) for _ in images]


def test_read_ahead_is_bounded_and_pause_requeues_all_owned_jobs(tmp_path):
    engine = make_engine(tmp_path)
    gate = threading.Event()
    engine.embedder = PreparedEmbedder(gate)
    try:
        engine.pipeline.inputs.fill()
        assert len(engine.pipeline.inputs.pending) == 16
        assert not engine.pipeline.idle
        assert engine.process_embedding_batch() is None
        engine.pipeline.inputs.cancel()
        assert not engine.pipeline.inputs.pending
        assert engine.catalog.db.execute("SELECT count(*) FROM jobs WHERE stage='embedding' AND status='pending'").fetchone()[0] == 20
        gate.set()
        engine.pipeline.inputs.fill()
        deadline = time.monotonic()+5
        while engine.pipeline.inputs.pending:
            engine.process_embedding_batch()
            assert time.monotonic() < deadline
            time.sleep(.01)
        assert engine.catalog.stats()['embeddings'] == 16
        assert all(1 <= n <= 4 for n in engine.embedder.batches)
    finally:
        gate.set()
        engine.close()


def test_batch_rejects_changed_files_and_does_not_stop_on_corruption(tmp_path):
    engine = make_engine(tmp_path, count=4)
    engine.embedder = PreparedEmbedder()
    inputs = engine.pipeline.inputs
    try:
        # Invalid bytes with a registered matching file fingerprint reach the
        # decoder, testing per-image errors rather than a stat mismatch.
        broken = engine.cfg.root / '2.jpg'
        broken.write_bytes(b'broken image')
        stat = broken.stat()
        with engine.catalog.db:
            engine.catalog.db.execute('UPDATE assets SET size=?,mtime_ns=? WHERE filename=?',
                                      (stat.st_size, stat.st_mtime_ns, broken.name))
        inputs.fill()
        deadline = time.monotonic()+5
        while not all(f.done() for f in inputs.pending):
            assert time.monotonic() < deadline
            time.sleep(.01)
        changed = engine.cfg.root / '1.jpg'
        Image.new('RGB', (100, 70), 'red').save(changed)
        engine.process_embedding_batch()
        assert engine.catalog.stats()['embeddings'] == 2
        changed_row = engine.catalog.db.execute("SELECT * FROM assets WHERE filename='1.jpg'").fetchone()
        assert changed_row['version'] == 2 and not changed_row['metadata_ready']
        assert engine.catalog.db.execute("SELECT count(*) FROM jobs WHERE stage='embedding' AND status='error'").fetchone()[0] == 1
    finally:
        engine.close()


def test_async_index_does_not_ack_a_newer_revision_or_use_sqlite_in_writer(tmp_path):
    engine = make_engine(tmp_path, count=1)
    cat, index = engine.catalog, engine.index
    job = cat.next_job(('embedding',))
    cat.complete_embedding(job, np.ones(768, np.float32)/np.sqrt(768))
    gate, started = threading.Event(), threading.Event()
    write = index.write_flush
    owner = threading.get_ident()
    def delayed(payload):
        assert threading.get_ident() != owner
        started.set()
        assert gate.wait(5)
        write(payload)
    index.write_flush = delayed
    try:
        assert index.start_flush()
        assert started.wait(5)
        assert not index.start_flush()
        assert cat.stats()['outbox'] == 1
        # Same file version, newer caption: the first commit must not lose it.
        caption_job = {'asset_id': job['asset_id'], 'file_version': 1}
        cat.complete_caption(caption_job, {'description': 'new caption', 'objects': [], 'actions': [], 'uncertainties': []})
        gate.set()
        index.collect_flush(wait=True)
        assert cat.stats()['outbox'] == 1
        index.write_flush = write
        index.flush_all()
        assert cat.stats()['outbox'] == 0
        assert index.table.count_rows() == 1
        assert 'new caption' in index.table.to_arrow().column('search_text')[0].as_py()
    finally:
        gate.set()
        engine.close()


def test_failed_async_write_keeps_durable_work_for_retry(tmp_path):
    engine = make_engine(tmp_path, count=1)
    cat, index = engine.catalog, engine.index
    job = cat.next_job(('embedding',))
    cat.complete_embedding(job, np.ones(768, np.float32)/np.sqrt(768))
    write = index.write_flush
    def fail(payload):
        raise OSError('simulated index failure')
    index.write_flush = fail
    try:
        import pytest
        index.start_flush()
        with pytest.raises(OSError, match='simulated index failure'):
            index.collect_flush(wait=True)
        assert cat.stats()['outbox'] == 1
        index.write_flush = write
        index.flush_all()
        assert cat.stats()['outbox'] == 0 and index.table.count_rows() == 1
    finally:
        engine.close()


def test_search_waits_for_inflight_index_commit(tmp_path):
    engine = make_engine(tmp_path, count=1)
    cat, index = engine.catalog, engine.index
    job = cat.next_job(('embedding',))
    vector = np.ones(768, np.float32)/np.sqrt(768)
    cat.complete_embedding(job, vector)
    gate = threading.Event()
    write = index.write_flush
    def delayed(payload):
        assert gate.wait(5)
        write(payload)
    index.write_flush = delayed
    release = threading.Timer(.05, gate.set)
    try:
        assert index.start_flush()
        release.start()
        rows = index.candidates(vector, '', Filters())
        assert [row['id'] for row in rows] == [job['asset_id']]
        assert index.writing is None and cat.stats()['outbox'] == 0
    finally:
        gate.set()
        release.join()
        engine.close()


def test_index_cleanup_preserves_search_and_cannot_lose_outbox_work(tmp_path, monkeypatch, caplog):
    engine = make_engine(tmp_path, count=1)
    cat, index = engine.catalog, engine.index
    try:
        job = cat.next_job(('embedding',))
        vector = np.ones(768, np.float32)/np.sqrt(768)
        cat.complete_embedding(job, vector)
        index.flush_all()
        index.cleanup_history(force=True)
        assert [r['id'] for r in index.candidates(vector, '', Filters())] == [job['asset_id']]
        def fail(**kwargs):
            raise OSError('simulated cleanup failure')
        monkeypatch.setattr(index.table, 'optimize', fail)
        index.last_cleanup -= 601
        cat.complete_caption({'asset_id': job['asset_id'], 'file_version': 1},
                             {'description': 'fresh', 'objects': [], 'actions': [], 'uncertainties': []})
        index.flush_all()
        assert cat.stats()['outbox'] == 0
        assert 'Search index history cleanup failed' in caplog.text
        assert 'fresh' in index.table.to_arrow().column('search_text')[0].as_py()
    finally:
        engine.close()
