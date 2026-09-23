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
    def __init__(self, directory, budget=20 * 1024**3):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.budget = budget
        self.active_session = ''
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
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(jobs)')}
        for name in ('outcome', 'ack_session', 'receipt'):
            if name not in columns:
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        for row in self.db.execute("SELECT id,stages,result FROM jobs WHERE status='done' AND outcome='' ").fetchall():
            result = json.loads(zlib.decompress(row['result']))
            self.db.execute('UPDATE jobs SET outcome=? WHERE id=?',
                (self.outcome(json.loads(row['stages']),result),row['id']))
        self.db.execute('''CREATE TABLE IF NOT EXISTS session_totals(session TEXT PRIMARY KEY,
            completed INTEGER DEFAULT 0,partial INTEGER DEFAULT 0,failed INTEGER DEFAULT 0,
            saved INTEGER DEFAULT 0,discarded INTEGER DEFAULT 0)''')
        self.db.execute('''INSERT OR IGNORE INTO session_totals
            SELECT session,sum(status='done' AND outcome='complete'),sum(status='done' AND outcome='partial'),
            sum(status='done' AND outcome='failed'),sum(ack_session=session AND receipt='saved'),
            sum(ack_session=session AND receipt='discarded') FROM jobs GROUP BY session''')
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
        # Retire a useful batch instead of oscillating at 100% for every photo.
        # A large active reserve remains protected even above the low watermark.
        target = int(self.budget*.85) if total+extra>self.budget*.95 else self.budget
        # Active queued inputs and results awaiting catalogue confirmation must
        # survive eviction. Expired sessions can reconstruct their queue.
        pinned = "j.status='running' OR (j.session=? AND (j.status='queued' OR (j.status='done' AND j.ack_session!=j.session)))"
        for row in self.db.execute(f"SELECT * FROM blobs b WHERE NOT EXISTS (SELECT 1 FROM jobs j WHERE j.blob=b.id AND ({pinned})) ORDER BY used",(self.active_session,)).fetchall():
            if total+extra <= target:
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
            row = self.db.execute('SELECT status,session,outcome FROM jobs WHERE id=?',(key,)).fetchone()
            if row and row['status']=='done' and row['session']!=session:
                self.increment(session,'completed' if row['outcome']=='complete' else row['outcome'])
            if not row and self.db.execute('SELECT count(*) FROM jobs').fetchone()[0] >= 20000:
                self.db.execute("DELETE FROM jobs WHERE status='done' AND id IN (SELECT id FROM jobs WHERE status='done' ORDER BY updated LIMIT 5000)")
                if self.db.execute('SELECT count(*) FROM jobs').fetchone()[0] >= 20000:
                    raise ValueError('Server queue full')
            self.db.execute('''INSERT INTO jobs(id,blob,session,stages,status,result,updated,context) VALUES(?,?,?,?,'queued',NULL,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    ack_session=CASE WHEN jobs.session=excluded.session THEN jobs.ack_session ELSE '' END,
                    receipt=CASE WHEN jobs.session=excluded.session THEN jobs.receipt ELSE '' END,
                    session=excluded.session,updated=excluded.updated''',
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
            row = self.db.execute('SELECT stages,session,status FROM jobs WHERE id=?',(key,)).fetchone()
            outcome = self.outcome(json.loads(row['stages']), result)
            if row['status']!='done':
                self.increment(row['session'],'completed' if outcome=='complete' else outcome)
            self.db.execute("UPDATE jobs SET status='done',result=?,updated=?,outcome=? WHERE id=?",(packed,time.time(),outcome,key))

    @staticmethod
    def outcome(stages, result):
        done = result.get('stages', {})
        return 'complete' if set(stages)<=set(done) and not result.get('errors') else 'partial' if done else 'failed'

    def increment(self, session, field):
        assert field in ('completed','partial','failed','saved','discarded')
        self.db.execute('INSERT OR IGNORE INTO session_totals(session) VALUES(?)',(session,))
        self.db.execute(f'UPDATE session_totals SET {field}={field}+1 WHERE session=?',(session,))

    def ready(self, keys, limit=16):
        if not isinstance(keys,list) or len(keys)>128 or any(not valid_key(key) for key in keys):
            raise ValueError('Invalid result keys')
        if not keys:
            return []
        with self.lock:
            rows = self.db.execute("SELECT id,result FROM jobs WHERE status='done' AND id IN ("+
                ','.join('?' for _ in keys)+') ORDER BY updated LIMIT ?', [*keys, limit]).fetchall()
            return [dict(key=row['id'],result=json.loads(zlib.decompress(row['result']))) for row in rows]

    def acknowledge(self, session, receipts):
        if not isinstance(receipts,dict) or len(receipts)>128 or any(not valid_key(k) or v not in ('saved','partial','discarded') for k,v in receipts.items()):
            raise ValueError('Invalid receipts')
        with self.lock, self.db:
            for key,value in receipts.items():
                row = self.db.execute("SELECT ack_session FROM jobs WHERE id=? AND session=? AND status='done'",(key,session)).fetchone()
                if row and row['ack_session']!=session:
                    if value in ('saved','discarded'):
                        self.increment(session,value)
                    self.db.execute('UPDATE jobs SET ack_session=?,receipt=? WHERE id=?',(session,value,key))

    def missing(self, keys):
        with self.lock:
            return [key for key in keys if not self.db.execute('SELECT 1 FROM jobs WHERE id=?',(key,)).fetchone()]

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
            results = self.db.execute('SELECT coalesce(sum(length(result)),0) FROM jobs').fetchone()[0]
            totals = self.db.execute('SELECT * FROM session_totals WHERE session=?',(session,)).fetchone()
            totals = dict(totals) if totals else dict(completed=0,partial=0,failed=0,saved=0,discarded=0)
            waiting = self.db.execute("SELECT count(*) FROM jobs WHERE session=? AND status='done' AND ack_session!=session",(session,)).fetchone()[0]
            pinned = self.db.execute("""SELECT coalesce(sum(size),0) FROM blobs b WHERE EXISTS(
                SELECT 1 FROM jobs j WHERE j.blob=b.id AND (j.status='running' OR
                (j.session=? AND (j.status='queued' OR (j.status='done' AND j.ack_session!=j.session)))))""",(session,)).fetchone()[0]
            pinned += self.db.execute("""SELECT coalesce(sum(length(result)),0) FROM jobs j
                WHERE j.status='running' OR (j.session=? AND j.status='done' AND j.ack_session!=j.session)""",(session,)).fetchone()[0]
            return dict(queued=counts.get('queued',0),running=counts.get('running',0),completed=totals['completed'],
                partial=totals['partial'],failed=totals['failed'],saved=totals['saved'],awaiting_save=waiting,discarded=totals['discarded'],
                cache_bytes=size+results,cache_limit=self.budget,cache_pending_bytes=pinned,
                cache_reusable_bytes=max(0,size+results-pinned))
