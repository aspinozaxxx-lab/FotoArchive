"""Read-only acceptance probe, runnable from the packaged executable."""
import json
import queue
import sys
import time

from .browse_reader import BrowseReader
from . import __version__


def run(cfg):
    events = queue.Queue()
    reader = BrowseReader(cfg, events.put)
    presentation = dict(stacks=True,seconds=2,versions=True,sort='newest')
    report = {'passed': False, 'version':__version__, 'scope': 'read-only catalogue reader with stacks; no model loading or original-file writes',
              'executable': sys.executable, 'frozen': bool(getattr(sys, 'frozen', False)),
              'switches': [], 'pages': []}
    try:
        reader.enable()
        for request_id, kind in enumerate(['video', 'photo', ''] * 4, 1):
            start = time.perf_counter()
            reader.replace(dict(action='browse', id=request_id, filters={'media_kind': kind},presentation=presentation))
            event = events.get(timeout=30)
            assert event['type'] == 'results', event.get('message')
            assert event['id'] == request_id
            report['switches'].append({'kind': kind or 'all', 'seconds': time.perf_counter()-start,
                                       'total': event['total'], 'items': len(event['items']), 'cached': event.get('cached', False)})
        for offset in (200, max(0, event['page_total']-200), 0):
            start = time.perf_counter()
            reader.page(dict(action='search_page', id=request_id, offset=offset, view=0))
            page = events.get(timeout=30)
            assert page['type'] == 'search_page', page.get('message')
            report['pages'].append({'offset': offset, 'seconds': time.perf_counter()-start, 'items': len(page['items'])})
        start = time.perf_counter()
        for request_id in range(100, 150):
            reader.replace(dict(action='browse', id=request_id,
                                filters={'media_kind': 'photo' if request_id % 2 == 0 else 'video'},presentation=presentation))
        while True:
            latest = events.get(timeout=30)
            assert latest['type'] != 'error', latest.get('message')
            if latest.get('id') == 149:
                break
        report['rapid_switch_seconds'] = time.perf_counter()-start
        samples = sorted(row['seconds'] for row in report['switches'][1:])
        report['warm_switch_p95_seconds'] = samples[int((len(samples)-1)*.95+.5)]
        cached = sorted(row['seconds'] for row in report['switches'] if row['cached'])
        report['cached_switch_p95_seconds'] = cached[int((len(cached)-1)*.95+.5)] if cached else None
        report['passed'] = report['warm_switch_p95_seconds'] < 2 and report['rapid_switch_seconds'] < 2
    except Exception as exc:
        report['error'] = str(exc)
    finally:
        reader.close()
        reader.thread.join(5)
    output = cfg.data_dir / 'reports/browse_benchmark.json'
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if report['passed'] else 1
