"""Bounded, resumable indexing of the configured subtrees, never the whole archive by default."""
import json
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fotoarchive.config import Settings
from fotoarchive.engine import Engine

cfg = Settings.load()
if cfg.includes != ["2003"] and "--allow-configured-folders" not in sys.argv:
    raise SystemExit("This validation command only indexes the 2003 MVP")
engine = Engine(cfg)
report = {"started": time.time(), "includes": cfg.includes}
try:
    for scan in engine.scan():
        pass
    report["scan"] = scan
    print(json.dumps(scan), flush=True)
    steps = 0
    while True:
        job = engine.process_one()
        if not job:
            break
        steps += 1
        if steps % 10 == 0 or job["stage"] == "caption":
            print(json.dumps(engine.catalog.stats()), flush=True)
    engine.index.flush_all()
    engine.index.maintain()
    report["stats"] = engine.catalog.stats()
    report["elapsed_seconds"] = time.time() - report["started"]
    report["stage_timings"] = [dict(r) for r in engine.catalog.db.execute("SELECT stage,count(*) files,sum(elapsed) total_seconds,avg(elapsed) average_seconds FROM jobs WHERE status='done' GROUP BY stage")]
    report["errors"] = engine.catalog.errors()
    (cfg.data_dir / "reports/index_mvp.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2), flush=True)
finally:
    engine.close()
