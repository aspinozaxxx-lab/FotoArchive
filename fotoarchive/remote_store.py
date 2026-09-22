"""Server-only durable queue and capped opaque input/result cache."""
import json
import os
import sqlite3
import threading
import time
import zlib
from pathlib import Path
from .remote_protocol import digest, unpack, valid_key, job_key
from .config import EMBED_VERSION, FACE_VERSION, CAPTION_VERSION, GEO_VERSION

VERSIONS = dict(embedding=EMBED_VERSION, faces=FACE_VERSION, caption=CAPTION_VERSION, location=GEO_VERSION)


class Store:
    def __init__(self, directory, budget=180_000_000_000):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.budget = budget
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.directory/'queue.sqlite3', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        if self.db.execute('PRAGMA auto_vacuum').fetchone()[0]!=2:
            self.db.execute('PRAGMA auto_vacuum=INCREMENTAL')
            self.db.execute('VACUUM')
        self.db.execute('PRAGMA max_page_count=1048576')
        self.db.executescript('''PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS blobs(id TEXT PRIMARY KEY,size INTEGER,used REAL);
          CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,blob TEXT,session TEXT,stages TEXT,
            status TEXT,result BLOB,updated REAL);
          CREATE INDEX IF NOT EXISTS queue ON jobs(session,status,updated);
          CREATE INDEX IF NOT EXISTS by_blob ON jobs(blob,status);
          UPDATE jobs SET status='queued' WHERE status='running';''')
        if 'context' not in {row[1] for row in self.db.execute('PRAGMA table_info(jobs)')}:
            self.db.execute("ALTER TABLE jobs ADD COLUMN context TEXT NOT NULL DEFAULT '{}'")
        # Recover a crash between atomic file replacement and SQLite commit.
        for path in self.directory.glob('*.facache'):
            if valid_key(path.stem):
                self.db.execute('INSERT OR IGNORE INTO blobs VALUES(?,?,?)',
                                (path.stem, path.stat().st_size, path.stat().st_mtime))
        self.db.commit()

    def path(self, key):
        if not valid_key(key):
            raise ValueError('Invalid cache key')
        return self.directory/(key+'.facache')

    def has(self, key):
        with self.lock:
            return self.path(key).is_file()

    def put(self, key, data):
        if digest(data) != key:
            raise ValueError('Checksum mismatch')
        unpack(data)  # Validate in memory; no viewable images ever touch disk.
        with self.lock, self.db:
            if not self.has(key):
                self.trim(len(data))
                temp = self.path(key).with_suffix('.partial')
                temp.write_bytes(data)
                os.replace(temp, self.path(key))
            self.db.execute('INSERT OR REPLACE INTO blobs VALUES(?,?,?)', (key,len(data),time.time()))

    def trim(self, extra=0):
        total = self.db.execute('SELECT coalesce(sum(size),0) FROM blobs').fetchone()[0]
        total += self.db.execute('SELECT coalesce(sum(length(result)),0) FROM jobs').fetchone()[0]
        # Queued jobs from expired sessions can be reconstructed by the client;
        # only a running job must remain pinned during eviction.
        for row in self.db.execute("SELECT * FROM blobs b WHERE NOT EXISTS (SELECT 1 FROM jobs j WHERE j.blob=b.id AND j.status='running') ORDER BY used").fetchall():
            if total+extra <= self.budget:
                break
            self.path(row['id']).unlink(missing_ok=True)
            result_bytes = self.db.execute('SELECT coalesce(sum(length(result)),0) FROM jobs WHERE blob=?',(row['id'],)).fetchone()[0]
            self.db.execute('DELETE FROM blobs WHERE id=?',(row['id'],))
            self.db.execute('DELETE FROM jobs WHERE blob=?',(row['id'],))
            total -= row['size']+result_bytes
        if total+extra > self.budget:
            raise ValueError('Server cache limit reached')
        self.db.execute('PRAGMA incremental_vacuum(256)')

    def submit(self, session, blob, stages, context=None):
        context = context or {}
        if len(json.dumps(context))>12000:
            raise ValueError('Context too large')
        if not stages or len(stages)>4 or any(VERSIONS.get(k)!=v for k,v in stages.items()):
            raise ValueError('Model versions do not match')
        key = job_key(blob, stages,context)
        with self.lock, self.db:
            if not self.has(blob):
                raise FileNotFoundError('Input cache miss')
            row = self.db.execute('SELECT status FROM jobs WHERE id=?',(key,)).fetchone()
            if not row and self.db.execute('SELECT count(*) FROM jobs').fetchone()[0] >= 20000:
                self.db.execute("DELETE FROM jobs WHERE status='done' AND id IN (SELECT id FROM jobs WHERE status='done' ORDER BY updated LIMIT 5000)")
                if self.db.execute('SELECT count(*) FROM jobs').fetchone()[0] >= 20000:
                    raise ValueError('Server queue full')
            self.db.execute('''INSERT INTO jobs(id,blob,session,stages,status,result,updated,context) VALUES(?,?,?,?,'queued',NULL,?,?)
                ON CONFLICT(id) DO UPDATE SET session=excluded.session,updated=excluded.updated''',
                (key,blob,session,json.dumps(stages),time.time(),json.dumps(context)))
            self.db.execute('UPDATE blobs SET used=? WHERE id=?',(time.time(),blob))
        return key

    def take(self, session):
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM jobs WHERE session=? AND status='queued' ORDER BY updated LIMIT 1",(session,)).fetchone()
            if not row:
                return None
            self.db.execute("UPDATE jobs SET status='running' WHERE id=?",(row['id'],))
            return dict(id=row['id'], blob=str(self.path(row['blob'])), stages=json.loads(row['stages']),context=json.loads(row['context']))

    def finish(self, key, result):
        packed = zlib.compress(json.dumps(result,ensure_ascii=False).encode(),1)
        if len(packed)>4*1024**2:
            raise ValueError('Result too large')
        with self.lock, self.db:
            self.trim(len(packed))
            self.db.execute("UPDATE jobs SET status='done',result=?,updated=? WHERE id=?",(packed,time.time(),key))

    def reset(self):
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET status='queued' WHERE status='running'")

    def result(self, key):
        if not valid_key(key):
            raise ValueError('Invalid job key')
        with self.lock:
            row = self.db.execute('SELECT status,result FROM jobs WHERE id=?',(key,)).fetchone()
            if not row:
                raise FileNotFoundError('Job not found')
            return dict(status=row['status'],result=json.loads(zlib.decompress(row['result'])) if row['result'] else None)

    def stats(self, session):
        with self.lock:
            counts = dict(self.db.execute('SELECT status,count(*) FROM jobs WHERE session=? GROUP BY status',(session,)))
            size = self.db.execute('SELECT coalesce(sum(size),0) FROM blobs').fetchone()[0]
            return dict(queued=counts.get('queued',0),running=counts.get('running',0),completed=counts.get('done',0),cache_bytes=size,cache_limit=self.budget)
