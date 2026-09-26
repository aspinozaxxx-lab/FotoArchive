from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

from .config import Settings, EMBED_VERSION, CAPTION_VERSION, FACE_VERSION, GEO_VERSION, SUPPORTED, VIDEO_FORMATS, MEDIA_VERSION
from .media_types import format_key


@dataclass
class Filters:
    media_kind: str = ""
    folder: str = ""
    include_subfolders: bool = True
    place: str = ""
    geo_bounds: str = ""
    has_gps: bool = False
    date_from: str = ""
    date_to: str = ""
    unknown_date: bool = False
    extension: str = ""
    camera: str = ""
    orientation: str = ""
    min_width: int = 0
    min_height: int = 0
    min_megapixels: float = 0
    min_bytes: int = 0
    max_bytes: int = 0

    def expression(self, literal=False):
        clauses = ["present = 1", "metadata_ready = 1"]
        params = []

        def add(column, operator, value):
            if literal:
                rendered = str(value) if isinstance(value, (int, float)) else "'" + str(value).replace("'", "''") + "'"
                clauses.append(f"{column} {operator} {rendered}")
            else:
                clauses.append(f"{column} {operator} ?")
                params.append(value)

        if self.folder:
            folder = self.folder.replace("\\", "/").strip("/")
            if not self.include_subfolders:
                add("folder", "=", folder)
            elif literal:
                escaped = folder.replace("'", "''")
                clauses.append(f"(folder = '{escaped}' OR starts_with(folder, '{escaped}/'))")
            else:
                clauses.append("(folder = ? OR substr(folder, 1, ?) = ?)")
                params += [folder, len(folder) + 1, folder + "/"]
        if self.unknown_date:
            clauses.append("captured_at IS NULL")
        else:
            if self.date_from:
                add("captured_at", ">=", date.fromisoformat(self.date_from).isoformat())
            if self.date_to:
                add("captured_at", "<", (date.fromisoformat(self.date_to) + timedelta(days=1)).isoformat())
        if self.media_kind in ('photo', 'video'):
            extensions = ','.join("'" + extension + "'" for extension in sorted(VIDEO_FORMATS))
            clauses.append(f"extension {'IN' if self.media_kind == 'video' else 'NOT IN'} ({extensions})")
        for field in ("extension", "camera", "orientation"):
            if getattr(self, field):
                add(field, "=", getattr(self, field))
        for field, column in (("min_width", "width"), ("min_height", "height"), ("min_bytes", "size")):
            if getattr(self, field):
                add(column, ">=", getattr(self, field))
        if self.min_megapixels:
            add("pixels", ">=", round(self.min_megapixels * 1_000_000))
        if self.max_bytes:
            add("size", "<=", self.max_bytes)
        if self.place:
            text = self.place.casefold()
            if literal:
                raise ValueError('Geographic filters must be resolved against the catalogue before vector search')
            clauses.append("(instr(lower(geo_text),lower(?))>0 OR instr(lower(user_place),lower(?))>0)")
            params += [text, text]
        if self.has_gps or self.geo_bounds:
            clauses.append('coalesce(user_latitude,latitude) IS NOT NULL AND coalesce(user_longitude,longitude) IS NOT NULL')
        if self.geo_bounds:
            west, south, east, north = map(float, self.geo_bounds.split(','))
            if not (-180 <= west <= 180 and -180 <= east <= 180 and -90 <= south <= north <= 90):
                raise ValueError('Некорректная область карты')
            add('coalesce(user_latitude,latitude)', '>=', south)
            add('coalesce(user_latitude,latitude)', '<=', north)
            if west <= east:
                add('coalesce(user_longitude,longitude)', '>=', west)
                add('coalesce(user_longitude,longitude)', '<=', east)
            else:
                clauses.append('(coalesce(user_longitude,longitude)>=? OR coalesce(user_longitude,longitude)<=?)')
                params += [west, east]
        return " AND ".join(clauses), params


