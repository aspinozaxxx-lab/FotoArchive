import json
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fotoarchive.config import Settings
from fotoarchive.engine import Engine

engine = Engine(Settings.load())
started = time.perf_counter()
try:
    processed = 0
    while job := engine.process_one(stages=("location", "faces")):
        processed += 1
        if processed % 100 == 0:
            print(json.dumps({"processed": processed, "stage": job["stage"], "elapsed": round(time.perf_counter()-started, 1)}), flush=True)
    engine.index.flush_all()
    engine.index.maintain()
    report = {"stats": engine.catalog.stats(), "new_jobs": processed, "elapsed_seconds": time.perf_counter() - started,
              "detected_faces": engine.catalog.db.execute("SELECT count(*) FROM faces").fetchone()[0],
              "located_images": engine.catalog.db.execute("SELECT count(*) FROM assets WHERE latitude IS NOT NULL OR geo_text<>''").fetchone()[0]}
    (engine.cfg.data_dir / "reports/v02_index.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
finally:
    engine.close()
