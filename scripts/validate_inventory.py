"""Count enabled folders read-only and time progress queries on synthetic rows."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fotoarchive.catalog import Catalog
from fotoarchive.config import Settings
from fotoarchive.inventory import SourceInventory


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--label', default='v054-inventory')
    args = parser.parse_args()
    source = Settings.load()
    folder = Path(tempfile.mkdtemp(prefix=args.label + '-', dir=source.data_dir / 'reports'))
    cfg = Settings(data_dir=folder, root=source.root, includes=list(source.includes))
    cat = Catalog(cfg)
    inventory = SourceInventory(cat)
    started = time.perf_counter()
    try:
        inventory.ensure()
        while not inventory.collect():
            if time.perf_counter() - started > 120:
                raise TimeoutError('Source count did not finish')
            time.sleep(.02)
        result = inventory.status()
        assert result['phase'] == 'ready', result
        assert result['counts']['registered'] == 0
        report = {'source': str(source.root), 'includes': source.includes,
                  'total': result['total'], 'count_seconds': inventory.saved['seconds'],
                  'inventory': result,
                  'images_imported': 0, 'temporary_catalog': str(folder)}
    finally:
        inventory.close()
        cat.close()

    path = source.data_dir / 'reports/scale_200k/catalog/catalog.sqlite3'
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA temp_store=FILE')
        columns = {row[1] for row in db.execute('PRAGMA main.table_info(assets)')}
        missing = [name for name in ('face_version', 'geo_version') if name not in columns]
        if missing:
            # The existing synthetic fixture predates face/location support.
            # Supply those columns in a temporary view, keeping it read-only.
            db.execute('CREATE TEMP VIEW assets AS SELECT a.*,' + ','.join('NULL AS ' + name for name in missing) + ' FROM main.assets a')
        db.execute('CREATE TEMP TABLE source_inventory(path_key TEXT PRIMARY KEY) WITHOUT ROWID')
        db.execute('INSERT INTO source_inventory SELECT path_key FROM assets')
        probe = object.__new__(SourceInventory)
        probe.catalog = SimpleNamespace(db=db)
        probe.cfg = source
        probe.phase, probe.saved, probe.error = 'ready', {'total': 200000}, None
        probe.status()
        timings = []
        for _ in range(11):
            start = time.perf_counter()
            progress = probe.status()
            timings.append(time.perf_counter() - start)
            assert progress['counts']['scanned'] == 200000
        report['scale'] = {'rows': 200000, 'progress_p95_seconds': sorted(timings)[-1]}
    finally:
        db.close()
    report['passed'] = report['scale']['progress_p95_seconds'] < 2
    output = source.data_dir / 'reports' / (args.label + '.json')
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
