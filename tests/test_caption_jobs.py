import threading
import time

from PIL import Image
import pytest

from fotoarchive.catalog import Catalog
from fotoarchive.inference import GPUUnavailable
from test_gpu_pipeline import make_engine, PreparedEmbedder


class SlowVision:
    def __init__(self, error=None):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.closed = False
        self.error = error

    def describe(self, path):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(5)
        if self.error:
            raise self.error
        return {'description': 'Visible scene', 'objects': [], 'actions': [], 'uncertainties': []}

    def close(self):
        self.closed = True


def test_only_one_caption_runs_while_directml_jobs_continue_and_pause_drains_it(tmp_path):
    engine = make_engine(tmp_path, count=4)
    vision = engine.vlm = SlowVision()
    engine.embedder = PreparedEmbedder()
    captions = engine.pipeline.captions
    try:
        assert captions.fill() and vision.entered.wait(2)
        assert not captions.fill() and vision.calls == 1
        assert not engine.pipeline.idle
        engine.pipeline.inputs.fill()
        deadline = time.monotonic() + 3
        while not engine.process_embedding_batch():
            assert time.monotonic() < deadline
            time.sleep(.01)
        assert engine.catalog.stats()['embeddings'] > 0
        assert engine.catalog.stats()['captions'] == 0
        captions.cancel()
        assert captions.pending  # finish the current photo, no second request
        vision.release.set()
        assert captions.collect(wait=True)
        assert not captions.pending and vision.calls == 1
        assert engine.catalog.stats()['captions'] == 1
        assert engine.catalog.db.execute("SELECT count(*) FROM jobs WHERE stage='caption' AND status='pending'").fetchone()[0] == 3
    finally:
        vision.release.set()
        engine.close()
    assert vision.closed


def test_late_caption_cannot_describe_a_new_file_version(tmp_path):
    engine = make_engine(tmp_path, count=1)
    vision = engine.vlm = SlowVision()
    captions = engine.pipeline.captions
    try:
        captions.fill()
        assert vision.entered.wait(2)
        path = engine.cfg.root / '0.jpg'
        Image.new('RGB', (120, 100), 'red').save(path)
        asset_id, changed = engine.catalog.register(path)
        assert changed
        vision.release.set()
        captions.collect(wait=True)
        asset = engine.catalog.get(asset_id)
        assert asset['version'] == 2 and not asset['description']
        assert engine.catalog.db.execute("SELECT status FROM jobs WHERE stage='caption'").fetchone()[0] == 'pending'
    finally:
        vision.release.set()
        engine.close()


def test_shutdown_saves_current_caption_before_closing_database(tmp_path):
    engine = make_engine(tmp_path, count=1)
    cfg = engine.cfg
    vision = engine.vlm = SlowVision()
    engine.pipeline.captions.fill()
    assert vision.entered.wait(2)
    vision.release.set()
    engine.close()
    assert vision.closed and not engine.lock.is_locked
    cat = Catalog(cfg)
    try:
        assert cat.stats()['captions'] == 1
        assert cat.db.execute("SELECT status FROM jobs WHERE stage='caption'").fetchone()[0] == 'done'
    finally:
        cat.close()


def test_gpu_failure_during_shutdown_still_closes_models_and_releases_lock(tmp_path):
    engine = make_engine(tmp_path, count=1)
    vision = engine.vlm = SlowVision(GPUUnavailable('GPU failed'))
    engine.pipeline.captions.fill()
    assert vision.entered.wait(2)
    vision.release.set()
    with pytest.raises(GPUUnavailable, match='GPU failed'):
        engine.close()
    assert vision.closed and not engine.lock.is_locked
    cat = Catalog(engine.cfg)
    try:
        assert cat.db.execute("SELECT status FROM jobs WHERE stage='caption'").fetchone()[0] == 'error'
    finally:
        cat.close()
