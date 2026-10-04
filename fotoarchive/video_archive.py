"""Durable per-segment progress and searchable video descriptions."""
import json
from pathlib import Path

import numpy as np

from .config import EMBED_VERSION, FACE_VERSION, VIDEO_VERSION, VIDEO_SEGMENT_MS, VIDEO_PIXEL_BUDGET, VIDEO_INTERN_REV, QWEN_REV


def pipeline_version(cfg):
    return VIDEO_VERSION + ':' + cfg.video_model + (':'+VIDEO_INTERN_REV+':structured-ru:'+QWEN_REV if cfg.video_model=='internvideo' else '')


def initialize(catalog):
    catalog.db.executescript('''
      CREATE TABLE IF NOT EXISTS video_segments(
        unit_id TEXT PRIMARY KEY REFERENCES units(id) ON DELETE CASCADE,
        end_ms INTEGER NOT NULL,base_json TEXT,bindings_json TEXT NOT NULL DEFAULT '[]');
      CREATE TABLE IF NOT EXISTS video_people(
        unit_id TEXT NOT NULL REFERENCES units(id) ON DELETE CASCADE,
        person_id TEXT NOT NULL,PRIMARY KEY(unit_id,person_id));
      CREATE INDEX IF NOT EXISTS video_people_person ON video_people(person_id,unit_id);
      CREATE TABLE IF NOT EXISTS video_caption_vectors(unit_id TEXT PRIMARY KEY REFERENCES units(id) ON DELETE CASCADE,
        file_version INTEGER NOT NULL,model_version TEXT NOT NULL,vector BLOB NOT NULL);
    ''')
    columns = {r[1] for r in catalog.db.execute('PRAGMA table_info(unit_jobs)')}
    if 'attempts' not in columns:
        catalog.db.execute('ALTER TABLE unit_jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0')


def schedule(catalog, force=False):
    version = pipeline_version(catalog.cfg)
    with catalog.db:
        catalog.db.execute('''INSERT OR IGNORE INTO jobs(asset_id,stage,file_version,model_version)
            SELECT id,'video',version,? FROM assets WHERE present=1 AND media_kind='video' ''', (version,))
        catalog.db.execute('''UPDATE jobs SET status='pending',error=NULL,model_version=?
            WHERE stage='video' AND (model_version<>? OR ?)''', (version, version, int(force)))
        catalog.db.execute('''UPDATE unit_jobs SET status='pending',error=NULL,model_version=?
            WHERE stage='video' AND (model_version<>? OR ?)''', (version, version, int(force)))
        catalog.db.execute('''UPDATE jobs SET status='error',error=(SELECT error FROM jobs m
            WHERE m.asset_id=jobs.asset_id AND m.stage='metadata') WHERE stage='video' AND status='pending'
            AND asset_id IN (SELECT a.id FROM assets a JOIN jobs m ON m.asset_id=a.id
              WHERE a.metadata_ready=0 AND m.stage='metadata' AND m.status='error')''')
        # Old frame captions are superseded by chronological descriptions.
        # Keep the previous retrieval results visible until replacements arrive.
        catalog.db.execute("DELETE FROM unit_jobs WHERE stage='caption' AND unit_id IN (SELECT u.id FROM units u JOIN jobs j ON j.asset_id=u.asset_id WHERE j.stage='video')")
    return catalog.db.execute("SELECT count(*) FROM jobs WHERE stage='video' AND status='pending'").fetchone()[0]


