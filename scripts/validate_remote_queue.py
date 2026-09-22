"""Measure remote claims against the isolated 200k catalogue from validate_v070_scale.py."""
import sys,time,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fotoarchive.config import Settings,CAPTION_VERSION
from fotoarchive.catalog import Catalog
from fotoarchive.remote_jobs import claim_bundle
base=Path('D:/FotoArchiveData/reports/v070-scale')
cfg=Settings.load(base/'catalog')
assert cfg.root.resolve().is_relative_to(base.resolve())
cat=Catalog(cfg)
assert cat.db.execute('select count(*) from assets').fetchone()[0]==200000
with cat.db:
    cat.db.execute("""INSERT OR IGNORE INTO jobs(asset_id,stage,file_version,model_version,status)
        SELECT id,'caption',version,?,'pending' FROM assets WHERE media_kind='photo'""",(CAPTION_VERSION,))
    cat.db.execute("UPDATE jobs SET status=CASE WHEN asset_id>191808 THEN 'running' ELSE 'pending' END WHERE stage='caption'")
times=[]
for _ in range(100):
    start=time.perf_counter()
    bundle=claim_bundle(cat)
    times.append((time.perf_counter()-start)*1000)
    assert bundle and bundle[1] and bundle[0]['id']<=191808
report=dict(assets=200000,existing_reserved=cat.db.execute("select count(*) from jobs where status='running'").fetchone()[0],
    samples=100,claim_p95_ms=sorted(times)[94],claim_max_ms=max(times))
cat.close()
print(json.dumps(report))
report_path=Path('D:/FotoArchiveData/reports/v081-scale/remote-claims.json')
report_path.parent.mkdir(parents=True,exist_ok=True)
report_path.write_text(json.dumps(report,indent=2))
