"""Compare old and new embedding pipelines on identical copies, with real DirectML."""
import gc
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fotoarchive.config import Settings
from fotoarchive.engine import Engine
from fotoarchive.inference import Embedder
from fotoarchive.media import sha256


class OriginalEmbedder(Embedder):
    """The 0.5.1 dynamic-shape, one-image path, kept only for this comparison."""
    IMAGE_BATCH = 1

    def _session(self, tower):
        if tower not in self.sessions:
            options = self.ort.SessionOptions()
            options.enable_mem_pattern = False
            options.execution_mode = self.ort.ExecutionMode.ORT_SEQUENTIAL
            options.intra_op_num_threads = 4
            session = self.ort.InferenceSession(str(self.cfg.data_dir / 'models/siglip/onnx' / f'{tower}_model_fp16.onnx'),
                sess_options=options, providers=[('DmlExecutionProvider', {'device_id': self.cfg.gpu_device}), 'CPUExecutionProvider'])
            session.disable_fallback()
            assert session.get_providers()[0] == 'DmlExecutionProvider'
            self.sessions[tower] = session
        return self.sessions[tower]


def main():
    base = Settings.load()
    folder = Path(tempfile.mkdtemp(prefix='v052-pipeline-', dir=base.data_dir / 'reports'))
    source = folder / 'source'
    source.mkdir()
    with sqlite3.connect(f'file:{(base.data_dir / "catalog.sqlite3").as_posix()}?mode=ro', uri=True) as db:
        samples = [Path(r[0]) for r in db.execute("SELECT path FROM assets WHERE present=1 AND metadata_ready=1 AND width*height>=5000000 AND extension='.jpg' ORDER BY id LIMIT 8")]
    hashes = {str(path):sha256(path) for path in samples}
    paths = []
    for i in range(128):
        target = source / f'{i:04d}.jpg'
        if i < len(samples):
            shutil.copyfile(samples[i], target)
        else:
            os.link(paths[i % len(samples)], target)  # links among copies only
        paths.append(target)
    report = {'passed':False, 'folder':str(folder), 'samples':128, 'modes':{},
              'scope':'read, decode, preprocess, DirectML embedding, SQLite and LanceDB writes on identical SSD copies'}
    vectors = {}
    for mode in ('original', 'pipeline'):
        cfg = Settings(data_dir=folder / mode, root=source, includes=['.'])
        cfg.initialize()
        for src in (base.data_dir / 'models/siglip').rglob('*'):
            if src.is_file():
                dst = cfg.data_dir / 'models/siglip' / src.relative_to(base.data_dir / 'models/siglip')
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.link(src, dst)  # inference only; model files are not edited
        engine = Engine(cfg)
        try:
            for path in paths:
                asset_id, _ = engine.catalog.register(path)
                engine.process_one(('metadata',), asset_id=asset_id)
            engine.embedder = OriginalEmbedder(cfg) if mode == 'original' else Embedder(cfg, profile=True)
            engine.embedder.image(paths[0])
            if mode == 'original':
                engine.index.start_flush = lambda limit=64: bool(engine.index.flush(limit=64))
            started = time.perf_counter()
            completed, high_water = 0, 0
            while completed < len(paths):
                if mode == 'original':
                    result = engine.process_one(('embedding',))
                else:
                    engine.index.collect_flush()
                    engine.pipeline.inputs.fill()
                    high_water = max(high_water, len(engine.pipeline.inputs.pending))
                    result = engine.process_embedding_batch()
                if result:
                    completed += result.get('batch_count', 1)
                else:
                    time.sleep(.001)
                assert time.perf_counter()-started < 180
            engine.index.flush_all()
            elapsed = time.perf_counter()-started
            assert engine.catalog.stats()['embeddings'] == len(paths)
            assert engine.catalog.stats()['errors'] == 0
            assert engine.index.table.count_rows() == len(paths)
            vectors[mode] = [engine.catalog.vector(i) for i in range(1, len(paths)+1)]
            stats = {'seconds':elapsed, 'photos_per_second':len(paths)/elapsed, 'max_input_queue':high_water}
            if mode == 'pipeline':
                stats['gpu_profile'] = engine.embedder.finish_profile()
                assert stats['gpu_profile']['provider_events'].get('DmlExecutionProvider', 0) > 0
                query = engine.embedder.text('люди у реки')
                assert query.shape == (768,)
            report['modes'][mode] = stats
            print(mode, json.dumps(stats), flush=True)
        finally:
            engine.close()
            del engine
            gc.collect()
    import numpy as np
    report['min_cosine_to_original'] = float(np.min(np.sum(np.asarray(vectors['original'])*np.asarray(vectors['pipeline']), axis=1)))
    report['speedup'] = report['modes']['original']['seconds']/report['modes']['pipeline']['seconds']
    report['original_hashes_unchanged'] = all(sha256(Path(p)) == h for p,h in hashes.items())
    report['passed'] = report['original_hashes_unchanged'] and report['min_cosine_to_original'] > .999
    (base.data_dir / 'reports/v052-pipeline.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    main()
