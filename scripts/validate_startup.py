"""Repeat process starts against an existing catalogue; never scan originals."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fotoarchive.config import Settings
from fotoarchive.browse_reader import BrowseViews
from fotoarchive.preferences import Preferences


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--exe')
    parser.add_argument('--runs', type=int, default=5)
    args = parser.parse_args()
    cfg = Settings.load(args.data_dir)
    state = Preferences(cfg.data_dir).values.get('browser_state', {})
    presentation = dict(stacks=True, seconds=2, versions=True, sort='newest', expanded=[])
    presentation.update(state.get('presentation', {}))
    views = BrowseViews(cfg)
    try:
        _, session, _ = views.select(dict(id=0, filters=state.get('filters', {}),
            presentation=presentation), lambda: False)
        session.page()
        views.cache.save(session, views.cache_key, views.revision, lambda: False)
    finally:
        views.close()
    prefix = [args.exe] if args.exe else [sys.executable, '-m', 'fotoarchive']
    report = dict(method='Fresh processes, existing SSD layout cache and OS caches; no OS cache purge or source enumeration', runs=[])
    for _ in range(args.runs):
        started = time.perf_counter()
        result = subprocess.run(prefix+['--startup-benchmark', '--data-dir', str(cfg.data_dir)],
                                cwd=ROOT, capture_output=True, text=True, timeout=40,
                                env=os.environ | {'FOTOARCHIVE_BENCHMARK_STARTED': str(started)})
        row = json.loads((cfg.data_dir/'reports/startup-benchmark.json').read_text())
        row['process_wall_seconds'] = time.perf_counter()-started
        row['exit_code'] = result.returncode
        report['runs'].append(row)
        print(json.dumps(row), flush=True)
    for field in ('window_seconds', 'first_thumbnail_seconds', 'max_ui_tick_ms'):
        values = sorted(row.get(field, float('inf')) for row in report['runs'])
        report[field+'_p95'] = values[round((len(values)-1)*.95)]
    report['passed'] = all(r['passed'] and r['exit_code'] == 0 for r in report['runs']) and report['first_thumbnail_seconds_p95'] < 2
    (cfg.data_dir/'reports/startup-restarts.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
