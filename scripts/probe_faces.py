import json
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fotoarchive.config import Settings
from fotoarchive.faces import FaceModels

cfg = Settings.load()
model = FaceModels(cfg, profile=True)
path = cfg.root / "2003/08_08_03/007.JPG"
started = time.perf_counter()
faces = model.process(path)
report = {"file": str(path), "faces": len(faces), "seconds": time.perf_counter() - started}
for i, face in enumerate(faces):
    face["portrait"].save(cfg.data_dir / "reports" / f"face_probe_{i}.jpg")
report["profiles"] = model.finish_profile()
assert faces and all(v["providers"].get("DmlExecutionProvider", 0) for v in report["profiles"].values())
(cfg.data_dir / "reports/face_probe.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report, indent=2), flush=True)
