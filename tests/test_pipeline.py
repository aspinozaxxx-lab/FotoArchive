from concurrent.futures import Future
from pathlib import Path
import time

from PIL import Image

from fotoarchive.catalog import Catalog
from fotoarchive.config import Settings
from fotoarchive.engine import Engine
from fotoarchive.media import sha256
from fotoarchive.preparation import PreparationPool, prepare_photo
from fotoarchive.scanning import SourceScanner


def settings(tmp_path, workers=2):
    root=tmp_path/'source';root.mkdir()
    cfg=Settings(data_dir=tmp_path/'data',root=root,includes=['.'],preparation_workers=workers)
    cfg.initialize()
    return cfg


def test_real_cpu_process_pool_prepares_files_and_is_bounded(tmp_path):
    cfg=settings(tmp_path)
    prototype=cfg.root/'0.jpg'
    Image.effect_noise((1200,900),60).convert('RGB').save(prototype,quality=90)
    content=prototype.read_bytes()
    catalog=Catalog(cfg)
    for i in range(16):
        path=cfg.root/f'{i}.jpg';path.write_bytes(content);catalog.register(path)
    broken=cfg.root/'broken.jpg';broken.write_bytes(b'not an image');catalog.register(broken)
    pool=PreparationPool(catalog)
    try:
        assert pool.fill(limit=8)==8 and len(pool.pending)<=pool.capacity
        deadline=time.monotonic()+30
        while True:
            pool.collect();pool.fill()
            if not pool.pending:
                break
            assert time.monotonic()<deadline
            time.sleep(.01)
        assert catalog.stats()['metadata']==16 and catalog.stats()['errors']==1
        assert pool.process_ids and pool.completed==17
        assert all(sha256(path)==sha256(prototype) for path in cfg.root.glob('*.jpg') if path!=broken)
        # No pointless vector deletions for new files without any search vectors.
        assert catalog.stats()['outbox']==0
    finally:
        pool.close();catalog.close()


def test_late_preparation_cannot_overwrite_new_file_version(tmp_path):
    cfg=settings(tmp_path)
    path=cfg.root/'photo.jpg';Image.new('RGB',(100,60),'red').save(path)
    catalog=Catalog(cfg);asset_id,_=catalog.register(path)
    job=catalog.next_job(('metadata',));asset=catalog.get(asset_id)
    result=prepare_photo(asset,str(cfg.data_dir/'old.webp'))
    Image.new('RGB',(80,140),'blue').save(path)
    catalog.register(path)
    pool=PreparationPool(catalog);future=Future();future.set_result(result)
    pool.pending[future]=(job,asset)
    pool.collect()
    assert catalog.get(asset_id)['version']==2 and not catalog.get(asset_id)['metadata_ready']
    assert catalog.db.execute("SELECT status FROM jobs WHERE stage='metadata'").fetchone()[0]=='pending'
    pool.close();catalog.close()


def test_pause_requeues_not_started_jobs_and_keeps_version_guard(tmp_path):
    cfg=settings(tmp_path)
    path=cfg.root/'photo.jpg';Image.new('RGB',(100,60),'red').save(path)
    catalog=Catalog(cfg);asset_id,_=catalog.register(path)
    job=catalog.next_job(('metadata',));pool=PreparationPool(catalog)
    future=Future();pool.pending[future]=(job,catalog.get(asset_id))
    pool.cancel()
    assert future.cancelled() and not pool.pending
    assert catalog.db.execute("SELECT status FROM jobs WHERE stage='metadata'").fetchone()[0]=='pending'
    pool.close();catalog.close()


def test_directory_producer_backpressure_and_scan_restart(tmp_path):
    cfg=settings(tmp_path)
    for i in range(15):
        (cfg.root/f'{i}.jpg').write_bytes(b'file')
    scanner=SourceScanner(cfg,capacity=2)
    deadline=time.monotonic()+5
    while scanner.queue.qsize()<2:
        assert time.monotonic()<deadline
        time.sleep(.01)
    assert scanner.queue.qsize()==2
    scanner.close();assert not scanner.thread.is_alive()
    engine=Engine(cfg)
    engine.pipeline.request_scan()
    engine.close()
    engine=Engine(cfg)
    try:
        assert engine.pipeline.scanning
        while engine.pipeline.scanning:
            engine.pipeline.scan_tick(limit=3)
            time.sleep(.01)
        assert engine.catalog.stats()['total']==15
        assert engine.catalog.state('scan_queue')==[]
    finally:
        engine.close()
