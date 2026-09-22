"""Repair generated catalogue entries after decoder fixes; never write sources."""
from pathlib import Path

from .media_types import is_appledouble


def apply(catalog):
    excluded = []
    # Retry inaccessible candidates at the next start, instead of treating an
    # unavailable disk as evidence that an original should leave the catalogue.
    for row in catalog.db.execute("SELECT id,path,version FROM assets WHERE present=1 AND filename GLOB '._*'").fetchall():
        try:
            sidecar = is_appledouble(Path(row['path']))
        except OSError:
            continue
        if sidecar:
            with catalog.db:
                catalog.db.execute('UPDATE assets SET present=0 WHERE id=?', (row['id'],))
                catalog.db.execute('DELETE FROM jobs WHERE asset_id=?', (row['id'],))
                catalog._enqueue(row['id'], row['version'])
            excluded.append(row['id'])
    if not catalog.state('psd_decoder_v1', False):
        with catalog.db:
            catalog.db.execute("""UPDATE jobs SET status='pending',error=NULL WHERE status='error'
                AND asset_id IN (SELECT id FROM assets WHERE present=1 AND extension IN ('.psd','.psb'))""")
            catalog.set_state('psd_decoder_v1', True)
    return excluded