def create_segments(catalog, asset):
    version = pipeline_version(catalog.cfg)
    count=min(VIDEO_SEGMENT_MS//1000,max(1,VIDEO_PIXEL_BUDGET//max(1,asset['width']*asset['height'])))
    step=count*1000
    starts=range(0,max(1,asset['duration_ms']),step)
    desired={f"{asset['id']}:segment:{t}" for t in starts}
    for old in catalog.db.execute("SELECT id FROM units WHERE asset_id=? AND kind='segment'",(asset['id'],)).fetchall():
        if old[0] not in desired:
            catalog._enqueue(asset['id'],asset['version'],old[0])
            catalog.db.execute('DELETE FROM embeddings WHERE unit_id=?',(old[0],))
            catalog.db.execute('DELETE FROM units WHERE id=?',(old[0],))
    for start in starts:
        unit = f"{asset['id']}:segment:{start}"
        catalog.db.execute("INSERT OR IGNORE INTO units(id,asset_id,kind,timestamp_ms) VALUES(?,?,'segment',?)",
                           (unit, asset['id'], start))
        catalog.db.execute('INSERT INTO video_segments(unit_id,end_ms) VALUES(?,?) ON CONFLICT(unit_id) DO UPDATE SET end_ms=excluded.end_ms',
                           (unit, min(start+step, asset['duration_ms'])))
        catalog.db.execute('INSERT OR IGNORE INTO unit_details(unit_id,file_version,thumbnail) VALUES(?,?,?)',
                           (unit, asset['version'], asset['thumbnail'] or ''))
        catalog.db.execute("INSERT OR IGNORE INTO unit_jobs(unit_id,stage,file_version,model_version) VALUES(?,'video',?,?)",
                           (unit, asset['version'], version))


def claim(catalog, asset_id=None, excluded=()):
    skip = ' AND a.id NOT IN ('+','.join('?' for _ in excluded)+')' if excluded else ''
    only = ' AND a.id=?' if asset_id is not None else ''
    row = catalog.db.execute('''SELECT a.* FROM jobs j INDEXED BY jobs_ready JOIN assets a ON a.id=j.asset_id
        WHERE j.stage='video' AND j.status='pending' AND a.present=1 AND a.metadata_ready=1
        AND a.version=j.file_version '''+skip+only+' ORDER BY a.id DESC LIMIT 1',
        [*excluded, *([asset_id] if asset_id is not None else [])]).fetchone()
    if not row:
        return None
    asset = dict(row)
    with catalog.db:
        create_segments(catalog, asset)
        segment = catalog.db.execute('''SELECT j.*,u.timestamp_ms,s.end_ms FROM unit_jobs j
            JOIN units u ON u.id=j.unit_id JOIN video_segments s ON s.unit_id=u.id
            WHERE u.asset_id=? AND j.stage='video' AND j.status='pending' AND j.file_version=?
            ORDER BY u.timestamp_ms LIMIT 1''', (asset['id'], asset['version'])).fetchone()
        if not segment:
            return None
        job = dict(segment) | dict(asset_id=asset['id'])
        catalog.db.execute("UPDATE unit_jobs SET status='running',attempts=attempts+1 WHERE unit_id=? AND stage='video'", (job['unit_id'],))
        catalog.db.execute("UPDATE jobs SET status='running',attempts=attempts+1 WHERE asset_id=? AND stage='video'", (asset['id'],))
    asset.update(unit_id=job['unit_id'], video_start_ms=job['timestamp_ms'], video_end_ms=job['end_ms'],
                 video_model=catalog.cfg.video_model)
    return asset, job


def normalized_vector(value):
    vector = np.asarray(value, dtype=np.float32)
    if vector.shape != (768,) or not np.isfinite(vector).all():
        raise ValueError('Некорректный эмбеддинг видео')
    norm = float(np.linalg.norm(vector))
    if not .98 < norm < 1.02:
        raise ValueError('Ненормализованный эмбеддинг видео')
    return vector


def save_description(catalog, asset, unit_id, base, bindings):
    from .video_people import render_names
    names = dict(catalog.db.execute('SELECT id,name FROM people'))
    result = render_names(base, bindings, names)
    persons = sorted({b['person_id'] for b in bindings if b.get('person_id') in names})
    result['people'] = [dict(id=key, name=names[key]) for key in persons]
    catalog.db.execute('UPDATE unit_details SET description=?,observations_json=? WHERE unit_id=? AND file_version=?',
        (result['description'], json.dumps(result, ensure_ascii=False), unit_id, asset['version']))
    catalog.db.execute('UPDATE video_segments SET base_json=?,bindings_json=? WHERE unit_id=?',
        (json.dumps(base, ensure_ascii=False), json.dumps(bindings, ensure_ascii=False), unit_id))
    catalog.db.execute('DELETE FROM video_people WHERE unit_id=?', (unit_id,))
    catalog.db.executemany('INSERT INTO video_people VALUES(?,?)', [(unit_id, key) for key in persons])
    catalog._enqueue(asset['id'], asset['version'], unit_id)


def complete(catalog, job, result):
    if not catalog.current_job(job) or job['model_version'] != pipeline_version(catalog.cfg):
        return False
    asset = catalog.get(job['asset_id'])
    start, end = job['timestamp_ms'], job['end_ms']
    frames = result.get('frames', [])
    expected = list(range(start, end, 1000))
    if [frame.get('timestamp_ms') for frame in frames] != expected:
        raise ValueError('Сервер вернул неполный отрезок видео')
    from .video_caption import validate_caption
    caption = validate_caption(result['caption'], start, end, result.get('audio_used', False))
    if not isinstance(caption.get('description'), str) or not 0 < len(caption['description']) <= 12000:
        raise ValueError('Некорректное описание видео')
    vector = normalized_vector(result['vector'])
    text_vector=normalized_vector(result['text_vector'])
    # Validate everything before creating any generated file or DB row.
    for frame in frames:
        normalized_vector(frame['vector'])
        if not isinstance(frame.get('faces'), list) or len(frame['faces']) > 100:
            raise ValueError('Некорректный список лиц видео')
        for face in frame['faces']:
            fv = np.asarray(face.get('vector'), dtype=np.float32)
            if fv.shape != (128,) or not np.isfinite(fv).all() or not .98 < np.linalg.norm(fv) < 1.02:
                raise ValueError('Некорректный эмбеддинг лица видео')
    bindings = result.get('bindings', [])
    if len(bindings) > 300 or any(not isinstance(b.get('token'), str) for b in bindings):
        raise ValueError('Некорректные привязки людей')
    with catalog.db:
        for frame in frames:
            timestamp = frame['timestamp_ms']
            unit_id = f"{asset['id']}:frame:{timestamp}"
            catalog.db.execute("INSERT OR IGNORE INTO units(id,asset_id,kind,timestamp_ms) VALUES(?,?,'frame',?)",
                               (unit_id, asset['id'], timestamp))
            thumbnail = catalog.cfg.data_dir/'thumbnails'/str(asset['id']//1000)/f"{asset['id']}_{asset['version']}_frame{timestamp}.webp"
            catalog.db.execute('INSERT OR IGNORE INTO unit_details(unit_id,file_version,thumbnail) VALUES(?,?,?)',
                               (unit_id, asset['version'], str(thumbnail)))
            frame_job = dict(asset_id=asset['id'], file_version=asset['version'], unit_id=unit_id, timestamp_ms=timestamp)
            catalog.complete_embedding(frame_job, normalized_vector(frame['vector']), commit=False)
            catalog.complete_faces(frame_job, frame['faces'], commit=False)
            catalog.db.executemany("INSERT OR REPLACE INTO unit_jobs(unit_id,stage,file_version,model_version,status) VALUES(?,?,?,?,'done')",
                [(unit_id, stage, asset['version'], version) for stage,version in (('embedding',EMBED_VERSION),('faces',FACE_VERSION))])
            if frame.get('thumbnail'):
                thumbnail.parent.mkdir(parents=True, exist_ok=True)
                temp = thumbnail.with_suffix('.tmp')
                frame['thumbnail'].save(temp, format='WEBP', quality=85)
                temp.replace(thumbnail)
        catalog.complete_embedding(job, vector, commit=False)
        catalog.db.execute('INSERT OR REPLACE INTO video_caption_vectors VALUES(?,?,?,?)',
            (job['unit_id'],asset['version'],EMBED_VERSION,text_vector.tobytes()))
        # Revalidate identity assignments against the current person examples.
        from .video_people import rebind, people_snapshot
        bindings = rebind(catalog, asset['id'], bindings, people_snapshot(catalog))
        caption.update(scope='segment', start_ms=start, end_ms=end,
                       model=result.get('model', ''), backend=result.get('backend', ''),
                       formatter_model=result.get('formatter_model', ''),
                       audio_used=result.get('audio_used', False),
                       uncertainties=caption.get('uncertainties', []))
        save_description(catalog, asset, job['unit_id'], caption, bindings)
        refresh_summary(catalog, asset['id'])
    return True


def finish(catalog, job, elapsed, error):
    catalog.db.execute("UPDATE unit_jobs SET status=?,error=?,elapsed=? WHERE unit_id=? AND stage='video' AND file_version=?",
        ('error' if error else 'done', error, elapsed, job['unit_id'], job['file_version']))
    pending, errors = catalog.db.execute('''SELECT count(*) FILTER(WHERE j.status IN ('pending','running')),
        count(*) FILTER(WHERE j.status='error') FROM unit_jobs j JOIN units u ON u.id=j.unit_id
        WHERE u.asset_id=? AND j.stage='video' AND j.file_version=?''', (job['asset_id'], job['file_version'])).fetchone()
    status = 'pending' if pending else 'error' if errors else 'done'
    catalog.db.execute("UPDATE jobs SET status=?,error=? WHERE asset_id=? AND stage='video' AND file_version=?",
        (status, f'Ошибок отрезков: {errors}' if errors else None, job['asset_id'], job['file_version']))
    if status == 'done':
        catalog.db.execute('UPDATE assets SET embed_version=?,face_version=?,caption_version=? WHERE id=? AND version=?',
            (EMBED_VERSION, FACE_VERSION, job['model_version'], job['asset_id'], job['file_version']))
        catalog.db.execute("UPDATE jobs SET status='done',error=NULL WHERE asset_id=? AND stage IN ('embedding','faces','caption')",
                           (job['asset_id'],))
    refresh_summary(catalog, job['asset_id'])


def refresh_summary(catalog, asset_id):
    rows = catalog.db.execute('''SELECT u.timestamp_ms,s.end_ms,d.description,d.observations_json,j.status FROM video_segments s
        JOIN units u ON u.id=s.unit_id JOIN unit_details d ON d.unit_id=u.id
        JOIN unit_jobs j ON j.unit_id=u.id AND j.stage='video' WHERE u.asset_id=?
        ORDER BY u.timestamp_ms''', (asset_id,)).fetchall()
    from .video import timestamp_text
    text = '\n'.join(f"{timestamp_text(r['timestamp_ms'])}–{timestamp_text(r['end_ms'])}: {r['description']}"
                     for r in rows if r['description'])[:12000]
    summary = dict(scope='video', segment_count=len(rows), completed=sum(r['status']=='done' for r in rows))
    details=[json.loads(row['observations_json'] or '{}') for row in rows]
    summary['people']=list({person['id']:person for detail in details for person in detail.get('people',[])}.values())
    for field in ('audio_description','speech'):
        summary[field]='\n'.join(f"{timestamp_text(row['timestamp_ms'])}: {detail[field]}" for row,detail in zip(rows,details) if detail.get(field))[:6000]
    catalog.db.execute('UPDATE assets SET description=?,observations_json=? WHERE id=?',
                       (text, json.dumps(summary), asset_id))


def refresh_person_names(catalog):
    from .video_people import people_snapshot, rebind
    people = people_snapshot(catalog)
    cursor = catalog.db.execute('''SELECT s.*,u.asset_id FROM video_segments s JOIN units u ON u.id=s.unit_id
        WHERE s.base_json IS NOT NULL ORDER BY u.asset_id,u.timestamp_ms''')
    for row in cursor:
        asset = catalog.get(row['asset_id'])
        bindings = rebind(catalog, asset['id'], json.loads(row['bindings_json']), people)
        with catalog.db:
            save_description(catalog, asset, row['unit_id'], json.loads(row['base_json']), bindings)
            # Each dense frame inherits the segment's text in the search index.
            catalog.db.executemany('INSERT INTO outbox(unit_id,revision) VALUES(?,?) ON CONFLICT(unit_id) DO UPDATE SET revision=outbox.revision+1',
                [(r[0],asset['version']) for r in catalog.db.execute("SELECT id FROM units WHERE asset_id=? AND kind='frame' AND timestamp_ms>=? AND timestamp_ms<?",
                    (asset['id'],catalog.db.execute('SELECT timestamp_ms FROM units WHERE id=?',(row['unit_id'],)).fetchone()[0],row['end_ms']))])
        yield row['asset_id']
    for row in catalog.db.execute("SELECT DISTINCT asset_id FROM units WHERE kind='segment'"):
        with catalog.db:
            refresh_summary(catalog, row[0])
        yield row[0]
