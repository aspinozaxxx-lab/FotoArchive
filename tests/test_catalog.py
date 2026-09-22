import sqlite3
import numpy as np
import pytest
from PIL import Image
from fotoarchive.catalog import Catalog, Filters, enumerate_source
from fotoarchive.config import Settings
from fotoarchive.engine import Engine
from fotoarchive.media import extract_metadata, sha256, PreviewCache
from fotoarchive.search import SearchIndex

@pytest.fixture
def cfg(tmp_path):
    root = tmp_path / "originals"
    (root / "2003").mkdir(parents=True)
    return Settings(data_dir=tmp_path / "data", root=root)

def make_image(cfg, name="photo.jpg", original="2003:05:09 12:30:45", folder="2003"):
    path = cfg.root / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (64, 48), "red")
    exif = Image.Exif()
    if original:
        exif[34665] = {36867: original}
    image.save(path, exif=exif)
    return path

def prepare(catalog, path, vector=None):
    asset_id, _ = catalog.register(path)
    job = {"asset_id": asset_id, "file_version": catalog.get(asset_id)["version"], "stage": "metadata"}
    catalog.complete_metadata(job, extract_metadata(path), path, sha256(path))
    catalog.finish_job(job, 0.1)
    if vector is not None:
        job["stage"] = "embedding"
        catalog.complete_embedding(job, vector)
        catalog.finish_job(job, 0.1)
    return asset_id

def test_only_enabled_subtree_and_supported_media(cfg):
    make_image(cfg)
    make_image(cfg, folder="2004")
    (cfg.root / "2003" / "notes.xmp").write_text("sidecar")
    found = list(enumerate_source(cfg))
    assert len(found) == 2
    assert sum(supported for _, supported in found) == 1
    cfg.includes = ["../outside"]
    with pytest.raises(ValueError):
        list(enumerate_source(cfg))

@pytest.mark.parametrize("date_value,expected", [("2002:01:03 03:21:25", "2002-01-03T03:21:25"), (None, None), ("0000:00:00 00:00:00", None)])
def test_exif_is_authoritative(cfg, date_value, expected):
    assert extract_metadata(make_image(cfg, original=date_value))["captured_at"] == expected

def test_unchanged_scan_and_changed_file_invalidation(cfg):
    path = make_image(cfg)
    cat = Catalog(cfg)
    before = sha256(path)
    asset_id = prepare(cat, path, np.ones(768, dtype=np.float32))
    cat.complete_caption({"asset_id": asset_id, "file_version": 1}, {"description": "Красный кадр"})
    assert cat.register(path) == (asset_id, False)
    assert cat.get(asset_id)["version"] == 1
    assert sha256(path) == before
    Image.new("RGB", (80, 60), "blue").save(path)
    assert cat.register(path) == (asset_id, True)
    assert cat.get(asset_id)["version"] == 2
    assert cat.get(asset_id)["description"] == ""
    assert cat.vector(asset_id) is None
    assert cat.stats()["pending"] == 5
    cat.close()

def test_crash_recovery_and_missing_source_preserve_catalog(cfg):
    cat = Catalog(cfg)
    prepare(cat, make_image(cfg))
    job = cat.next_job()
    assert job["stage"] == "embedding"
    cat.close()
    cat = Catalog(cfg)
    cat.recover()
    assert cat.next_job()["asset_id"] == job["asset_id"]
    cfg.root.rename(cfg.root.with_name("disconnected"))
    with pytest.raises(FileNotFoundError):
        list(enumerate_source(cfg))
    assert cat.stats()["total"] == 1
    cat.close()

def test_outbox_crash_after_vector_commit_is_replayable(cfg):
    cat = Catalog(cfg)
    prepare(cat, make_image(cfg), np.ones(768, dtype=np.float32))
    index = SearchIndex(cat)
    cat.db.execute("CREATE TRIGGER simulate_crash BEFORE DELETE ON outbox BEGIN SELECT RAISE(ABORT,'simulated crash'); END")
    cat.db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        index.flush()
    assert index.table.count_rows() == 1
    assert cat.stats()["outbox"] == 1
    cat.db.execute("DROP TRIGGER simulate_crash")
    cat.db.commit()
    index.flush_all()
    assert index.table.count_rows() == 1
    assert cat.stats()["outbox"] == 0
    cat.close()

def test_prefilter_and_fts(cfg):
    cat = Catalog(cfg)
    v1 = np.zeros(768, dtype=np.float32); v1[0] = 1
    v2 = np.zeros(768, dtype=np.float32); v2[0:2] = [0.9,0.1]
    prepare(cat, make_image(cfg, "a.jpg", original="2002:01:01 10:00:00"), v1)
    b = prepare(cat, make_image(cfg, "b.jpg", original="2003:01:01 10:00:00", folder="2003/o'brien_%"), v2)
    cat.complete_caption({"asset_id": b, "file_version": 1}, {"description": "Спортсмены в спортивном зале"})
    index = SearchIndex(cat)
    index.flush_all()
    index.maintain()
    assert [r["id"] for r in index.candidates(v1, "", Filters(date_from="2003-01-01"), limit=1)] == [b]
    assert [r["id"] for r in index.candidates(v1, "спортсмены", Filters(folder="2003/o'brien_%"))] == [b]
    assert cat.browse(Filters(folder="2003/o'brien_%"))[1] == 1
    assert index.candidates(v1, "", Filters(folder="x' OR 1=1 --")) == []
    cat.close()

def test_corrupt_file_does_not_stop_other_metadata(cfg):
    make_image(cfg)
    (cfg.root / "2003" / "broken.jpg").write_bytes(b"broken")
    engine = Engine(cfg)
    list(engine.scan())
    engine.process_one()
    engine.process_one()
    assert engine.catalog.stats()["metadata"] == 1
    assert engine.catalog.stats()["errors"] == 1
    engine.close()

def test_preview_cache_enforces_budget(cfg):
    cat = Catalog(cfg)
    a = cat.get(prepare(cat, make_image(cfg, "a.jpg")))
    b = cat.get(prepare(cat, make_image(cfg, "b.jpg")))
    cache = PreviewCache(cfg.data_dir / "previews", budget=1)
    old = cache.get(a)
    new = cache.get(b)
    assert new.exists() and not old.exists()
    cat.close()

def test_only_one_catalog_worker(cfg):
    engine = Engine(cfg)
    try:
        with pytest.raises(RuntimeError, match="уже обрабатывается"):
            Engine(cfg)
    finally:
        engine.close()

def test_filename_search_handles_literal_unicode_and_quotes(cfg):
    cat=Catalog(cfg)
    a=prepare(cat,make_image(cfg,"день_рождения_03.jpg"))
    assert [r['id'] for r in cat.name_matches('рождения',Filters())] == [a]
    assert cat.name_matches('" OR *',Filters()) == []
    assert cat.name_matches('рождения',Filters(date_from='2004-01-01')) == []
    cat.close()

def test_verification_cache_invalidated_by_changed_original(cfg):
    engine = Engine(cfg)
    try:
        path=make_image(cfg)
        asset_id=prepare(engine.catalog,path)
        original=engine.catalog.get(asset_id)
        Image.new("RGB",(90,90),"blue").save(path)
        with pytest.raises(ValueError,match="изменился"):
            engine.verify(original,["Есть человек"])
    finally:
        engine.close()
