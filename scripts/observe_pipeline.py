"""Read-only, bounded sampling of the running application's durable progress."""
import argparse
import json
from pathlib import Path
import sqlite3
import time


def snapshot(path):
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        return {
            'at': time.time(),
            'jobs': [dict(row) for row in db.execute(
                "SELECT stage,count(*) n,sum(elapsed) elapsed FROM jobs WHERE status='done' GROUP BY stage")],
            'errors': [dict(row) for row in db.execute(
                "SELECT stage,count(*) n FROM jobs WHERE status='error' GROUP BY stage")],
            'running': [dict(row) for row in db.execute(
                "SELECT stage,count(*) n FROM jobs WHERE status='running' GROUP BY stage")],
            'outbox': db.execute('SELECT count(*) FROM outbox').fetchone()[0],
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=float, default=30)
    parser.add_argument('--label', default='v052-live')
    parser.add_argument('--data-dir', type=Path, default=Path(r'D:\FotoArchiveData'))
    args = parser.parse_args()
    samples = [snapshot(args.data_dir / 'catalog.sqlite3')]
    started = time.monotonic()
    while time.monotonic() - started < args.seconds:
        time.sleep(max(0, min(2, args.seconds - (time.monotonic() - started))))
        samples.append(snapshot(args.data_dir / 'catalog.sqlite3'))
    initial = {row['stage']: row for row in samples[0]['jobs']}
    elapsed = samples[-1]['at'] - samples[0]['at']
    changes = {row['stage']: {'completed': row['n'] - initial.get(row['stage'], {}).get('n', 0)}
               for row in samples[-1]['jobs']}
    report = {'seconds': elapsed, 'changes': changes, 'samples': samples}
    output = args.data_dir / 'reports' / f'{args.label}.json'
    output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({'report': str(output), 'seconds': elapsed, 'changes': changes,
                      'errors_before': samples[0]['errors'], 'errors_after': samples[-1]['errors']}, indent=2))


if __name__ == '__main__':
    main()
