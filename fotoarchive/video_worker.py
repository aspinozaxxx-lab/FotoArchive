"""Local video process: DirectML retrieval/faces + Vulkan audiovisual captions."""
import base64
import io
import json
from pathlib import Path
import sys

import numpy as np

from .config import Settings, VIDEO_OMNI_REV, VIDEO_GGUF_REV, QWEN_REV
from .faces import FaceModels
from .inference import Embedder, GPUUnavailable
from .remote_protocol import unpack
from .video_content import VideoInput
from .video_native import NativeOmni
from .video_people import label_people


def encode_face(face):
    stream = io.BytesIO()
    face['portrait'].save(stream, format='WEBP', quality=90)
    return face | dict(portrait=base64.b64encode(stream.getvalue()).decode(), vector=face['vector'].tolist())


def image_text(image):
    stream = io.BytesIO()
    copy = image.copy(); copy.thumbnail((320, 320))
    copy.save(stream, format='WEBP', quality=85)
    return base64.b64encode(stream.getvalue()).decode()


def gpu_proof(name, session, provider):
    events = json.loads(Path(session.end_profiling()).read_text())
    math = [e for e in events if e.get('args', {}).get('provider') == provider and
            e['args'].get('op_name', '').startswith(('Conv','FusedConv','MatMul','FusedMatMul','Gemm','Attention','DmlFusedNode'))]
    if not math:
        raise GPUUnavailable(name+': не подтверждено выполнение вычислений на GPU')
    return dict(provider=provider, math_nodes=len(math))


def analyze(data, embedder, faces, describe, people):
    times = data.metadata['requested_times_ms']
    batches = [faces.process_pil(image,detail=True) for image in data.images]
    labelled, bindings = label_people(data.images, batches, times, people)
    caption = describe(data, labelled, bindings, batches)
    vectors = []
    for start in range(0, len(data.images), 4):
        vectors.extend(embedder.prepared_images([embedder.prepare_pil(image) for image in data.images[start:start+4]]))
    vector = np.mean(vectors, axis=0)
    vector /= np.linalg.norm(vector)
    records = [dict(timestamp_ms=timestamp, vector=vec.tolist(), faces=[encode_face(face) for face in found],
                    thumbnail=image_text(image)) for timestamp, vec, found, image in zip(times, vectors, batches, data.images)]
    return dict(caption=caption, bindings=bindings, vector=vector.tolist(), frames=records,
                audio_used=data.has_audio, model='MiniCPM-o 4.5:'+VIDEO_OMNI_REV)


def main(data_dir=None):
    cfg = Settings.load(data_dir or sys.argv[1])
    cfg.initialize()
    native = NativeOmni(cfg)
    from .inference import VisionLanguage
    from .video_caption import needs_translation,structure_narrative,caption_vector
    formatter = VisionLanguage(cfg)
    embedder = faces = None
    proven = {}
    try:
        for line in sys.stdin:
            job = json.loads(line)
            try:
                if job['model'] != 'omni':
                    raise GPUUnavailable('InternVideo2.5 доступна на сервере; локально используется MiniCPM-o')
                data = VideoInput.decode(unpack(Path(job['blob']).read_bytes()))
                embedder = embedder or Embedder(cfg, profile=True)
                faces = faces or FaceModels(cfg, profile=True)
                def describe_video(data,images,bindings,batches):
                    caption=native.describe(data,images,bindings)
                    if 'narrative' in caption or needs_translation(caption):
                        native.close()
                        try:
                            caption=structure_narrative(formatter,caption.get('narrative') or json.dumps(caption,ensure_ascii=False),data.metadata,data.has_audio)
                        finally:formatter.close()
                    return caption
                result = analyze(data, embedder, faces, describe_video, job['people'])
                result['text_vector']=caption_vector(embedder,result['caption']).tolist()
                for name, session in [('siglip-video', embedder._session('vision')),('siglip-video-text',embedder._session('text')), *faces.sessions.items()]:
                    if name not in proven and (name != 'sface' or any(frame['faces'] for frame in result['frames'])):
                        proven[name] = gpu_proof(name, session, 'DmlExecutionProvider')
                result.update(model=f'MiniCPM-o 4.5 GGUF Q4_K_M:{VIDEO_GGUF_REV}',
                    formatter_model='Qwen3-VL-4B Q4_K_M:'+QWEN_REV,
                    backend='DirectML + Vulkan Q4_K_M', gpu_proof=proven | dict(omni=native.proof()))
                print('RESULT\t'+json.dumps(result, ensure_ascii=False), flush=True)
            except Exception as exc:
                import traceback
                with (cfg.data_dir/'logs/video-errors.log').open('a',encoding='utf-8') as log:
                    log.write(traceback.format_exc())
                print('RESULT\t'+json.dumps(dict(error=f'{type(exc).__name__}: {exc}'), ensure_ascii=True), flush=True)
    finally:
        native.close()
        formatter.close()


if __name__ == '__main__':
    main()
