"""Catalogue-owner side of the remote queue. No network IO on the owner thread."""
import base64
import io
import json
from pathlib import Path
import queue
import time
import numpy as np
from PIL import Image
from .remote_client import Transport
from .remote_protocol import MAX_PENDING


def claim_bundle(catalog, excluded=()):
    db = catalog.db
    skip = ' AND a.id NOT IN ('+','.join('?' for _ in excluded)+')' if excluded else ''
    # One ordered index seek per stage avoids sorting the entire archive for
    # every input. The separate asset/status index makes overlap checks cheap
    # even when thousands of remote jobs are already reserved.
    candidates=[]
    sql='''SELECT j.asset_id FROM jobs j INDEXED BY jobs_ready JOIN assets a ON a.id=j.asset_id
        WHERE j.status='pending' AND j.stage=? AND j.file_version=a.version
        AND a.present=1 AND a.metadata_ready=1
        AND NOT EXISTS(SELECT 1 FROM jobs busy WHERE busy.asset_id=a.id AND busy.status='running')'''+skip+'''
        ORDER BY j.asset_id DESC LIMIT 1'''
    for stage in ('embedding','faces','caption','location'):
        row=db.execute(sql,[stage,*excluded]).fetchone()
        if row:
            candidates.append(row[0])
    if not candidates:
        return None
    asset = catalog.get(max(candidates))
    jobs = []
    with db:
        if asset['media_kind']=='video':
            unit = db.execute('''SELECT u.id,u.timestamp_ms FROM units u JOIN unit_jobs j ON j.unit_id=u.id
                WHERE u.asset_id=? AND j.file_version=? AND j.status='pending'
                ORDER BY u.timestamp_ms LIMIT 1''',(asset['id'],asset['version'])).fetchone()
            for row in (db.execute("SELECT * FROM unit_jobs WHERE unit_id=? AND file_version=? AND status='pending'",(unit['id'],asset['version'])).fetchall() if unit else []):
                job = dict(row) | dict(asset_id=asset['id'],timestamp_ms=unit['timestamp_ms'])
                jobs.append(job)
                db.execute("UPDATE unit_jobs SET status='running' WHERE unit_id=? AND stage=?",(unit['id'],job['stage']))
            asset = catalog.media_units.asset_at(asset,unit['id'] if unit else None)
            location = db.execute("SELECT asset_id,stage,file_version,model_version FROM jobs WHERE asset_id=? AND stage='location' AND status='pending'",(asset['id'],)).fetchone()
            if location:
                jobs.append(dict(location))
        else:
            jobs = [dict(row) for row in db.execute("SELECT asset_id,stage,file_version,model_version FROM jobs WHERE asset_id=? AND status='pending' AND stage IN ('embedding','faces','caption','location')",(asset['id'],))]
        for job in jobs:
            db.execute("UPDATE jobs SET status='running',attempts=attempts+1 WHERE asset_id=? AND stage=?",(asset['id'],job['stage']))
    return asset,jobs


def vector(value, size):
    array = np.asarray(value,dtype=np.float32)
    if array.shape!=(size,) or not np.isfinite(array).all() or not .98<float(np.linalg.norm(array))<1.02:
        raise ValueError('Некорректный вектор сервера')
    return array


def decode_faces(records):
    if not isinstance(records,list) or len(records)>1000:
        raise ValueError('Некорректный список лиц')
    result = []
    for face in records:
        box = np.asarray(face['box'],dtype=float)
        points = np.asarray(face['landmarks'],dtype=float)
        if box.shape!=(4,) or not np.isfinite(box).all() or (box<0).any() or (box>1).any() or (box[2:]<=box[:2]).any():
            raise ValueError('Некорректная рамка лица')
        if points.shape!=(5,2) or not np.isfinite(points).all() or not 0<=face['confidence']<=1:
            raise ValueError('Некорректные точки лица')
        with Image.open(io.BytesIO(base64.b64decode(face['portrait'],validate=True))) as portrait:
            if portrait.size!=(112,112):
                raise ValueError('Некорректный портрет')
            result.append(face | dict(vector=vector(face['vector'],128),portrait=portrait.convert('RGB')))
    return result


