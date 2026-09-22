"""Read-only original integrity and no-op rescan verification."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fotoarchive.config import Settings
from fotoarchive.engine import Engine
from fotoarchive.media import sha256

cfg = Settings.load()
before = json.loads((cfg.data_dir / "reports/originals_before.json").read_text(encoding="utf-8"))
after = {}
for relative in before:
    path = cfg.root / relative
    stat = path.stat()
    after[relative] = {"sha256": sha256(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
report = {"originals": len(after), "originals_unchanged": before == after}
engine = Engine(cfg)
try:
    attempts_before = engine.catalog.db.execute("SELECT sum(attempts) FROM jobs").fetchone()[0]
    for scan in engine.scan():
        pass
    report["rescan"] = scan
    report["stats"] = engine.catalog.stats()
    report["no_reprocessing"] = scan["changed"] == 0 and engine.process_one() is None
    report["job_attempts_unchanged"] = attempts_before == engine.catalog.db.execute("SELECT sum(attempts) FROM jobs").fetchone()[0]
    report["dates"] = dict(engine.catalog.db.execute("SELECT coalesce(substr(captured_at,1,4),'unknown'),count(*) FROM assets GROUP BY 1"))
    report["formats"] = dict(engine.catalog.db.execute("SELECT extension,count(*) FROM assets GROUP BY extension"))
    report["passed"] = (report["originals_unchanged"] and report["no_reprocessing"] and report["job_attempts_unchanged"]
                        and report["dates"] == {"2002": 23, "2003": 150, "unknown": 32}
                        and report["formats"] == {".bmp": 17, ".jpg": 188} and scan["skipped"] == 14)
finally:
    engine.close()
(cfg.data_dir / "reports/originals_after.json").write_text(json.dumps(after, indent=2), encoding="utf-8")
(cfg.data_dir / "reports/mvp_validation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report, indent=2))
assert report["passed"]