class Catalog:
    @classmethod
    def open_reader(cls, cfg: Settings):
        """A separate connection for browsing; never migrate or write the catalogue."""
        reader = cls.__new__(cls)
        reader.cfg = cfg
        uri = (cfg.data_dir / 'catalog.sqlite3').resolve().as_uri() + '?mode=ro'
        reader.db = sqlite3.connect(uri, uri=True, timeout=1)
        reader.db.row_factory = sqlite3.Row
        reader.db.create_function('lower',1,lambda s:s.casefold() if s else '',deterministic=True)
        reader.db.execute('PRAGMA cache_size=-16384')
        from .media_units import MediaUnits
        reader.media_units = MediaUnits(reader, initialize=False)
        return reader

    def __init__(self, cfg: Settings):
        self.cfg = cfg
        cfg.initialize()
        self.db = sqlite3.connect(cfg.data_dir / "catalog.sqlite3", timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.create_function('lower',1,lambda s:s.casefold() if s else '',deterministic=True)
        names_exist = self.db.execute("SELECT 1 FROM sqlite_master WHERE name='asset_names'").fetchone() is not None
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS sources(id INTEGER PRIMARY KEY, root TEXT UNIQUE NOT NULL);
        CREATE TABLE IF NOT EXISTS assets(
          id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id),
          relative_path TEXT NOT NULL, path_key TEXT NOT NULL UNIQUE, path TEXT NOT NULL,
          folder TEXT NOT NULL, filename TEXT NOT NULL, extension TEXT NOT NULL,
          size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, version INTEGER NOT NULL DEFAULT 1,
          present INTEGER NOT NULL DEFAULT 1, metadata_ready INTEGER NOT NULL DEFAULT 0,
          width INTEGER NOT NULL DEFAULT 0, height INTEGER NOT NULL DEFAULT 0, pixels INTEGER NOT NULL DEFAULT 0,
          captured_at TEXT, date_raw TEXT, date_offset TEXT, camera TEXT NOT NULL DEFAULT '',
          orientation TEXT NOT NULL DEFAULT '', metadata_json TEXT, content_hash TEXT,
          thumbnail TEXT, description TEXT NOT NULL DEFAULT '', observations_json TEXT,
          caption_version TEXT, embed_version TEXT, updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS assets_folder ON assets(folder);
        CREATE INDEX IF NOT EXISTS assets_date ON assets(captured_at);
        CREATE INDEX IF NOT EXISTS assets_extension ON assets(extension);
        CREATE INDEX IF NOT EXISTS assets_browse ON assets(present,metadata_ready,captured_at DESC,id DESC);
        CREATE INDEX IF NOT EXISTS assets_summary ON assets(present,metadata_ready,embed_version,caption_version,captured_at);
        CREATE INDEX IF NOT EXISTS assets_camera ON assets(camera);
        CREATE TABLE IF NOT EXISTS units(
          id TEXT PRIMARY KEY, asset_id INTEGER NOT NULL REFERENCES assets(id),
          kind TEXT NOT NULL, timestamp_ms INTEGER, UNIQUE(asset_id,kind,timestamp_ms)
        );
        CREATE TABLE IF NOT EXISTS embeddings(
          unit_id TEXT PRIMARY KEY REFERENCES units(id), file_version INTEGER NOT NULL,
          model_version TEXT NOT NULL, dimensions INTEGER NOT NULL, vector BLOB NOT NULL
        );
        CREATE TABLE IF NOT EXISTS jobs(
          asset_id INTEGER NOT NULL REFERENCES assets(id), stage TEXT NOT NULL,
          file_version INTEGER NOT NULL, model_version TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
          error TEXT, elapsed REAL, PRIMARY KEY(asset_id,stage)
        );
        CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(status,stage,asset_id);
        CREATE INDEX IF NOT EXISTS jobs_asset_status ON jobs(asset_id,status);
        CREATE TABLE IF NOT EXISTS outbox(unit_id TEXT PRIMARY KEY, revision INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS verification_cache(
          cache_key TEXT PRIMARY KEY, asset_id INTEGER NOT NULL, file_version INTEGER NOT NULL,
          result_json TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS app_state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE VIRTUAL TABLE IF NOT EXISTS asset_names USING fts5(filename,relative_path,content='assets',content_rowid='id',tokenize='trigram');
        CREATE TRIGGER IF NOT EXISTS assets_names_insert AFTER INSERT ON assets BEGIN
          INSERT INTO asset_names(rowid,filename,relative_path) VALUES(new.id,new.filename,new.relative_path);
        END;
        CREATE TRIGGER IF NOT EXISTS assets_names_delete AFTER DELETE ON assets BEGIN
          INSERT INTO asset_names(asset_names,rowid,filename,relative_path) VALUES('delete',old.id,old.filename,old.relative_path);
        END;
        CREATE TRIGGER IF NOT EXISTS assets_names_update AFTER UPDATE OF filename,relative_path ON assets BEGIN
          INSERT INTO asset_names(asset_names,rowid,filename,relative_path) VALUES('delete',old.id,old.filename,old.relative_path);
          INSERT INTO asset_names(rowid,filename,relative_path) VALUES(new.id,new.filename,new.relative_path);
        END;
        PRAGMA user_version=1;
        """)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(assets)")}
        for name, declaration in {"face_version": "TEXT", "geo_version": "TEXT", "latitude": "REAL", "longitude": "REAL", "altitude": "REAL",
            "media_kind": "TEXT NOT NULL DEFAULT 'photo'", "duration_ms": "INTEGER NOT NULL DEFAULT 0",
            "geo_text": "TEXT NOT NULL DEFAULT ''", "geo_source": "TEXT NOT NULL DEFAULT ''", "geo_json": "TEXT NOT NULL DEFAULT '{}'"}.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE assets ADD COLUMN {name} {declaration}")
        if "location_json" not in {r[1] for r in self.db.execute("PRAGMA table_info(units)")}:
            self.db.execute("ALTER TABLE units ADD COLUMN location_json TEXT")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS faces(id TEXT PRIMARY KEY,asset_id INTEGER NOT NULL REFERENCES assets(id),file_version INTEGER NOT NULL,
              model_version TEXT NOT NULL,box_json TEXT NOT NULL,landmarks_json TEXT NOT NULL,confidence REAL NOT NULL,vector BLOB NOT NULL,thumbnail TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS faces_asset ON faces(asset_id,file_version,model_version);
            CREATE TABLE IF NOT EXISTS geo_embeddings(asset_id INTEGER PRIMARY KEY REFERENCES assets(id),file_version INTEGER NOT NULL,
              model_version TEXT NOT NULL,geo_version TEXT NOT NULL,vector BLOB NOT NULL);
            PRAGMA user_version=2;
        """)
        with self.db:
            if not names_exist:
                self.db.execute("INSERT INTO asset_names(asset_names) VALUES('rebuild')")
            self.db.execute("INSERT OR IGNORE INTO sources(root) VALUES(?)", (str(cfg.root.resolve()),))
        self.source_id = self.db.execute("SELECT id FROM sources WHERE root=?", (str(cfg.root.resolve()),)).fetchone()[0]
        from .media_units import MediaUnits
        self.media_units = MediaUnits(self)
        from .library import initialize_library
        initialize_library(self.db)
        self.db.executescript('''
            INSERT OR IGNORE INTO app_state(key,value) VALUES('browse_revision','0');
            CREATE TRIGGER IF NOT EXISTS browse_insert AFTER INSERT ON assets BEGIN
              UPDATE app_state SET value=CAST(value AS INTEGER)+1 WHERE key='browse_revision';
            END;
            CREATE TRIGGER IF NOT EXISTS browse_delete AFTER DELETE ON assets BEGIN
              UPDATE app_state SET value=CAST(value AS INTEGER)+1 WHERE key='browse_revision';
            END;
            CREATE TRIGGER IF NOT EXISTS browse_update AFTER UPDATE OF
              present,metadata_ready,version,captured_at,folder,extension,camera,orientation,
              width,height,pixels,size,media_kind ON assets BEGIN
              UPDATE app_state SET value=CAST(value AS INTEGER)+1 WHERE key='browse_revision';
            END;
        ''')
        if not names_exist and not self.state('catalog_identity'):
            from uuid import uuid4
            self.set_state('catalog_identity', uuid4().hex)

    def close(self):
        self.db.close()

    def state(self, key, default=None):
        row = self.db.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO app_state VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def recover(self):
        with self.db:
            self.db.execute("UPDATE jobs SET status='pending' WHERE status='running'")
            self.db.execute("UPDATE unit_jobs SET status='pending' WHERE status='running'")
            # Prior versions could race with eviction of a shared preview. Only
            # those reproducible cache misses are retried, not damaged originals.
            for table in ('jobs','unit_jobs'):
                self.db.execute(f"UPDATE {table} SET status='pending',error=NULL WHERE status='error' AND instr(error,?)>0 AND instr(error,'WinError 2')>0",
                                (str(self.cfg.data_dir/'previews'),))
            from .media_units import UNIT_STAGES
            for stage, version in UNIT_STAGES.items():
                self.db.execute("UPDATE unit_jobs SET status='pending',model_version=? WHERE stage=? AND model_version<>?", (version, stage, version))
            self.db.execute("UPDATE jobs SET status='pending',model_version=? WHERE stage='embedding' AND model_version<>?", (EMBED_VERSION, EMBED_VERSION))
            self.db.execute("UPDATE jobs SET status='pending',model_version=? WHERE stage='caption' AND model_version<>?", (CAPTION_VERSION, CAPTION_VERSION))
            for stage, column, version in (("faces", "face_version", FACE_VERSION), ("location", "geo_version", GEO_VERSION)):
                self.db.execute(f"""INSERT OR IGNORE INTO jobs(asset_id,stage,file_version,model_version)
                    SELECT id,?,version,? FROM assets WHERE present=1 AND ({column} IS NULL OR {column}<>?)""", (stage, version, version))
                self.db.execute("UPDATE jobs SET status='pending',model_version=? WHERE stage=? AND model_version<>?", (version, stage, version))

    def register(self, path: Path):
        path = path.resolve()
        relative = path.relative_to(self.cfg.root.resolve()).as_posix()
        info = path.stat()
        key = os.path.normcase(str(path))
        old = self.db.execute("SELECT * FROM assets WHERE path_key=?", (key,)).fetchone()
        if old and old["size"] == info.st_size and old["mtime_ns"] == info.st_mtime_ns:
            if not old["present"]:
                with self.db:
                    self.db.execute("UPDATE assets SET present=1 WHERE id=?", (old["id"],))
                    self.db.execute("UPDATE jobs SET status='pending',error=NULL WHERE asset_id=? AND status='error'", (old['id'],))
                    self.db.execute("""UPDATE unit_jobs SET status='pending',error=NULL WHERE status='error'
                        AND unit_id IN (SELECT id FROM units WHERE asset_id=?)""", (old['id'],))
                    self._enqueue(old["id"], old["version"])
            return old["id"], False
        version = old["version"] + 1 if old else 1
        with self.db:
            if old:
                asset_id = old["id"]
                self.db.execute("""UPDATE assets SET size=?,mtime_ns=?,version=?,present=1,metadata_ready=0,
                  content_hash=NULL,description='',observations_json=NULL,embed_version=NULL,caption_version=NULL,face_version=NULL,geo_version=NULL,
                  latitude=NULL,longitude=NULL,altitude=NULL,geo_text='',geo_source='',geo_json='{}',
                  updated_at=? WHERE id=?""", (info.st_size, info.st_mtime_ns, version, time.time(), asset_id))
                self._enqueue(asset_id, version)
                self.db.execute("DELETE FROM embeddings WHERE unit_id IN (SELECT id FROM units WHERE asset_id=?)", (asset_id,))
                self.db.execute("DELETE FROM faces WHERE asset_id=?", (asset_id,))
                self.db.execute("DELETE FROM geo_embeddings WHERE asset_id=?", (asset_id,))
                if path.suffix.lower() in VIDEO_FORMATS:
                    self.db.execute('DELETE FROM units WHERE asset_id=?', (asset_id,))
            else:
                result = self.db.execute("""INSERT INTO assets(source_id,relative_path,path_key,path,folder,filename,extension,size,mtime_ns,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""", (self.source_id, relative, key, str(path), Path(relative).parent.as_posix(), path.name, path.suffix.lower(), info.st_size, info.st_mtime_ns, time.time()))
                asset_id = result.lastrowid
                if path.suffix.lower() not in VIDEO_FORMATS:
                    self.db.execute("INSERT INTO units(id,asset_id,kind) VALUES(?,?,'photo')", (f"{asset_id}:photo", asset_id))
            self.db.execute('UPDATE assets SET media_kind=? WHERE id=?', ('video' if path.suffix.lower() in VIDEO_FORMATS else 'photo', asset_id))
            for stage, model in (("metadata", MEDIA_VERSION), ("embedding", EMBED_VERSION), ("caption", CAPTION_VERSION), ("faces", FACE_VERSION), ("location", GEO_VERSION)):
                self.db.execute("INSERT OR REPLACE INTO jobs(asset_id,stage,file_version,model_version) VALUES(?,?,?,?)", (asset_id, stage, version, model))
            if old:
                self._enqueue(asset_id, version)
        return asset_id, True

    def _enqueue(self, asset_id, version, unit_id=None):
        units = [(unit_id,)] if unit_id else self.db.execute('SELECT id FROM units WHERE asset_id=?', (asset_id,)).fetchall()
        self.db.executemany("INSERT INTO outbox VALUES(?,?) ON CONFLICT(unit_id) DO UPDATE SET revision=outbox.revision+1", [(row[0], version) for row in units])

    def get(self, asset_id):
        row = self.db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        return dict(row) if row else None

    def job_asset(self, job):
        return self.media_units.asset_at(self.get(job['asset_id']), job.get('unit_id'))

    def current_job(self, job):
        return self.db.execute('SELECT 1 FROM assets WHERE id=? AND version=? AND present=1',
                               (job['asset_id'], job['file_version'])).fetchone() is not None

    def next_job(self, stages=None, asset_id=None):
        row = None
        asset_clause = ' AND a.id=?' if asset_id is not None else ''
        for stage in (stages or ("metadata", "embedding", "location", "faces", "caption")):
            row = self.db.execute("""SELECT j.asset_id,j.stage,j.file_version,j.model_version FROM jobs j
              JOIN assets a ON a.id=j.asset_id WHERE j.status='pending' AND j.stage=? AND a.present=1
              AND j.file_version=a.version AND (j.stage='metadata' OR a.metadata_ready=1)"""+asset_clause+
              " ORDER BY j.asset_id LIMIT 1", [stage]+([asset_id] if asset_id is not None else [])).fetchone()
            if row:
                break
        if not row:
            return None
        job = dict(row)
        with self.db:
            if job['stage'] in ('embedding', 'caption', 'faces') and self.get(job['asset_id'])['media_kind'] == 'video':
                job = self.media_units.claim(job)
                if job is None:
                    raise RuntimeError('Очередь кадров видео не согласована с очередью файла')
            self.db.execute("UPDATE jobs SET status='running',attempts=attempts+1 WHERE asset_id=? AND stage=?", (row["asset_id"], row["stage"]))
        return job

    def complete_metadata(self, job, metadata, thumbnail, content_hash):
        if not self.current_job(job):
            return
        asset_id = job["asset_id"]
        allowed = ("width", "height", "captured_at", "date_raw", "date_offset", "camera", "orientation", "metadata_json")
        with self.db:
            self.db.execute("UPDATE assets SET " + ",".join(f"{k}=?" for k in allowed) + ",pixels=?,thumbnail=?,content_hash=?,metadata_ready=1 WHERE id=? AND version=?",
                [metadata[k] for k in allowed] + [metadata["width"] * metadata["height"], str(thumbnail), content_hash, asset_id, job["file_version"]])
            self.db.execute('UPDATE assets SET duration_ms=? WHERE id=? AND version=?', (metadata.get('duration_ms', 0), asset_id, job['file_version']))
            self.media_units.create(job, metadata)
            indexed = self.db.execute('SELECT embed_version,face_version,geo_version FROM assets WHERE id=?',(asset_id,)).fetchone()
            if indexed and any(indexed):
                self._enqueue(asset_id, job["file_version"])

    def complete_embedding(self, job, vector):
        import numpy as np
        if not self.current_job(job):
            return
        vector = np.asarray(vector, dtype=np.float32)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO embeddings VALUES(?,?,?,?,?)", (job.get('unit_id', f"{job['asset_id']}:photo"), job["file_version"], EMBED_VERSION, len(vector), vector.tobytes()))
            if not job.get('unit_id'):
                self.db.execute("UPDATE assets SET embed_version=? WHERE id=? AND version=?", (EMBED_VERSION, job["asset_id"], job["file_version"]))
            self._enqueue(job["asset_id"], job["file_version"], job.get('unit_id'))

    def complete_caption(self, job, result):
        if not self.current_job(job):
            return
        if not isinstance(result.get("description"), str) or not result["description"].strip():
            raise ValueError("Пустое описание модели")
        with self.db:
            if job.get('unit_id'):
                self.db.execute('UPDATE unit_details SET description=?,observations_json=? WHERE unit_id=? AND file_version=?',
                                (result['description'], json.dumps(result, ensure_ascii=False), job['unit_id'], job['file_version']))
            else:
                self.db.execute("UPDATE assets SET description=?,observations_json=?,caption_version=? WHERE id=? AND version=?", (result["description"], json.dumps(result, ensure_ascii=False), CAPTION_VERSION, job["asset_id"], job["file_version"]))
            self._enqueue(job["asset_id"], job["file_version"], job.get('unit_id'))

    def complete_location(self, job, location, vector=None):
        import numpy as np
        if not self.current_job(job):
            return
        with self.db:
            fields = ("latitude", "longitude", "altitude", "geo_text", "geo_source", "geo_json")
            self.db.execute("UPDATE assets SET " + ",".join(k + "=?" for k in fields) + ",geo_version=? WHERE id=? AND version=?",
                            [location[k] for k in fields] + [GEO_VERSION, job["asset_id"], job["file_version"]])
            self.db.execute("DELETE FROM geo_embeddings WHERE asset_id=?", (job["asset_id"],))
            if vector is not None:
                self.db.execute("INSERT INTO geo_embeddings VALUES(?,?,?,?,?)", (job["asset_id"], job["file_version"], EMBED_VERSION, GEO_VERSION, np.asarray(vector, dtype=np.float32).tobytes()))
            self.db.execute("UPDATE units SET location_json=? WHERE asset_id=?", (json.dumps(location, ensure_ascii=False), job["asset_id"]))
            self._enqueue(job["asset_id"], job["file_version"])

    def complete_faces(self, job, records):
        import numpy as np
        if not self.current_job(job):
            return
        import hashlib
        version_key = hashlib.sha256(FACE_VERSION.encode()).hexdigest()[:10]
        prepared = []
        for number, face in enumerate(records):
            face_id = f"{job['asset_id']}:{job['file_version']}:{version_key}:{number}"
            if job.get('unit_id'):
                face_id += f":frame{job['timestamp_ms']}"
            target = self.cfg.data_dir / "faces" / str(job["asset_id"] // 1000) / (face_id.replace(":", "_") + ".webp")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".tmp")
            face["portrait"].save(temporary, format="WEBP", quality=90)
            temporary.replace(target)
            prepared.append((face_id, job["asset_id"], job["file_version"], FACE_VERSION,
                             json.dumps(face["box"]), json.dumps(face["landmarks"]), face["confidence"], face["vector"].astype(np.float32).tobytes(), str(target)))
        with self.db:
            if job.get('unit_id'):
                self.db.execute('DELETE FROM faces WHERE id IN (SELECT face_id FROM face_units WHERE unit_id=?)', (job['unit_id'],))
            else:
                self.db.execute("DELETE FROM faces WHERE asset_id=?", (job["asset_id"],))
            self.db.executemany("INSERT INTO faces VALUES(?,?,?,?,?,?,?,?,?)", prepared)
            if job.get('unit_id'):
                self.db.executemany('INSERT INTO face_units VALUES(?,?)', [(row[0], job['unit_id']) for row in prepared])
            else:
                self.db.execute("UPDATE assets SET face_version=? WHERE id=? AND version=?", (FACE_VERSION, job["asset_id"], job["file_version"]))
            self._enqueue(job["asset_id"], job["file_version"], job.get('unit_id'))

    def faces_for(self, asset_id, unit_id=None):
        return [dict(r) | {"box": json.loads(r["box_json"])} for r in self.db.execute("""SELECT f.id,f.asset_id,f.file_version,f.box_json,f.confidence,f.thumbnail
            FROM faces f JOIN assets a ON a.id=f.asset_id WHERE f.asset_id=? AND f.file_version=a.version AND f.model_version=?
            AND a.present=1 AND (? IS NULL OR f.id IN (SELECT face_id FROM face_units WHERE unit_id=?)) ORDER BY f.id""", (asset_id, FACE_VERSION, unit_id, unit_id))]

    def face_vector(self, face_id):
        import numpy as np
        row = self.db.execute("""SELECT f.vector FROM faces f JOIN assets a ON a.id=f.asset_id
            WHERE f.id=? AND f.file_version=a.version AND f.model_version=? AND a.present=1""", (face_id, FACE_VERSION)).fetchone()
        return np.frombuffer(row[0], dtype=np.float32).copy() if row else None

    def face_info(self, face_id):
        row = self.db.execute("""SELECT f.id,f.asset_id,f.file_version,f.thumbnail,f.box_json,a.filename,a.relative_path
            FROM faces f JOIN assets a ON a.id=f.asset_id
            WHERE f.id=? AND f.file_version=a.version AND f.model_version=? AND a.present=1""", (face_id, FACE_VERSION)).fetchone()
        if not row:
            return None
        result = dict(row) | {"box": json.loads(row["box_json"])}
        unit = self.db.execute('SELECT u.id unit_id,u.timestamp_ms FROM face_units f JOIN units u ON u.id=f.unit_id WHERE f.face_id=?', (face_id,)).fetchone()
        return result | (dict(unit) if unit else {})

    def finish_job(self, job, elapsed, error=None, source='local'):
        if not self.current_job(job):
            return
        observer = getattr(self, 'on_job_finished', None)
        previous = None
        if observer:
            if job.get('unit_id'):
                previous = self.db.execute('SELECT status FROM unit_jobs WHERE unit_id=? AND stage=? AND file_version=?',
                    (job['unit_id'],job['stage'],job['file_version'])).fetchone()
            else:
                previous = self.db.execute('SELECT status FROM jobs WHERE asset_id=? AND stage=? AND file_version=?',
                    (job['asset_id'],job['stage'],job['file_version'])).fetchone()
        with self.db:
            if job.get('unit_id'):
                self.media_units.finish(job, elapsed, error)
            else:
                self.db.execute("UPDATE jobs SET status=?,error=?,elapsed=? WHERE asset_id=? AND stage=? AND file_version=?", ("error" if error else "done", error, elapsed, job["asset_id"], job["stage"], job["file_version"]))
        if observer and previous and previous[0] != ('error' if error else 'done'):
            observer(job, error, source)

    def requeue_jobs(self, jobs):
        with self.db:
            self.db.executemany("UPDATE unit_jobs SET status='pending' WHERE unit_id=? AND stage=? AND file_version=? AND status='running'",
                                [(job['unit_id'], job['stage'], job['file_version']) for job in jobs if job.get('unit_id')])
            self.db.executemany("UPDATE jobs SET status='pending' WHERE asset_id=? AND stage=? AND file_version=? AND status='running'",
                                [(job['asset_id'],job['stage'],job['file_version']) for job in jobs])

    def retry_errors(self):
        with self.db:
            self.db.execute("UPDATE jobs SET status='pending',error=NULL WHERE status='error'")
            self.db.execute("UPDATE unit_jobs SET status='pending',error=NULL WHERE status='error'")

    def vector(self, asset_id, unit_id=None):
        import numpy as np
        row = self.db.execute("SELECT e.vector FROM embeddings e JOIN units u ON u.id=e.unit_id JOIN assets a ON a.id=u.asset_id WHERE u.asset_id=? AND a.present=1 AND e.file_version=a.version AND e.model_version=? AND (? IS NULL OR u.id=?) ORDER BY u.timestamp_ms LIMIT 1", (asset_id, EMBED_VERSION, unit_id, unit_id)).fetchone()
        return np.frombuffer(row[0], dtype=np.float32).copy() if row else None

    def browse(self, filters: Filters, offset=0, limit=200, literal_query=""):
        where, params = filters.expression()
        if literal_query:
            where += " AND (instr(lower(filename),lower(?))>0 OR instr(lower(relative_path),lower(?))>0)"
            params += [literal_query, literal_query]
        total = self.db.execute(f"SELECT count(*) FROM assets WHERE {where}", params).fetchone()[0]
        rows = self.db.execute(f"SELECT * FROM assets WHERE {where} ORDER BY captured_at DESC,id DESC LIMIT ? OFFSET ?", params + [limit, offset])
        return [dict(r) for r in rows], total

    def stats(self):
        result = dict(self.db.execute("""SELECT count(*) total,sum(metadata_ready) metadata,
          sum(embed_version=?) embeddings,sum(caption_version=?) captions,
          sum(metadata_ready=1 AND captured_at IS NULL) unknown_dates FROM assets WHERE present=1""", (EMBED_VERSION, CAPTION_VERSION)).fetchone())
        result = {k: v or 0 for k, v in result.items()}
        result["errors"] = self.db.execute("SELECT count(*) FROM jobs j JOIN assets a ON a.id=j.asset_id WHERE j.status='error' AND a.present=1").fetchone()[0]
        result["pending"] = self.db.execute("SELECT count(*) FROM jobs j JOIN assets a ON a.id=j.asset_id WHERE j.status IN ('pending','running') AND a.present=1 AND j.file_version=a.version").fetchone()[0]
        result["outbox"] = self.db.execute("SELECT count(*) FROM outbox").fetchone()[0]
        result["faces"], result["locations"] = self.db.execute("SELECT coalesce(sum(face_version=?),0),coalesce(sum(geo_version=?),0) FROM assets WHERE present=1", (FACE_VERSION, GEO_VERSION)).fetchone()
        result['video_frames'] = {row['stage']: row['n'] for row in self.db.execute("SELECT j.stage,count(*) n FROM unit_jobs j JOIN units u ON u.id=j.unit_id JOIN assets a ON a.id=u.asset_id WHERE j.status='done' AND a.present=1 AND j.file_version=a.version GROUP BY j.stage")}
        result['video_frames']['total'] = self.db.execute("SELECT count(*) FROM units u JOIN assets a ON a.id=u.asset_id WHERE u.kind='frame' AND a.present=1").fetchone()[0]
        return result

    def name_matches(self, query: str, filters: Filters, limit=200, offset=0):
        if len(query.strip()) < 3:
            return []
        where, params = filters.expression()
        literal = '"' + query.strip().replace('"', '""') + '"'
        return [dict(row) for row in self.db.execute(
            f"SELECT a.* FROM asset_names JOIN assets a ON a.id=asset_names.rowid WHERE asset_names MATCH ? AND {where} ORDER BY a.id LIMIT ? OFFSET ?",
            [literal] + params + [limit, offset])]

    def facets(self):
        result = {}
        for field in ("folder", "camera", "extension"):
            result[field] = [r[0] for r in self.db.execute(f"SELECT DISTINCT {field} FROM assets WHERE present=1 AND {field}<>'' ORDER BY {field}")]
        result["date_bounds"] = list(self.db.execute("SELECT min(substr(captured_at,1,10)),max(substr(captured_at,1,10)) FROM assets WHERE present=1 AND metadata_ready=1").fetchone())
        return result

    def errors(self):
        records = [dict(r) for r in self.db.execute("SELECT a.path,j.stage,j.error FROM jobs j JOIN assets a ON a.id=j.asset_id WHERE j.status='error' AND a.present=1 ORDER BY a.id LIMIT 500")]
        from .video import timestamp_text
        for row in self.db.execute('''SELECT a.path,j.stage,j.error,u.timestamp_ms FROM unit_jobs j
            JOIN units u ON u.id=j.unit_id JOIN assets a ON a.id=u.asset_id WHERE j.status='error' AND a.present=1 LIMIT 500'''):
            records.append({'path': row['path'] + ' · кадр ' + timestamp_text(row['timestamp_ms']), 'stage': row['stage'], 'error': row['error']})
        return records


def enumerate_source(cfg: Settings, skip_includes=()):
    """Yield bounded directory entries only in explicitly enabled subtrees."""
    root = cfg.root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Источник недоступен: {root}. Каталог сохранён.")
    visited = set()
    skip = {(root / include).resolve() for include in skip_includes}
    for include in cfg.includes:
        selected = (root / include).resolve()
        if not selected.is_relative_to(root):
            raise ValueError("Папка находится вне источника")
        if not selected.is_dir():
            raise FileNotFoundError(f"Папка недоступна: {selected}")
        stack = [selected]
        while stack:
            folder = stack.pop()
            if folder in visited or folder in skip:
                continue
            visited.add(folder)
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.is_symlink() or getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        path = Path(entry.path)
                        yield path, format_key(path) in SUPPORTED
