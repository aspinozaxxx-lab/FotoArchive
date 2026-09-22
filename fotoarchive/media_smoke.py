"""Explicit acceptance mode for a small, separate test catalogue and real GPU."""
import json
import sys
import time
import traceback
from pathlib import Path

from .catalog import Filters
from .engine import Engine
from .faces import FaceModels
from .inference import Embedder
from .media import sha256


def run(cfg):
    manifest = json.loads((cfg.data_dir/'media-smoke-manifest.json').read_text(encoding='utf-8'))
    if cfg.root.resolve() != Path(manifest['source']).resolve():
        raise ValueError('The acceptance catalogue must use the prepared sample copies')
    report = {'frozen': bool(getattr(sys, 'frozen', False)), 'started': time.time(), 'passed': False, 'executable': sys.executable}
    output = cfg.data_dir / 'reports/media-smoke.json'
    cfg.initialize()
    engine = None
    def save():
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        engine = Engine(cfg)
        engine.embedder = Embedder(cfg, profile=True)
        engine.face_models = FaceModels(cfg, profile=True)
        for _ in engine.scan():
            pass
        engine.pipeline.cpu.fill()
        deadline = time.monotonic() + 180
        while engine.pipeline.cpu.pending:
            engine.pipeline.cpu.collect()
            if time.monotonic() > deadline:
                raise TimeoutError('CPU preparation timed out')
            time.sleep(.02)
        report['preparation'] = engine.pipeline.cpu.status()
        assert engine.catalog.stats()['metadata'] == len(manifest['samples']), engine.catalog.errors()
        report['phases'] = {}
        for stage in ('embedding', 'faces', 'location', 'caption'):
            start = time.perf_counter()
            while True:
                if stage == 'caption':
                    if not engine.pipeline.captions.fill():
                        break
                    engine.pipeline.captions.collect(wait=True)
                else:
                    if engine.process_one((stage,)) is None:
                        break
            report['phases'][stage] = time.perf_counter() - start
            report['progress'] = engine.catalog.stats()
            report['errors'] = engine.catalog.errors()
            save()
        engine.index.flush_all()
        assert not engine.catalog.errors(), engine.catalog.errors()
        query = engine.embeddings().text('люди на улице')
        photos = engine.index.candidates(query, '', Filters(media_kind='photo'))
        videos = engine.index.candidates(query, '', Filters(media_kind='video'))
        assert photos and videos
        assert all(row['media_kind'] == 'photo' for row in photos)
        assert all(row['media_kind'] == 'video' and row.get('unit_id') for row in videos)
        report['video_matches'] = [{'id': row['id'], 'timestamp_ms': row['timestamp_ms']} for row in videos]
        report['photo_results'] = len(photos)
        report['units'] = engine.catalog.db.execute('SELECT count(*) FROM units').fetchone()[0]
        report['faces'] = engine.catalog.db.execute('SELECT count(*) FROM faces').fetchone()[0]
        report['directml'] = engine.embeddings().finish_profile()
        assert report['directml']['provider_events'].get('DmlExecutionProvider', 0) > 0
        report['face_profile'] = engine.face_model().finish_profile()
        report['face_providers'] = {name: session.get_providers() for name, session in engine.face_model().sessions.items()}
        assert all(providers[0] == 'DmlExecutionProvider' for providers in report['face_providers'].values())
        report['vulkan_log'] = str(engine.vision().log_path)
        log = engine.vision().log_path.read_text(encoding='utf-8', errors='replace')
        report['vulkan_evidence'] = [line for line in log.splitlines() if 'offloaded ' in line or 'CLIP using Vulkan' in line]
        assert report['vulkan_evidence']
        for row in manifest['samples'] + manifest.get('sidecars', []):
            assert sha256(Path(row['path'])) == row['sha256']
            if row.get('original'):
                assert sha256(Path(row['original'])) == row['sha256']
        report['originals_unchanged'] = True
        report['passed'] = True
    except Exception as exc:
        report.update(error=str(exc), traceback=traceback.format_exc())
    finally:
        if engine:
            engine.close()
        report['elapsed'] = time.time() - report['started']
        save()
    return 0 if report['passed'] else 1
