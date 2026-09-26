from __future__ import annotations

import hashlib
import json
import queue
import time
import traceback
from collections import deque
from contextlib import ExitStack
from pathlib import Path
from filelock import FileLock, Timeout as LockTimeout

from .catalog import Catalog, Filters, enumerate_source
from .config import Settings, VERIFY_VERSION, FACE_VERSION
from .inference import Embedder, GPUUnavailable, VisionLanguage
from .media import extract_metadata, atomic_thumbnail, sha256, visual_path
from .search import SearchIndex
from .search_session import SearchSession


class Engine:
    def __init__(self, cfg: Settings, emit=lambda event: None):
        self.cfg, self.emit = cfg, emit
        cfg.initialize()
        self.lock = FileLock(cfg.data_dir / "worker.lock")
        try:
            self.lock.acquire(timeout=0)
        except LockTimeout as exc:
            raise RuntimeError("Этот каталог уже обрабатывается другим экземпляром приложения.") from exc
        try:
            self.catalog = Catalog(cfg)
            self.catalog.recover()
            from .media_upgrade import apply
            apply(self.catalog)
            self.index = SearchIndex(self.catalog)
        except Exception:
            if hasattr(self, "catalog"):
                self.catalog.close()
            self.lock.release()
            raise
        self.embedder = None
        self.vlm = None
        self.face_models = None
        self.places = None
        self.processed_since_flush = 0
        from .processing_metrics import ProcessingMetrics
        self.metrics = ProcessingMetrics()
        self.catalog.on_job_finished = self.metrics.record
        from .pipeline import ProcessingPipeline
        self.pipeline = ProcessingPipeline(self)

    def embeddings(self):
        if self.embedder is None:
            self.embedder = Embedder(self.cfg)
        return self.embedder

    def vision(self):
        if self.vlm is None:
            self.vlm = VisionLanguage(self.cfg)
        return self.vlm

    def face_model(self):
        if self.face_models is None:
            from .faces import FaceModels
            self.face_models = FaceModels(self.cfg)
        return self.face_models

    def location(self, path):
        from .location import Places, extract_location
        if self.places is None:
            self.places = Places(self.cfg)
        return self.places.enrich(extract_location(path))

    def load_faces(self, asset_id, unit_id=None):
        asset = self.catalog.media_units.asset_at(self.catalog.get(asset_id), unit_id)
        if not asset:
            return []
        unit_id = asset.get('unit_id')
        unit_done = self.catalog.db.execute("SELECT status FROM unit_jobs WHERE unit_id=? AND stage='faces'", (unit_id,)).fetchone() if unit_id else None
        ready = unit_done and unit_done[0] == 'done' if unit_id else asset['face_version'] == FACE_VERSION
        path = Path(asset["path"])
        try:
            before = path.stat()
        except OSError:
            if ready:
                return self.catalog.faces_for(asset_id, unit_id)
            raise
        if before.st_size != asset["size"] or before.st_mtime_ns != asset["mtime_ns"]:
            raise ValueError("Снимок изменился. Обновите каталог перед поиском человека.")
        if ready:
            return self.catalog.faces_for(asset_id, unit_id)
        records = self.face_model().process(visual_path(asset, self.cfg))
        after = path.stat()
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise ValueError("Снимок изменился во время анализа.")
        job = {"asset_id": asset_id, "file_version": asset["version"], "stage": "faces"}
        if unit_id:
            job.update(unit_id=unit_id, timestamp_ms=asset['timestamp_ms'])
        self.catalog.complete_faces(job, records)
        self.catalog.finish_job(job, 0)
        self.index.start_flush()
        return self.catalog.faces_for(asset_id, unit_id)

    def scan(self, includes=None, skip_includes=()):
        from dataclasses import replace
        count = skipped = changed = 0
        cfg = replace(self.cfg, includes=includes) if includes is not None else self.cfg
        for path, supported in enumerate_source(cfg, skip_includes):
            if supported:
                _, updated = self.catalog.register(path)
                count += 1
                changed += int(updated)
            else:
                skipped += 1
            yield {"files": count, "skipped": skipped, "changed": changed}
        self.catalog.set_state("last_scan", {"files": count, "skipped": skipped, "changed": changed, "time": time.time()})

    def flush_progress(self, count=1):
        self.processed_since_flush += count
        if self.processed_since_flush >= 16 and self.index.start_flush():
            self.processed_since_flush = 0

    def process_embedding_batch(self, limit=4):
        prepared = self.pipeline.inputs.take(limit)
        if not prepared:
            return None
        valid = []
        for job, asset, result in prepared:
            current = self.catalog.get(asset['id'])
            if not current or not current['present'] or current['version'] != job['file_version']:
                continue
            try:
                stat = Path(asset['path']).stat()
                if result.get('changed') or (stat.st_size, stat.st_mtime_ns) != (asset['size'], asset['mtime_ns']):
                    self.catalog.register(Path(asset['path']))
                    self.catalog.requeue_jobs([job])
                    continue
                if result.get('error'):
                    raise ValueError(result['error'])
                valid.append((job, asset, result))
            except Exception as exc:
                self.catalog.finish_job(job, result.get('elapsed', 0), str(exc))
                self.emit({'type':'job_error','message':f"{asset['filename']}: {exc}"})
        if valid:
            self.emit({'type':'working','stage':'embedding',
                       'filename':f"{valid[0][1]['filename']} · {len(valid)} фото"})
            started = time.perf_counter()
            try:
                vectors = self.embeddings().prepared_images([result['pixels'] for _, _, result in valid])
                if len(vectors) != len(valid):
                    raise ValueError('Неполный результат обработки порции изображений')
            except Exception as exc:
                for job, asset, result in valid:
                    self.catalog.finish_job(job, result.get('elapsed', 0), str(exc))
                    self.emit({'type':'job_error','message':f"{asset['filename']}: {exc}"})
                if isinstance(exc, GPUUnavailable):
                    raise
            else:
                elapsed = (time.perf_counter()-started)/len(valid)
                for (job, asset, result), vector in zip(valid, vectors):
                    try:
                        stat = Path(asset['path']).stat()
                        if (stat.st_size, stat.st_mtime_ns) != (asset['size'], asset['mtime_ns']):
                            self.catalog.register(Path(asset['path']))
                            self.catalog.requeue_jobs([job])
                            continue
                        self.catalog.complete_embedding(job, vector)
                        self.catalog.finish_job(job, elapsed+result.get('elapsed', 0))
                    except Exception as exc:
                        self.catalog.finish_job(job, elapsed, str(exc))
                        self.emit({'type':'job_error','message':f"{asset['filename']}: {exc}"})
        self.flush_progress(len(prepared))
        return {'stage':'embedding','batch_count':len(prepared)}

    def process_one(self, stages=None, asset_id=None):
        job = self.catalog.next_job(stages, asset_id)
        if not job:
            return None
        asset = self.catalog.job_asset(job)
        path = Path(asset["path"])
        start = time.perf_counter()
        self.emit({"type": "working", "stage": job["stage"], "filename": asset["filename"]})
        try:
            stat = path.stat()
            if stat.st_size != asset["size"] or stat.st_mtime_ns != asset["mtime_ns"]:
                self.catalog.register(path)
                return job
            if job["stage"] == "metadata":
                result = extract_metadata(path)
                thumbnail = self.cfg.data_dir / "thumbnails" / str(asset["id"] // 1000) / f"{asset['id']}_{asset['version']}.webp"
                atomic_thumbnail(path, thumbnail)
                digest = sha256(path)
            elif job["stage"] == "embedding":
                result = self.embeddings().image(visual_path(asset, self.cfg))
            elif job["stage"] == "faces":
                result = self.face_model().process(visual_path(asset, self.cfg))
            elif job["stage"] == "location":
                result = self.location(path)
                geo_vector = self.embeddings().text(result["geo_text"]) if result["geo_text"] else None
            else:
                result = self.vision().describe(visual_path(asset, self.cfg))
            after = path.stat()
            if after.st_size != stat.st_size or after.st_mtime_ns != stat.st_mtime_ns:
                self.catalog.register(path)
                return job
            if job["stage"] == "metadata":
                self.catalog.complete_metadata(job, result, thumbnail, digest)
            elif job["stage"] == "embedding":
                self.catalog.complete_embedding(job, result)
            elif job["stage"] == "faces":
                self.catalog.complete_faces(job, result)
            elif job["stage"] == "location":
                self.catalog.complete_location(job, result, geo_vector)
            else:
                self.catalog.complete_caption(job, result)
            self.catalog.finish_job(job, time.perf_counter() - start)
        except Exception as exc:
            self.catalog.finish_job(job, time.perf_counter() - start, f"{type(exc).__name__}: {exc}")
            self.emit({"type": "job_error", "message": f"{asset['filename']}: {exc}"})
            if isinstance(exc, GPUUnavailable):
                raise
        self.flush_progress()
        return job

    def verify(self, asset, conditions):
        info = Path(asset["path"]).stat()
        if info.st_size != asset["size"] or info.st_mtime_ns != asset["mtime_ns"]:
            raise ValueError("Файл изменился. Обновите каталог перед проверкой условий.")
        asset = self.catalog.media_units.asset_at(asset, asset.get('unit_id'))
        payload = json.dumps([VERIFY_VERSION, asset["id"], asset["version"], asset.get('unit_id'), conditions], ensure_ascii=False)
        key = hashlib.sha256(payload.encode()).hexdigest()
        cached = self.catalog.db.execute("SELECT result_json FROM verification_cache WHERE cache_key=?", (key,)).fetchone()
        if cached:
            return json.loads(cached[0]) | {"cached": True}
        result = self.vision().verify(visual_path(asset, self.cfg), conditions)
        if asset.get('media_kind') == 'video':
            result.update(scope='frame', timestamp_ms=asset.get('timestamp_ms', 0))
        with self.catalog.db:
            self.catalog.db.execute("INSERT OR REPLACE INTO verification_cache VALUES(?,?,?,?,?)", (key, asset["id"], asset["version"], json.dumps(result, ensure_ascii=False), time.time()))
        return result

    def close(self):
        # Run every cleanup even if the last in-flight GPU task reports an error.
        with ExitStack() as cleanup:
            cleanup.callback(self.lock.release)
            cleanup.callback(self.catalog.close)
            cleanup.callback(self.index.close)
            if self.places:
                cleanup.callback(self.places.close)
            if self.vlm:
                cleanup.callback(self.vlm.close)
            cleanup.callback(self.pipeline.close)


def worker_main(data_dir, commands, events, shutdown, interactive_state=None, remote_lifetime=None):
    """One owner for DB/index writes and GPU sessions; UI never runs inference."""
    cfg = Settings.load(data_dir)

    def emit(event):
        if shutdown.is_set():
            return
        if event['type'] in ('ready', 'scan_done', 'index_done') and 'facets' in event and engine:
            engine.catalog.set_state('startup_facets', event['facets'])
        try:
            events.put(event, timeout=1)
        except queue.Full:
            if event["type"] not in {"status", "working"}:
                events.put(event)

    engine = None
    from .search_history import SearchHistory
    search_history = SearchHistory()
    try:
        engine = Engine(cfg, emit)
        from .orientation_engine import OrientationService
        orientation = OrientationService(engine)
        pipeline = engine.pipeline
        if remote_lifetime is not None:
            from .remote_jobs import RemoteJobs
            pipeline.remote = RemoteJobs(engine,lambda: not shutdown.is_set() and
                remote_lifetime[1]>0 and remote_lifetime[2]>0 and time.monotonic()-remote_lifetime[0]<6)
        paused = engine.catalog.state("paused", True)
        # Opening the catalogue never schedules source discovery, including
        # after decoder upgrades. Only explicit requests add scans to the queue.
        context = None
        reviewing = None
        review_id = None
        checking = deque()
        last_status = 0
        last_saved_status = 0
        maintenance_needed = False
        emit({"type": "ready", "facets": engine.catalog.facets(), "includes": cfg.includes})
        while not shutdown.is_set():
            if pipeline.remote:
                pipeline.remote.set_active(not paused)
                if pipeline.remote.collect():
                    maintenance_needed = True
            if not cfg.local_enabled and not pipeline.captions.pending:
                if engine.vlm:
                    engine.vlm.close()
                    engine.vlm = None
                engine.embedder = engine.face_models = None
            try:
                pipeline.inventory.collect()
                if pipeline.captions.collect():
                    pipeline.gpu_completed += 1
                    maintenance_needed = True
                engine.index.collect_flush()
                if pipeline.cpu.collect():
                    maintenance_needed = True
            except Exception as exc:
                pipeline.cpu.recover_broken()
                pipeline.inputs.cancel()
                pipeline.captions.cancel()
                if isinstance(exc,GPUUnavailable) and cfg.remote_enabled:
                    cfg.local_enabled = False
                    cfg.save()
                else:
                    paused = True
                engine.catalog.set_state('paused',paused)
                emit({'type':'error','message':str(exc)})
            try:
                command = commands.get(timeout=0 if reviewing is not None else 0.005 if (pipeline.scanning or checking or (not paused and cfg.local_enabled) or pipeline.cpu.pending) else 0.2)
            except queue.Empty:
                command = None
            if command:
                from .interactive import obsolete_command
                if obsolete_command(command, interactive_state):
                    continue
                request_id = command.get("id")
                action = command["action"]
                try:
                    if action == "stop":
                        break
                    elif action == 'clear_search':
                        checking.clear()
                        context = None
                        if reviewing:
                            reviewing.close()
                        reviewing = None
                    elif action == "pause":
                        paused = True
                        pipeline.inputs.cancel()
                        pipeline.captions.cancel()
                        pipeline.cpu.cancel()
                        engine.catalog.set_state("paused", True)
                    elif action == 'remote_enabled':
                        cfg.remote_enabled = bool(command['enabled'])
                        if not cfg.remote_enabled and pipeline.remote:
                            pipeline.remote.cancel()
                        if cfg.remote_enabled:
                            paused = False
                            engine.catalog.set_state('paused',False)
                        cfg.save()
                    elif action == 'local_enabled':
                        cfg.local_enabled = bool(command['enabled'])
                        if not cfg.local_enabled:
                            pipeline.inputs.cancel()
                            pipeline.captions.cancel()
                        else:
                            paused = False
                            engine.catalog.set_state('paused',False)
                        cfg.save()
                    elif action == "resume":
                        paused = False
                        engine.catalog.set_state("paused", False)
                        if pipeline.inventory.phase == 'error':
                            pipeline.inventory.start()
                    elif action == "scan":
                        pipeline.request_scan()
                        paused = False
                        engine.catalog.set_state("paused", False)
                    elif action == 'check_updates':
                        pipeline.request_scan(reconcile=True)
                        paused = False
                        engine.catalog.set_state('paused', False)
                    elif action in {"add_folder", "add_folders"}:
                        from .folders import add_folders
                        result = add_folders(cfg, command.get("paths", [command.get("path")]))
                        if result["added"]:
                            pipeline.request_scan(result['added'],result['scan_excludes'])
                            paused = False
                            engine.catalog.set_state("paused", False)
                        emit({"type": "folders_added", **result})
                    elif action == "retry":
                        engine.catalog.retry_errors()
                        paused = False
                        engine.catalog.set_state('paused',False)
                    elif action == "download_models":
                        from .bootstrap import main as download
                        download(cfg, lambda text: emit({"type": "working", "stage": "setup", "filename": text}))
                        emit({"type": "message", "message": "Модели загружены. Можно начать индексацию."})
                    elif action == "errors":
                        emit({"type": "errors", "items": engine.catalog.errors()})
                    elif action.startswith('orientation_'):
                        store = orientation.store
                        if action == 'orientation_start':
                            store.enqueue(Filters(**command.get('filters',{})))
                        elif action == 'orientation_pause':
                            engine.catalog.set_state('orientation_running',False)
                            engine.catalog.set_state('orientation_apply_paused',True)
                        elif action == 'orientation_retry':
                            store.retry()
                        elif action == 'orientation_decide':
                            store.decision(command['asset_id'],command['version'],command.get('selected'),command.get('excluded'),command.get('rotation'))
                        elif action == 'orientation_apply':
                            result = store.queue_apply()
                            emit({'type':'orientation_notice','message':f"Поставлено в очередь поворота: {result['count']}. Резервные копии сохраняются перед изменением файлов."})
                        elif action == 'orientation_undo':
                            count = store.queue_undo(command['batch_id'])
                            emit({'type':'orientation_notice','message':f'Поставлено в очередь восстановления: {count}.'})
                        elif action == 'orientation_cancel_edits':
                            with engine.catalog.db:
                                count = engine.catalog.db.execute("UPDATE rotation_edits SET status='cancelled' WHERE status='queued'").rowcount
                            emit({'type':'orientation_notice','message':f'Отменено ещё не начатых поворотов: {count}.'})
                        elif action == 'orientation_page':
                            emit({'type':'orientation_page','id':request_id,**store.page(command.get('group','suggestions'),command.get('offset',0))})
                        elif action != 'orientation_status':
                            raise ValueError('Неизвестная операция поворота')
                        emit({'type':'orientation_progress','stats':store.stats()})
                    elif action == "facets":
                        emit({"type": "ready", "facets": engine.catalog.facets()})
                    elif action == "diagnostics":
                        import sys
                        vector = engine.embeddings().text("люди на берегу реки")
                        asset = engine.catalog.get(command["asset_id"])
                        description = engine.vision().describe(Path(asset["path"]))
                        face_asset = engine.catalog.db.execute("""SELECT a.id,a.path,f.id face_id FROM faces f JOIN assets a ON a.id=f.asset_id
                            WHERE f.file_version=a.version AND f.model_version=? AND a.present=1 LIMIT 1""", (FACE_VERSION,)).fetchone()
                        face_records = engine.face_model().process(Path(face_asset["path"])) if face_asset else []
                        emit({"type": "diagnostics", "dimensions": len(vector), "description": description,
                              "faces_detected": len(face_records), "face_dimensions": len(face_records[0]["vector"]) if face_records else None,
                              "sample_face_id": face_asset["face_id"] if face_asset else None,
                              "face_providers": {name: session.get_providers() for name, session in engine.face_model().sessions.items()},
                              "providers": engine.embeddings()._session("text").get_providers(), "vlm_log": str(engine.vision().log_path),
                              "worker_executable": sys.executable, "frozen": bool(getattr(sys, "frozen", False)),
                              "runtime_prefix": sys.prefix})
                    elif action == "cancel" and (context is None or request_id == context.request_id):
                        checking.clear()
                    elif action == "faces":
                        asset = engine.catalog.media_units.asset_at(engine.catalog.get(command["asset_id"]), command.get('unit_id'))
                        faces = engine.load_faces(asset["id"], asset.get('unit_id'))
                        emit({"type": "face_list", "asset_id": asset["id"], "version": asset["version"], "faces": faces, 'unit_id': asset.get('unit_id')})
                    elif action == "face_suggestions":
                        from .face_search import boundary_suggestions, resolve_examples
                        if reviewing:
                            reviewing.close()
                        reviewing = None
                        review_id = request_id
                        engine.index.flush_all()
                        _, vectors = resolve_examples(engine.catalog, command["face_ids"])
                        reviewing = boundary_suggestions(engine.index, vectors, Filters(**command.get("filters", {})),
                                                         set(command.get("excluded", [])) | set(command["face_ids"]))
                    elif action == "cancel_face_suggestions" and request_id == review_id:
                        if reviewing:
                            reviewing.close()
                        reviewing = None
                    elif action in {"browse", "search", "similar", "face_search"}:
                        checking.clear()
                        cached = search_history.get(command)
                        if cached:
                            context = cached
                            restore = context.restore_position(command['refresh_anchor'])
                            offset = restore['row']//context.PAGE_SIZE*context.PAGE_SIZE
                            emit(dict(type='results',id=request_id,total=context.total,conditions=context.conditions,
                                semantic=context.mode!='browse',mode=context.mode,examples=context.examples,
                                restore=restore,**context.page(offset=offset,expand=True),**context.counts()))
                            if context.conditions:
                                checking.extend(context.pending())
                            continue
                        if action != 'browse':
                            engine.index.flush_all()
                        filters = Filters(**command.get("filters", {}))
                        query = command.get("query", "").strip()
                        conditions = []
                        examples = []
                        face_groups = []
                        positive = query
                        if action == "search" and command.get("complex"):
                            emit({"type": "working", "stage": "query", "filename": "Разбираю условия запроса"})
                            parsed = engine.vision().parse(query)
                            positive = parsed["positive_query"]
                            conditions = parsed["conditions"]
                        if action == "face_search":
                            from .face_search import resolve_examples
                            if command.get('people'):
                                vector = []
                                for person in command['people']:
                                    faces, vectors = resolve_examples(engine.catalog, person['examples'])
                                    examples.extend(faces)
                                    vector.extend(vectors)
                                    face_groups.append(dict(vectors=vectors, rejected=person.get('rejected', [])))
                            else:
                                examples, vector = resolve_examples(engine.catalog, command.get("face_ids", [command.get("face_id")]))
                        elif action == "similar":
                            vector = engine.catalog.vector(command["asset_id"], command.get('unit_id'))
                            if vector is None:
                                raise ValueError("Для этого снимка ещё не построен эмбеддинг")
                        elif action == "browse":
                            vector = None
                        else:
                            vector = engine.embeddings().text(positive) if positive else None
                        mode = "browse" if action == "browse" else "face" if action == "face_search" else "semantic"
                        read_catalog = Catalog.open_reader(cfg)
                        try:
                            context = SearchSession(read_catalog, engine.index, request_id, filters, positive, vector, conditions,
                                                exclude_id=command.get("asset_id") if action == "similar" else None, mode=mode,
                                                excluded_faces=command.get("excluded_faces", []),
                                                presentation=command.get('presentation'), face_groups=face_groups,
                                                people_mode=command.get('people_mode','all'))
                        except Exception:
                            read_catalog.close()
                            raise
                        context.examples = examples
                        search_history.put(command,context)
                        restore = (context.restore_position(command['refresh_anchor'])
                                   if command.get('refresh_anchor') else None)
                        page_offset = restore['row'] // context.PAGE_SIZE * context.PAGE_SIZE if restore else 0
                        emit({"type": "results", "id": request_id, "total": context.total, "conditions": conditions,
                              "semantic": mode != "browse", "mode": mode, "examples": examples,
                              'restore': restore, **context.page(offset=page_offset, expand=True), **context.counts()})
                        if conditions:
                            checking.extend(context.pending())
                            if not checking:
                                emit({"type": "verification_done", "id": request_id, "checked": 0, "exhausted": True})
                    elif action == "search_page" and context and request_id == context.request_id:
                        event = {"type": "search_page", "id": request_id, "view": command.get("view", 0),
                            **context.page(command.get("offset", 0), command.get("verdict", ""), expand=True), **context.counts()}
                        if command.get('layout_until'):
                            event['layout_geometry'] = context.layout_geometry(command['layout_until'],command.get('verdict',''))
                        emit(event)
                    elif action == 'search_places' and context and request_id == context.request_id:
                        from .library import Library
                        payload = Library(context.catalog).places(context.filters,command.get('viewport',''),command.get('step',5),
                            'SELECT asset_id FROM active_search')
                        emit(dict(type='library_places',serial=command.get('serial'),**payload))
                    elif action == 'match_moments' and context and request_id == context.request_id:
                        rows = [dict(row) for row in context.catalog.db.execute('''SELECT u.id unit_id,u.timestamp_ms
                            FROM search_moments m JOIN units u ON u.id=m.unit_id WHERE m.asset_id=?
                            ORDER BY u.timestamp_ms LIMIT 100 OFFSET ?''',(command['asset_id'],command.get('offset',0)))]
                        emit(dict(type='match_moments',serial=command['serial'],offset=command.get('offset',0),items=rows))
                    elif action == "verify_more" and context and context.conditions and request_id == context.request_id and not checking:
                        checking.extend(context.pending())
                        if not checking:
                            emit({"type": "verification_done", "id": context.request_id, **context.counts(), "exhausted": True})
                except Exception as exc:
                    emit({"type": "error", "id": request_id, "action": action, "view": command.get("view"),
                          "invalid_face_ids": getattr(exc, "invalid_face_ids", []),
                          "offset": command.get("offset"), "message": str(exc)})
                # Consume pending user commands before starting another background GPU task.
                continue
            if context and interactive_state is not None and context.request_id != interactive_state[0]:
                checking.clear()
                context = None
            if not paused:
                try:
                    pipeline.scan_tick()
                    pipeline.cpu.fill()
                    if pipeline.remote:
                        pipeline.remote.fill()
                    if cfg.local_enabled:
                        pipeline.inputs.fill()
                    if cfg.local_enabled and not checking and reviewing is None:
                        pipeline.captions.fill()
                except Exception as exc:
                    pipeline.cpu.cancel()
                    pipeline.inputs.cancel()
                    pipeline.captions.cancel()
                    if isinstance(exc,GPUUnavailable) and cfg.remote_enabled:
                        cfg.local_enabled = False
                        cfg.save()
                    else:
                        paused = True
                    engine.catalog.set_state('paused',paused)
                    emit({'type':'error','message':str(exc)})
            if reviewing is not None:
                try:
                    next(reviewing)
                except StopIteration as finished:
                    emit({"type": "face_suggestions", "id": review_id, "items": finished.value})
                    reviewing = None
                except Exception as exc:
                    emit({"type": "error", "action": "face_suggestions", "id": review_id, "message": str(exc)})
                    reviewing = None
            elif checking and context:
                asset = checking.popleft()
                try:
                    result = engine.verify(asset, context.conditions)
                except Exception as exc:
                    result = {"verdict": "uncertain", "checks": [], "error": str(exc)}
                context.mark(asset, result)
                presentation_state = {}
                if context.presentation:
                    context.presentation.build(context.view_verdict)
                    presentation_state = dict(presentation_total=context.presentation.total,
                                              presentation_verdict=context.view_verdict)
                emit({"type": "verified", "id": context.request_id, "asset": asset, "result": result,
                      "groups": context.groups(), "has_more": not context.exhausted, **context.counts(),**presentation_state})
                if not checking:
                    emit({"type": "verification_done", "id": context.request_id, **context.counts(), "exhausted": not context.can_verify()})
            elif orientation.tick(allow_analysis=paused and cfg.local_enabled):
                pass
            elif not paused:
                try:
                    job = pipeline.process_gpu() if cfg.local_enabled else None
                    maintenance_needed = True
                    unfinished = (not cfg.local_enabled and engine.catalog.db.execute("""SELECT 1 FROM jobs j JOIN assets a ON a.id=j.asset_id
                        WHERE j.status IN ('pending','running') AND a.present=1 AND j.file_version=a.version LIMIT 1""").fetchone()) if job is None and pipeline.idle else True
                    if job is None and pipeline.idle and not unfinished:
                        pipeline.cpu.close()
                        engine.index.flush_all()
                        engine.index.maintain()
                        maintenance_needed = False
                        paused = True
                        engine.catalog.set_state("paused", True)
                        emit({"type": "index_done", "stats": engine.catalog.stats(), "facets": engine.catalog.facets()})
                except GPUUnavailable as exc:
                    if cfg.remote_enabled:
                        cfg.local_enabled = False
                        cfg.save()
                    else:
                        paused = True
                    pipeline.cpu.cancel()
                    pipeline.inputs.cancel()
                    pipeline.captions.cancel()
                    engine.catalog.set_state("paused", paused)
                    emit({"type": "error", "message": str(exc)})
                except Exception as exc:
                    paused = True
                    pipeline.cpu.cancel()
                    pipeline.inputs.cancel()
                    pipeline.captions.cancel()
                    engine.catalog.set_state("paused",True)
                    emit({"type": "error", "message": str(exc)})
            elif maintenance_needed and not checking and not pipeline.cpu.pending and not pipeline.captions.pending:
                engine.index.flush_all()
                engine.index.maintain()
                maintenance_needed = False
            if time.monotonic() - last_status > 1:
                status = {"type": "status", "stats": engine.catalog.stats(), "paused": paused,
                      "processing_metrics":engine.metrics.snapshot(),
                      "local_enabled":cfg.local_enabled,"remote_enabled":cfg.remote_enabled,
                      "scanning":pipeline.scanning, "scan":pipeline.scan_state,"pipeline":pipeline.status(),
                      "inventory": pipeline.inventory.status()}
                emit(status)
                if time.monotonic() - last_saved_status > 10:
                    # Persist counts, not live GPU/queue telemetry from this run.
                    engine.catalog.set_state('startup_status', {k: status[k] for k in
                        ('stats', 'paused', 'inventory', 'local_enabled', 'remote_enabled')})
                    last_saved_status = time.monotonic()
                emit({'type':'orientation_progress','stats':orientation.store.stats()})
                last_status = time.monotonic()
    except Exception as exc:
        emit({"type": "fatal", "message": str(exc), "traceback": traceback.format_exc()})
    finally:
        search_history.close()
        if engine:
            engine.close()
