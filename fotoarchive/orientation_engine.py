"""One-file transactions, restart recovery, and background orientation work."""
import json
import os
from pathlib import Path
import shutil
import time

from .inference import GPUUnavailable
from .media import sha256
from .orientation import OrientationAnalyzer, ORIENTATION_VERSION
from .orientation_store import OrientationStore
from .rotation_files import write_rotated


class OrientationService:
    def __init__(self, engine):
        self.engine = engine
        self.store = OrientationStore(engine.catalog)
        self.db = engine.catalog.db

    def update_edit(self, edit_id, **values):
        with self.db:
            self.db.execute('UPDATE rotation_edits SET '+','.join(key+'=?' for key in values)+' WHERE id=?', list(values.values())+[edit_id])

    def apply_file(self, edit):
        catalog = self.engine.catalog
        path = Path(edit['path'])
        asset = catalog.get(edit['asset_id'])
        if path.resolve() != Path(asset['path']).resolve() or not path.resolve().is_relative_to(catalog.cfg.root.resolve()):
            raise ValueError('Путь файла не соответствует исходному каталогу')
        undo = edit['status'].startswith('undo_')
        if edit['status'] in {'queued','undo_queued'}:
            before = path.stat()
            current_hash = sha256(path)
            if undo:
                if current_hash != edit['after_hash']:
                    raise ValueError('Файл изменился после поворота; восстановление отменено, чтобы сохранить ваши изменения')
                backup = Path(edit['backup_path'])
                if sha256(backup) != edit['before_hash']:
                    raise ValueError('Резервная копия повреждена')
            else:
                if asset['version'] != edit['file_version'] or before.st_size != asset['size'] or before.st_mtime_ns != asset['mtime_ns']:
                    raise ValueError('Файл изменился после рекомендации; выполните повторную проверку')
                if asset.get('content_hash') and current_hash != asset['content_hash']:
                    raise ValueError('Содержимое файла изменилось после индексации')
                backup = catalog.cfg.data_dir / 'backups/rotations' / edit['batch_id'] / f"{edit['asset_id']}_{edit['file_version']}{path.suffix}"
                backup.parent.mkdir(parents=True, exist_ok=True)
                if not backup.exists():
                    temporary = backup.with_suffix(backup.suffix+'.tmp')
                    shutil.copyfile(path,temporary)
                    with temporary.open('rb+') as stream:
                        os.fsync(stream.fileno())
                    temporary.replace(backup)
                if sha256(backup) != current_hash:
                    raise ValueError('Не удалось проверить резервную копию; оригинал не изменён')
            stage = path.with_name(f'.fotoarchive-{edit["batch_id"]}-{edit["id"]}.tmp')
            if undo:
                shutil.copyfile(backup,stage)
                with stage.open('rb+') as stream:
                    os.fsync(stream.fileno())
            else:
                write_rotated(backup,stage,edit['rotation'])
            after_hash = sha256(stage)
            if undo and after_hash != edit['before_hash']:
                raise ValueError('Восстановленная копия не совпала с оригиналом')
            if sha256(path) != current_hash:
                stage.unlink(missing_ok=True)
                raise ValueError('Файл изменился во время подготовки; оригинал не перезаписан')
            values = dict(status='undo_prepared' if undo else 'prepared',stage_path=str(stage),backup_path=str(backup))
            if not undo:
                values.update(before_hash=current_hash,after_hash=after_hash)
            self.update_edit(edit['id'],**values)
            edit = dict(self.db.execute('SELECT * FROM rotation_edits WHERE id=?',(edit['id'],)).fetchone())
        current_hash = sha256(path)
        expected_before = edit['after_hash'] if undo else edit['before_hash']
        expected_after = edit['before_hash'] if undo else edit['after_hash']
        stage = Path(edit['stage_path'])
        if stage.parent.resolve() != path.parent.resolve() or stage.name != f'.fotoarchive-{edit["batch_id"]}-{edit["id"]}.tmp':
            raise ValueError('Некорректный временный путь в журнале')
        if current_hash == expected_before:
            if sha256(stage) != expected_after:
                raise ValueError('Временный файл повреждён')
            # The staged sibling and original are on the same volume; replacement
            # is atomic. The persisted hashes recover a crash immediately after it.
            os.replace(stage,path)
        elif current_hash != expected_after:
            raise ValueError('Файл изменился после подготовки; оригинал не перезаписан')
        stage.unlink(missing_ok=True)
        asset_id, _ = catalog.register(path)
        version = catalog.get(asset_id)['version']
        self.update_edit(edit['id'],status='undo_indexing' if undo else 'indexing',applied_version=version)
        with self.db:
            self.db.execute("""UPDATE orientation_checks SET file_version=?,model_version=?,status='done',rotation=0,certain=1,
                selected=0,error=NULL,reason=?,updated_at=? WHERE asset_id=?""", (version,ORIENTATION_VERSION,
                'Исходный файл восстановлен из резервной копии.' if undo else 'Поворот подтверждён и применён к исходному файлу.',time.time(),asset_id))
            if undo:
                self.db.execute('UPDATE orientation_checks SET certain=0 WHERE asset_id=?',(asset_id,))
        self.engine.emit({'type':'rotation_changed','asset_id':asset_id})

    def tick(self, allow_analysis=True):
        catalog = self.engine.catalog
        edit = None
        if not catalog.state('orientation_apply_paused', False):
            edit = self.db.execute("SELECT * FROM rotation_edits WHERE status IN ('queued','prepared','indexing','undo_queued','undo_prepared','undo_indexing') ORDER BY id LIMIT 1").fetchone()
        if edit:
            edit = dict(edit)
            self.engine.emit({'type':'working','stage':'rotation','filename':Path(edit['path']).name})
            try:
                if edit['status'] in {'indexing','undo_indexing'}:
                    job = self.engine.process_one(asset_id=edit['asset_id'])
                    if job is None:
                        if self.db.execute("""SELECT 1 FROM jobs j JOIN assets a ON a.id=j.asset_id
                            WHERE j.asset_id=? AND j.file_version=a.version AND j.status IN ('pending','running')
                            AND (j.stage='metadata' OR a.metadata_ready=1) LIMIT 1""",(edit['asset_id'],)).fetchone():
                            return True
                        self.engine.index.flush_all()
                        errors = self.db.execute("SELECT count(*) FROM jobs WHERE asset_id=? AND status='error'",(edit['asset_id'],)).fetchone()[0]
                        self.update_edit(edit['id'],status='undone' if edit['status']=='undo_indexing' else 'done',
                                         error='Файл повёрнут, но часть поисковых данных требует повторной обработки' if errors else None)
                        self.engine.emit({'type':'rotation_changed','asset_id':edit['asset_id']})
                else:
                    self.apply_file(edit)
            except Exception as exc:
                status = 'done' if edit['status']=='indexing' else 'undone' if edit['status']=='undo_indexing' else 'error'
                self.update_edit(edit['id'],status=status,error=str(exc))
                self.engine.emit({'type':'orientation_notice','message':str(exc)})
            return True
        if not allow_analysis or not catalog.state('orientation_running',False):
            return False
        asset = self.store.next_check()
        if not asset:
            if not self.store.stats()['pending']:
                catalog.set_state('orientation_running',False)
            return False
        start = time.perf_counter()
        self.engine.emit({'type':'working','stage':'orientation','filename':asset['filename']})
        try:
            path = Path(asset['path'])
            before = path.stat()
            if (before.st_size,before.st_mtime_ns) != (asset['size'],asset['mtime_ns']):
                raise ValueError('Файл изменился; сначала обновите каталог')
            result = OrientationAnalyzer(self.engine.face_model(),self.engine.vision()).analyze(path)
            after = path.stat()
            if (before.st_size,before.st_mtime_ns) != (after.st_size,after.st_mtime_ns):
                raise ValueError('Файл изменился во время проверки')
            self.store.complete(asset,result,time.perf_counter()-start)
        except Exception as exc:
            self.store.fail(asset,exc,time.perf_counter()-start)
            if isinstance(exc,GPUUnavailable):
                catalog.set_state('orientation_running',False)
        self.engine.emit({'type':'orientation_progress','stats':self.store.stats()})
        return True
