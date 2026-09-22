"""Durable per-frame progress while the catalogue and its counters remain per file."""
import json
from pathlib import Path

from .config import EMBED_VERSION, CAPTION_VERSION, FACE_VERSION
from .video import sample_times

UNIT_STAGES = {'embedding': EMBED_VERSION, 'caption': CAPTION_VERSION, 'faces': FACE_VERSION}
COLUMNS = {'embedding': 'embed_version', 'caption': 'caption_version', 'faces': 'face_version'}


class MediaUnits:
    def __init__(self, catalog, initialize=True):
        self.catalog, self.db = catalog, catalog.db
        if not initialize:
            return
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS unit_details(
              unit_id TEXT PRIMARY KEY REFERENCES units(id) ON DELETE CASCADE, file_version INTEGER NOT NULL,
              thumbnail TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',observations_json TEXT);
            CREATE TABLE IF NOT EXISTS unit_jobs(
              unit_id TEXT NOT NULL REFERENCES units(id) ON DELETE CASCADE,stage TEXT NOT NULL,
              file_version INTEGER NOT NULL,model_version TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
              error TEXT,elapsed REAL,PRIMARY KEY(unit_id,stage));
            CREATE INDEX IF NOT EXISTS unit_jobs_status ON unit_jobs(stage,status,unit_id);
            CREATE TABLE IF NOT EXISTS face_units(
              face_id TEXT PRIMARY KEY REFERENCES faces(id) ON DELETE CASCADE,
              unit_id TEXT NOT NULL REFERENCES units(id) ON DELETE CASCADE);
            CREATE INDEX IF NOT EXISTS face_units_unit ON face_units(unit_id);
        ''')

    def create(self, job, metadata):
        if metadata.get('media_kind') != 'video':
            return
        for timestamp in sample_times(metadata['duration_ms']):
            unit_id = f"{job['asset_id']}:frame:{timestamp}"
            target = self.catalog.cfg.data_dir / 'thumbnails' / str(job['asset_id']//1000) / f"{job['asset_id']}_{job['file_version']}_frame{timestamp}.webp"
            self.db.execute("INSERT OR IGNORE INTO units(id,asset_id,kind,timestamp_ms) VALUES(?,?,'frame',?)",
                            (unit_id, job['asset_id'], timestamp))
            self.db.execute('INSERT OR IGNORE INTO unit_details(unit_id,file_version,thumbnail) VALUES(?,?,?)',
                            (unit_id, job['file_version'], str(target)))
            self.db.executemany('INSERT OR IGNORE INTO unit_jobs(unit_id,stage,file_version,model_version) VALUES(?,?,?,?)',
                               [(unit_id, stage, job['file_version'], version) for stage, version in UNIT_STAGES.items()])

    def claim(self, job):
        row = self.db.execute('''SELECT j.unit_id,u.timestamp_ms FROM unit_jobs j JOIN units u ON u.id=j.unit_id
            WHERE u.asset_id=? AND j.stage=? AND j.file_version=? AND j.status='pending'
            ORDER BY u.timestamp_ms LIMIT 1''', (job['asset_id'], job['stage'], job['file_version'])).fetchone()
        if row:
            self.db.execute("UPDATE unit_jobs SET status='running' WHERE unit_id=? AND stage=?", (row['unit_id'], job['stage']))
            return job | dict(row)
        return None

    def finish(self, job, elapsed, error):
        self.db.execute('UPDATE unit_jobs SET status=?,error=?,elapsed=? WHERE unit_id=? AND stage=? AND file_version=?',
                        ('error' if error else 'done', error, elapsed, job['unit_id'], job['stage'], job['file_version']))
        pending, errors, total_elapsed = self.db.execute('''SELECT sum(j.status IN ('pending','running')),
            sum(j.status='error'),sum(j.elapsed) FROM unit_jobs j JOIN units u ON u.id=j.unit_id
            WHERE u.asset_id=? AND j.stage=? AND j.file_version=?''', (job['asset_id'], job['stage'], job['file_version'])).fetchone()
        state = 'pending' if pending else 'error' if errors else 'done'
        message = f'Не удалось обработать кадров: {errors}. Повторите задания с ошибками.' if errors else None
        self.db.execute('UPDATE jobs SET status=?,error=?,elapsed=? WHERE asset_id=? AND stage=? AND file_version=?',
                        (state, message, total_elapsed, job['asset_id'], job['stage'], job['file_version']))
        if state == 'done':
            self.db.execute(f'UPDATE assets SET {COLUMNS[job["stage"]]}=? WHERE id=? AND version=?',
                            (UNIT_STAGES[job['stage']], job['asset_id'], job['file_version']))

    def asset_at(self, asset, unit_id=None):
        if not asset:
            return asset
        result = dict(asset)
        result['_cache_dir'] = str(self.catalog.cfg.data_dir / 'previews')
        result['_preview_budget'] = self.catalog.cfg.preview_budget
        if asset.get('media_kind') != 'video':
            return result
        where, values = ('u.id=?', [unit_id]) if unit_id else ('u.asset_id=?', [asset['id']])
        unit = self.db.execute('''SELECT u.id,u.timestamp_ms,d.* FROM units u JOIN unit_details d ON d.unit_id=u.id
            WHERE ''' + where + ' AND u.asset_id=? AND d.file_version=? ORDER BY u.timestamp_ms LIMIT 1',
            values + [asset['id'], asset['version']]).fetchone()
        if unit:
            result.update(unit_id=unit['id'], timestamp_ms=unit['timestamp_ms'], unit_thumbnail=unit['thumbnail'])
            if Path(unit['thumbnail']).exists():
                result['thumbnail'] = unit['thumbnail']
            result['description'] = unit['description']
            result['observations_json'] = unit['observations_json']
        return result
