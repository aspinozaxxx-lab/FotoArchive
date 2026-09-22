"""One-time index compaction. Run only after the app and worker have exited."""
import hashlib
import json
import time
from datetime import timedelta

import lancedb
from filelock import FileLock

from fotoarchive.catalog import Catalog
from fotoarchive.config import Settings


def signature(db):
    digest = hashlib.sha256()
    for row in db.execute('SELECT id,path_key,version,size,mtime_ns,content_hash,present FROM assets ORDER BY id'):
        digest.update(json.dumps(tuple(row), ensure_ascii=False).encode())
    return digest.hexdigest()


def compact(cfg):
    report = {'tables': [], 'scope': 'index history only; application and worker stopped', 'passed': False}
    catalog = Catalog(cfg)
    try:
        report['source_signature_before'] = signature(catalog.db)
        report['quick_check'] = catalog.db.execute('PRAGMA quick_check').fetchone()[0]
        assert report['quick_check'] == 'ok'
        connection = lancedb.connect(str(cfg.data_dir / 'vectors'))
        for name in ('photos', 'faces', 'places'):
            table = connection.open_table(name)
            entry = {'table': name, 'rows_before': table.count_rows(), 'versions_before': len(table.list_versions())}
            samples = table.search().limit(8).to_list()
            entry['sample_count'] = len(samples)
            started = time.perf_counter()
            table.optimize(cleanup_older_than=timedelta(0), delete_unverified=True)
            entry['seconds'] = time.perf_counter() - started
            entry['rows_after'] = table.count_rows()
            entry['versions_after'] = len(table.list_versions())
            assert entry['rows_before'] == entry['rows_after'], entry
            if samples:
                ids = ','.join("'" + r['unit_id'].replace("'", "''") + "'" for r in samples)
                actual = table.search().where('unit_id IN (' + ids + ')').limit(len(samples)).to_list()
                assert sorted(samples, key=lambda r: r['unit_id']) == sorted(actual, key=lambda r: r['unit_id'])
            entry['samples_unchanged'] = True
            report['tables'].append(entry)
            print(json.dumps(entry), flush=True)
        report['source_signature_after'] = signature(catalog.db)
        assert report['source_signature_before'] == report['source_signature_after']
        report['passed'] = True
    finally:
        catalog.close()
        (cfg.data_dir / 'reports/v064-index-cleanup.json').write_text(json.dumps(report, indent=2), encoding='utf-8')


if __name__ == '__main__':
    cfg = Settings.load()
    with FileLock(cfg.data_dir / 'worker.lock', timeout=0):
        compact(cfg)
