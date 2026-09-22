"""Read-only SigLIP DirectML shape/batch probe; never changes the catalogue."""
import gc
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import onnxruntime as ort
from PIL import Image
from fotoarchive.config import Settings
from fotoarchive.media import open_rgb


def main():
    cfg = Settings.load()
    processor = json.loads((cfg.data_dir / 'models/siglip/preprocessor_config.json').read_text())
    with sqlite3.connect(f'file:{(cfg.data_dir / "catalog.sqlite3").as_posix()}?mode=ro', uri=True) as db:
        paths = [Path(r[0]) for r in db.execute(
            'SELECT path FROM assets WHERE metadata_ready=1 AND present=1 ORDER BY id LIMIT 16')]
    images = []
    for path in paths:
        image = open_rgb(path).resize((224, 224), Image.Resampling(processor['resample']))
        array = np.asarray(image, np.float32) / 255
        array = (array - np.array(processor['image_mean'], np.float32)) / np.array(processor['image_std'], np.float32)
        images.append(array.transpose(2, 0, 1))
    inputs = np.stack(images)
    baseline = None
    report = {'concurrent_app_may_be_running': True, 'samples': len(paths), 'modes': []}
    output = cfg.data_dir / 'reports/v052-siglip-batches.json'
    for batch, fixed in ((1, False), (1, True), (4, True), (8, True)):
        options = ort.SessionOptions()
        options.enable_mem_pattern = False
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.intra_op_num_threads = 4
        options.log_severity_level = 3
        if fixed:
            for name, value in {'batch_size': batch, 'num_channels': 3, 'height': 224, 'width': 224}.items():
                options.add_free_dimension_override_by_name(name, value)
        session = ort.InferenceSession(str(cfg.data_dir / 'models/siglip/onnx/vision_model_fp16.onnx'),
            sess_options=options, providers=[('DmlExecutionProvider', {'device_id': cfg.gpu_device}), 'CPUExecutionProvider'])
        session.disable_fallback()
        assert session.get_providers()[0] == 'DmlExecutionProvider'
        name = 'pooler_output'
        session.run([name], {'pixel_values': inputs[:batch]})
        times = []
        for repeat in range(5):
            started = time.perf_counter()
            vectors = np.concatenate([session.run([name], {'pixel_values': inputs[i:i+batch]})[0]
                                      for i in range(0, len(inputs), batch)])
            times.append(time.perf_counter()-started)
        vectors = vectors.astype(np.float32)
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        if baseline is None:
            baseline = vectors.copy()
        result = {'batch': batch, 'fixed_shapes': fixed, 'seconds_for_16': times,
                  'median_photos_per_second': len(inputs)/float(np.median(times)),
                  'min_cosine_to_baseline': float(np.min(np.sum(baseline*vectors, axis=1))),
                  'providers': session.get_providers()}
        report['modes'].append(result)
        output.write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(result), flush=True)
        del session
        gc.collect()


if __name__ == '__main__':
    main()
