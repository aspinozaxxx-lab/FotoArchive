"""Extend the acceptance sample with the real 16-bit PSD and its AppleDouble."""
import json
from pathlib import Path
import shutil
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fotoarchive.media import sha256

root = Path(r'D:\FotoArchiveData')
base = root / 'reports/v061-media-runtime'
source, data = base/'samples', base/'data'
source.mkdir(parents=True, exist_ok=True)
data.mkdir(parents=True, exist_ok=True)
old = json.loads((root/'reports/v060-media-runtime/data/media-smoke-manifest.json').read_text(encoding='utf-8'))
samples = []
for entry in old['samples']:
    target = source/Path(entry['path']).name
    shutil.copy2(entry['path'], target)
    assert sha256(target) == entry['sha256']
    samples.append(entry | {'path': str(target)})
with sqlite3.connect((root/'catalog.sqlite3').as_uri()+'?mode=ro', uri=True) as db:
    name = db.execute("SELECT a.path FROM assets a JOIN jobs j ON j.asset_id=a.id WHERE a.extension='.psd' AND a.filename NOT GLOB '._*' AND j.stage='metadata' AND j.status='error' ORDER BY a.id LIMIT 1").fetchone()[0]
original = Path(name)
sidecar = original.with_name('._'+original.name)
sidecars = []
for path, filename, records in ((original, 'sample16.psd', samples), (sidecar, '._sample16.psd', sidecars)):
    digest = sha256(path)
    target = source/filename
    shutil.copy2(path, target)
    assert sha256(target) == digest
    records.append({'path': str(target), 'original': str(path), 'sha256': digest})
(data/'media-smoke-manifest.json').write_text(json.dumps({'source': str(source), 'samples': samples, 'sidecars': sidecars}, ensure_ascii=False, indent=2), encoding='utf-8')
(data/'settings.json').write_text(json.dumps({'root': str(source), 'includes': ['.'], 'preparation_workers': 2}, indent=2), encoding='utf-8')
print(json.dumps({'data': str(data), 'media_files': len(samples), 'sidecars': len(sidecars)}))
