"""Read-only GPU probe on real examples and artificial rotations of copies."""
import json
from pathlib import Path
import sqlite3
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import ImageDraw, ImageFont
from fotoarchive.config import Settings
from fotoarchive.faces import FaceModels
from fotoarchive.inference import VisionLanguage
from fotoarchive.media import open_rgb
from fotoarchive.orientation import OrientationAnalyzer, rotate_clockwise


def main():
    cfg = Settings.load()
    output = cfg.data_dir / "reports/orientation_probe"
    output.mkdir(exist_ok=True)
    database = sqlite3.connect(f"file:{(cfg.data_dir/'catalog.sqlite3').as_posix()}?mode=ro", uri=True)
    detector, vision = FaceModels(cfg), VisionLanguage(cfg)
    analyzer = OrientationAnalyzer(detector, vision)
    report = []
    # Ground truth determined by reviewing the actual photographs.
    samples = [(4, 90), (8, 90), (12, 90), (14, 90), (89, 90), (221, 90), (171, 0), (27, 0)]
    try:
        for asset_id, expected in samples:
            path = Path(database.execute("SELECT path FROM assets WHERE id=?", (asset_id,)).fetchone()[0])
            start = time.perf_counter()
            result = analyzer.analyze(path)
            row = {"asset_id":asset_id,"expected":expected,"seconds":time.perf_counter()-start,**result}
            report.append(row)
            print(json.dumps(row, ensure_ascii=True), flush=True)
        upright = open_rgb(Path(database.execute("SELECT path FROM assets WHERE id=191").fetchone()[0]))
        for rotation in (90, 180, 270):
            sample = rotate_clockwise(upright, rotation)
            sample.thumbnail((1200,1200))
            draw = ImageDraw.Draw(sample)
            draw.text((20,sample.height-55), "2003. 06. 15", fill="yellow", font=ImageFont.load_default(size=35))
            path = output / f"synthetic_stamp_{rotation}.jpg"
            sample.save(path,quality=95)
            start=time.perf_counter()
            result=analyzer.analyze(path)
            row={"synthetic_rotation":rotation,"expected":(-rotation)%360,"seconds":time.perf_counter()-start,**result}
            report.append(row);print(json.dumps(row,ensure_ascii=True),flush=True)
    finally:
        vision.close()
        (output/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__ == '__main__':
    main()