class RemoteJobs:
    def __init__(self, engine, alive):
        self.engine, self.catalog = engine, engine.catalog
        self.pending = {}
        self.excluded = {}
        self.serial = 0
        self.retry_at = 0
        self.next_fill = 0
        self.last_error = ''
        self.catalog.db.execute('''CREATE TABLE IF NOT EXISTS remote_runs(
            asset_id INTEGER,file_version INTEGER,unit_id TEXT,stage TEXT,model_version TEXT,
            job_key TEXT,provider TEXT,elapsed REAL,PRIMARY KEY(asset_id,file_version,unit_id,stage))''')
        self.catalog.db.commit()
        self.transport = Transport(engine.cfg,alive,engine.emit)

    def set_active(self, value):
        self.transport.active = value

    def fill(self):
        if not self.engine.cfg.remote_enabled or not self.transport.active or not self.transport.connected.is_set() or time.monotonic()<max(self.retry_at,self.next_fill):
            return
        now = time.monotonic()
        self.excluded = {asset_id:until for asset_id,until in self.excluded.items() if until>now}
        # Fill a useful reserve before the next slow local GPU call, while
        # bounding both coordinator latency and prepared metadata in memory.
        deadline = time.monotonic()+.015
        slots = min(16,MAX_PENDING-len(self.pending),self.transport.queue.maxsize-self.transport.queue.qsize())
        for _ in range(slots):
            if time.monotonic()>=deadline:
                break
            bundle = claim_bundle(self.catalog,self.excluded)
            if not bundle:
                self.next_fill = time.monotonic()+3
                break
            asset,jobs = bundle
            self.serial += 1
            self.pending[self.serial] = bundle
            self.transport.queue.put_nowait((self.serial,asset,{job['stage']:job['model_version'] for job in jobs}))

    def collect(self):
        completed = 0
        deadline = time.monotonic()+.025
        for _ in range(32):
            if time.monotonic()>=deadline:
                break
            try:
                serial,result,error = self.transport.results.get_nowait()
            except queue.Empty:
                break
            bundle = self.pending.pop(serial,None)
            if not bundle:
                continue
            asset,jobs = bundle
            receipt = 'saved'
            try:
                if error:
                    raise ValueError(error)
                info = Path(asset['path']).stat()
                if (info.st_size,info.st_mtime_ns)!=(asset['size'],asset['mtime_ns']):
                    raise ValueError('Исходный файл изменился')
                for job in jobs:
                    if not self.catalog.current_job(job):
                        receipt = 'discarded'
                        continue
                    # Interactive face recognition may have finished this stage
                    # while the same input was travelling to the server.
                    if job.get('unit_id'):
                        row = self.catalog.db.execute('SELECT status FROM unit_jobs WHERE unit_id=? AND stage=? AND file_version=?',(job['unit_id'],job['stage'],job['file_version'])).fetchone()
                    else:
                        row = self.catalog.db.execute('SELECT status FROM jobs WHERE asset_id=? AND stage=? AND file_version=?',(job['asset_id'],job['stage'],job['file_version'])).fetchone()
                    if not row or row[0]!='running':
                        if not row or row[0]!='done':
                            receipt = 'discarded'
                        continue
                    stage = job['stage']
                    if stage not in result['stages']:
                        receipt = 'partial'
                        self.catalog.requeue_jobs([job])
                        self.defer(asset['id'])
                        self.engine.metrics.record(job,result.get('errors',{}).get(stage,'Неполный результат'),'remote')
                        continue
                    value = result['stages'][stage]
                    if stage=='embedding':
                        self.catalog.complete_embedding(job,vector(value,768))
                    elif stage=='faces':
                        self.catalog.complete_faces(job,decode_faces(value))
                    elif stage=='caption':
                        if not isinstance(value.get('description'),str) or len(value['description'])>12000:
                            raise ValueError('Некорректное описание сервера')
                        self.catalog.complete_caption(job,value)
                    elif stage=='location':
                        location = value['location']
                        geo_vector = vector(value['vector'],768) if value['vector'] is not None else None
                        if bool(location['geo_text']) != (geo_vector is not None):
                            raise ValueError('Неполный результат обработки места')
                        self.catalog.complete_location(job,location,geo_vector)
                    self.catalog.finish_job(job,result['elapsed'],source='remote')
                    with self.catalog.db:
                        self.catalog.db.execute('INSERT OR REPLACE INTO remote_runs VALUES(?,?,?,?,?,?,?,?)',
                            (asset['id'],asset['version'],job.get('unit_id',''),stage,job['model_version'],result['key'],json.dumps(result['providers'].get(stage)),result['elapsed']))
                    completed += 1
            except Exception as exc:
                receipt = 'discarded'
                self.catalog.requeue_jobs(jobs)
                # One changed/unreadable input must not stop refilling the
                # entire server for a minute. Network retries live in Transport.
                self.defer(asset['id'])
                self.last_error = str(exc)[:300]
                self.engine.emit(dict(type='remote_notice',message=str(exc)[:300]))
            if result and result.get('key'):
                self.transport.acknowledge(result['key'],receipt)
        if completed:
            self.engine.flush_progress(completed)
        return completed

    def defer(self, asset_id):
        self.excluded[asset_id] = time.monotonic()+60
        while len(self.excluded)>2048:
            self.excluded.pop(next(iter(self.excluded)))

    def close(self):
        self.transport.close()
        self.cancel()

    def cancel(self):
        self.transport.cancel()
        # A large pre-upload reserve must not mean thousands of commits when
        # disabling the server or closing the application.
        self.catalog.requeue_jobs([job for _,jobs in self.pending.values() for job in jobs])
        self.pending.clear()
