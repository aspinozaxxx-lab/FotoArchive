"""Read a small real sample without importing files or changing the originals."""
import json
import os
from pathlib import Path
import sys
import time
from dataclasses import replace
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('OMP_NUM_THREADS', '2')
from fotoarchive.config import Settings
from fotoarchive.catalog import enumerate_source
from fotoarchive.media import read_media, sha256
from fotoarchive.location import extract_location

cfg = Settings.load()
wanted = {'.cr2', '.arw', '.crw', '.heic', '.png', '.gif', '.tif', '.psd', '.mpo', '.cpt',
          '.mov', '.mp4', '.avi', '.mpg', '.mod', '.wmv', '.mts'}
samples = defaultdict(list)
for path, supported in enumerate_source(replace(cfg, includes=['.'])):
    extension = path.suffix.lower()
    if extension in wanted and len(samples[extension]) < 2:
        samples[extension].append(path)
results = []
output = cfg.data_dir / 'reports/v060-media-probe.json'
for extension, paths in sorted(samples.items()):
    for path in paths:
        start = time.perf_counter()
        before = sha256(path)
        result = {'extension': extension, 'path': str(path), 'sha256_before': before}
        try:
            metadata, image = read_media(path)
            result.update(metadata=metadata, decoded_size=list(image.size), location=extract_location(path), passed=True)
            image.close()
        except Exception as exc:
            result.update(passed=False, error=f'{type(exc).__name__}: {exc}')
        result.update(sha256_after=sha256(path), seconds=time.perf_counter()-start)
        result['original_unchanged'] = result['sha256_before'] == result['sha256_after']
        results.append(result)
        output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({key: result[key] for key in ('extension','passed','seconds','original_unchanged')} |
                         ({'error': result['error']} if 'error' in result else {})), flush=True)
print(str(output))
