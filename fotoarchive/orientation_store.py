"""Durable orientation review and explicitly approved file-edit journal."""
import json
import time
from uuid import uuid4

from .catalog import Filters
from .orientation import ORIENTATION_VERSION


class OrientationStore:
    def __init__(self, catalog):
        self.catalog, self.db = catalog, catalog.db
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS orientation_checks(
              asset_id INTEGER PRIMARY KEY REFERENCES assets(id),file_version INTEGER NOT NULL,model_version TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending',rotation INTEGER,certain INTEGER NOT NULL DEFAULT 0,
              reason TEXT NOT NULL DEFAULT '',result_json TEXT,error TEXT,selected INTEGER NOT NULL DEFAULT 0,
              excluded INTEGER NOT NULL DEFAULT 0,elapsed REAL,updated_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS orientation_pending ON orientation_checks(status,excluded,asset_id);
            CREATE TABLE IF NOT EXISTS rotation_edits(
              id INTEGER PRIMARY KEY,batch_id TEXT NOT NULL,asset_id INTEGER NOT NULL REFERENCES assets(id),
              file_version INTEGER NOT NULL,rotation INTEGER NOT NULL,path TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'queued',backup_path TEXT,stage_path TEXT,before_hash TEXT,after_hash TEXT,
              applied_version INTEGER,error TEXT,created_at REAL NOT NULL,UNIQUE(batch_id,asset_id));
            CREATE INDEX IF NOT EXISTS rotation_work ON rotation_edits(status,id);
        """)
        with self.db:
            self.db.execute("UPDATE orientation_checks SET status='pending' WHERE status='running'")

    def enqueue(self, filters=None):
        where, params = (filters or Filters()).expression()
        now = time.time()
        with self.db:
            self.db.execute(f"""INSERT INTO orientation_checks(asset_id,file_version,model_version,updated_at)
                SELECT id,version,?,? FROM assets WHERE {where} AND extension IN ('.jpg','.jpeg','.bmp')
                ON CONFLICT(asset_id) DO UPDATE SET file_version=excluded.file_version,model_version=excluded.model_version,
                  status='pending',rotation=NULL,certain=0,reason='',result_json=NULL,error=NULL,selected=0,updated_at=excluded.updated_at
                WHERE orientation_checks.file_version<>excluded.file_version OR orientation_checks.model_version<>excluded.model_version""",
                [ORIENTATION_VERSION, now]+params)
        self.catalog.set_state("orientation_running", True)
        self.catalog.set_state("orientation_apply_paused", False)

    def next_check(self):
        row = self.db.execute("""SELECT a.* FROM orientation_checks c JOIN assets a ON a.id=c.asset_id
            WHERE c.status='pending' AND c.excluded=0 AND a.present=1 AND a.metadata_ready=1 AND c.file_version=a.version
            AND c.model_version=? ORDER BY a.id LIMIT 1""", (ORIENTATION_VERSION,)).fetchone()
        if row:
            with self.db:
                self.db.execute("UPDATE orientation_checks SET status='running' WHERE asset_id=?", (row['id'],))
        return dict(row) if row else None

    def complete(self, asset, result, elapsed):
        rotation = result.get("rotation")
        if rotation not in (None, 0, 90, 180, 270):
            raise ValueError("Недопустимый угол рекомендации")
        with self.db:
            self.db.execute("""UPDATE orientation_checks SET status='done',rotation=?,certain=?,reason=?,result_json=?,error=NULL,
                selected=CASE WHEN excluded=0 THEN ? ELSE 0 END,elapsed=?,updated_at=? WHERE asset_id=? AND file_version=?""",
                (rotation, int(result["certain"]), result["reason"], json.dumps(result, ensure_ascii=False),
                 int(bool(result["certain"] and rotation)), elapsed, time.time(), asset["id"], asset["version"]))

    def fail(self, asset, error, elapsed=0):
        with self.db:
            self.db.execute("UPDATE orientation_checks SET status='error',error=?,selected=0,elapsed=?,updated_at=? WHERE asset_id=? AND file_version=?",
                            (str(error), elapsed, time.time(), asset["id"], asset["version"]))

    def retry(self):
        with self.db:
            self.db.execute("UPDATE orientation_checks SET status='pending',error=NULL WHERE status='error' AND excluded=0")
        self.catalog.set_state("orientation_running", True)

    def stats(self):
        row = self.db.execute("""SELECT count(*) total,
            coalesce(sum(c.status='done'),0) checked,coalesce(sum(c.status IN ('pending','running') AND NOT c.excluded),0) pending,
            coalesce(sum(c.status='done' AND c.rotation>0 AND NOT c.excluded),0) recommendations,
            coalesce(sum(c.selected=1 AND NOT c.excluded AND c.status='done' AND c.rotation>0),0) selected,
            coalesce(sum(c.excluded),0) excluded,coalesce(sum(c.status='error'),0) errors,
            avg(c.elapsed) average_seconds FROM orientation_checks c JOIN assets a ON a.id=c.asset_id
            WHERE c.file_version=a.version AND a.present=1 AND c.model_version=?""", (ORIENTATION_VERSION,)).fetchone()
        result = dict(row)
        result['running'] = self.catalog.state('orientation_running', False)
        result['apply_paused'] = self.catalog.state('orientation_apply_paused', False)
        result['edits_pending'] = self.db.execute("SELECT count(*) FROM rotation_edits WHERE status IN ('queued','prepared','indexing','undo_queued','undo_prepared','undo_indexing')").fetchone()[0]
        result['applied'] = self.db.execute("SELECT count(*) FROM rotation_edits WHERE status='done'").fetchone()[0]
        result['edit_errors'] = self.db.execute("SELECT count(*) FROM rotation_edits WHERE status='error'").fetchone()[0]
        row = self.db.execute("SELECT batch_id FROM rotation_edits WHERE status='done' ORDER BY id DESC LIMIT 1").fetchone()
        result['last_batch'] = row[0] if row else None
        return result

    def page(self, group='suggestions', offset=0, limit=40):
        limit, offset = min(40, max(1, limit)), max(0, offset)
        where = {"suggestions":"c.status='done' AND c.rotation>0 AND c.excluded=0 AND c.certain=1",
                 "uncertain":"c.status='done' AND c.certain=0 AND c.excluded=0",
                 "excluded":"c.excluded=1", "upright":"c.status='done' AND c.rotation=0 AND c.certain=1 AND c.excluded=0",
                 "errors":"c.status='error'"}.get(group)
        if group in {'applied','edit_errors'}:
            state = 'done' if group == 'applied' else 'error'
            sql = " FROM rotation_edits e JOIN assets a ON a.id=e.asset_id WHERE e.status=?"
            total = self.db.execute("SELECT count(*)"+sql, (state,)).fetchone()[0]
            items = [dict(r) for r in self.db.execute("SELECT a.*,e.id edit_id,e.batch_id,e.rotation proposed_rotation,e.error rotation_error,e.before_hash,e.after_hash,e.backup_path"+sql+" ORDER BY e.id DESC LIMIT ? OFFSET ?", (state,limit,offset))]
        else:
            if where is None:
                raise ValueError('Неизвестная группа рекомендаций')
            sql = " FROM orientation_checks c JOIN assets a ON a.id=c.asset_id WHERE c.file_version=a.version AND a.present=1 AND c.model_version=? AND " + where
            total = self.db.execute("SELECT count(*)"+sql, (ORIENTATION_VERSION,)).fetchone()[0]
            items = [dict(r) for r in self.db.execute("SELECT a.*,c.rotation proposed_rotation,c.reason rotation_reason,c.certain,c.selected,c.excluded,c.error rotation_error"+sql+" ORDER BY a.id LIMIT ? OFFSET ?", (ORIENTATION_VERSION,limit,offset))]
        return {"items":items,"total":total,"offset":offset,"group":group}

    def decision(self, asset_id, version, selected=None, excluded=None, rotation=None):
        row = self.db.execute("""SELECT c.* FROM orientation_checks c JOIN assets a ON a.id=c.asset_id
            WHERE c.asset_id=? AND c.file_version=? AND a.version=c.file_version AND a.present=1""", (asset_id,version)).fetchone()
        if not row:
            raise ValueError('Фотография изменилась; обновите список рекомендаций')
        if excluded is True or selected is False:
            with self.db:
                self.db.execute("UPDATE rotation_edits SET status='cancelled' WHERE asset_id=? AND status='queued'",(asset_id,))
        if self.db.execute("SELECT 1 FROM rotation_edits WHERE asset_id=? AND status IN ('queued','prepared','indexing','undo_queued','undo_prepared','undo_indexing')",(asset_id,)).fetchone():
            raise ValueError('Изменение уже выполняется; дождитесь завершения')
        fields, values = [], []
        if rotation is not None:
            if rotation not in (0,90,180,270):
                raise ValueError('Недопустимый угол')
            fields += ['rotation=?']; values += [rotation]
        if selected is not None:
            if selected and (row['excluded'] or (rotation if rotation is not None else row['rotation']) not in (90,180,270)):
                raise ValueError('Сначала задайте поворот и снимите исключение')
            fields += ['selected=?']; values += [int(selected)]
        if excluded is not None:
            fields += ['excluded=?','selected=0']; values += [int(excluded)]
        if fields:
            with self.db:
                self.db.execute('UPDATE orientation_checks SET '+','.join(fields)+' WHERE asset_id=?', values+[asset_id])

    def queue_apply(self):
        batch = uuid4().hex
        with self.db:
            cursor = self.db.execute("""INSERT INTO rotation_edits(batch_id,asset_id,file_version,rotation,path,created_at)
                SELECT ?,a.id,a.version,c.rotation,a.path,? FROM orientation_checks c JOIN assets a ON a.id=c.asset_id
                WHERE c.file_version=a.version AND c.model_version=? AND c.selected=1 AND c.excluded=0 AND c.status='done'
                AND c.rotation IN (90,180,270) AND a.present=1 AND NOT EXISTS(SELECT 1 FROM rotation_edits e WHERE e.asset_id=a.id
                AND e.status IN ('queued','prepared','indexing','undo_queued','undo_prepared','undo_indexing'))""", (batch,time.time(),ORIENTATION_VERSION))
            count = cursor.rowcount
            self.db.execute("UPDATE orientation_checks SET selected=0 WHERE asset_id IN (SELECT asset_id FROM rotation_edits WHERE batch_id=?)", (batch,))
        if count:
            self.catalog.set_state('orientation_apply_paused', False)
        return {"batch_id":batch,"count":count}

    def queue_undo(self, batch_id):
        if not isinstance(batch_id,str) or not batch_id:
            raise ValueError('Не выбрана партия для восстановления')
        if self.db.execute("SELECT 1 FROM rotation_edits WHERE batch_id=? AND status IN ('queued','prepared','indexing','undo_queued','undo_prepared','undo_indexing')",(batch_id,)).fetchone():
            raise ValueError('Сначала дождитесь завершения партии или отмените ожидающие повороты')
        with self.db:
            count = self.db.execute("UPDATE rotation_edits SET status='undo_queued',error=NULL WHERE batch_id=? AND status='done'", (batch_id,)).rowcount
        if count:
            self.catalog.set_state('orientation_apply_paused', False)
        return count
