"""Prepare small copies of selected media for the frozen-program GPU check."""
from dataclasses import replace
import json
from pathlib import Path
import shutil
import sys

import av
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fotoarchive.catalog import enumerate_source
from fotoarchive.config import Settings
from fotoarchive.media import open_rgb, sha256

cfg = Settings.load()
base = cfg.data_dir / 'reports/v060-media-runtime'
source = base / 'samples'
data = base / 'data'
source.mkdir(parents=True, exist_ok=True)
data.mkdir(parents=True, exist_ok=True)
chosen = {}
wanted = {'.jpg', '.cr2', '.arw', '.crw', '.avi', '.mov'}
for path, supported in enumerate_source(cfg):
    extension = path.suffix.lower()
    if extension not in wanted or extension in chosen or path.stat().st_size > 50*1024**2:
        continue
    if extension in {'.avi', '.mov'}:
        try:
            with av.open(str(path)) as video:
                seconds = (video.duration or 0) / av.time_base
                if not 0 < seconds <= 21:
                    continue
        except av.error.FFmpegError:
            continue
    chosen[extension] = path
    if len(chosen) == len(wanted):
        break
assert wanted == chosen.keys(), chosen.keys()
samples = []
for extension, path in chosen.items():
    target = source / ('sample' + extension)
    shutil.copy2(path, target)
    samples.append({'original': str(path), 'path': str(target), 'sha256': sha256(path)})
image = open_rgb(chosen['.jpg'])
image.save(source/'sample.heic', format='HEIF')
image.save(source/'sample.png')
for name in ('sample.heic', 'sample.png'):
    samples.append({'path': str(source/name), 'sha256': sha256(source/name)})
(data/'media-smoke-manifest.json').write_text(json.dumps({'source': str(source), 'samples': samples}, ensure_ascii=False, indent=2), encoding='utf-8')
(data/'settings.json').write_text(json.dumps({'root': str(source), 'includes': ['.'], 'preparation_workers': 2}, indent=2), encoding='utf-8')
print(json.dumps({'data_dir': str(data), 'samples': len(samples), 'bytes': sum(Path(row['path']).stat().st_size for row in samples)}))
