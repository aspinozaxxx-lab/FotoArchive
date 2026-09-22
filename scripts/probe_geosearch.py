"""Local GPS -> GeoNames -> actual SigLIP -> retrieval on isolated generated fixtures."""
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from fotoarchive.catalog import Catalog, Filters
from fotoarchive.config import Settings
from fotoarchive.inference import Embedder
from fotoarchive.location import Places, extract_location
from fotoarchive.media import extract_metadata, sha256
from fotoarchive.search import SearchIndex


cfg = Settings.load()
folder = cfg.data_dir / "reports/geosearch_fixture"
source = folder / "source"
source.mkdir(parents=True, exist_ok=True)
catalog = Catalog(Settings(data_dir=folder / "catalog", root=source))
embedder, places = Embedder(cfg), Places(cfg)
locations, ids = {}, {}
for label, (lat, lon) in {"Москва":(55.75222,37.61556), "Париж":(48.85341,2.3488)}.items():
    path = source / f"{len(ids)}.jpg"
    exif = Image.Exif()
    exif[34853] = {1:"N",2:(lat,0.,0.),3:"E",4:(lon,0.,0.)}
    Image.new("RGB", (120,80), "#888888").save(path, exif=exif)
    asset_id, _ = catalog.register(path)
    job = {"asset_id":asset_id,"file_version":catalog.get(asset_id)["version"]}
    catalog.complete_metadata(job, extract_metadata(path), path, sha256(path))
    visual = embedder.image(path)
    catalog.complete_embedding(job, visual)
    location = places.enrich(extract_location(path))
    catalog.complete_location(job, location, embedder.text(location["geo_text"]))
    assert (catalog.vector(asset_id) == visual).all()
    locations[label] = location
    ids[label] = asset_id
index = SearchIndex(catalog)
index.flush_all(); index.maintain()
queries = {}
for city, asset_id in ids.items():
    results = index.candidates(embedder.text(city), city, Filters())
    queries[city] = [a["id"] for a in results]
    assert results[0]["id"] == asset_id, (city, queries[city])
report = {"passed":True, "fixtures":"Identical gray pictures with different GPS; no archive originals", "locations":locations, "queries":queries}
(cfg.data_dir / "reports/v02_geosearch.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps(report,ensure_ascii=True,indent=2))
places.close(); catalog.close()
