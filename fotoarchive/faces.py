"""Local face detection and matching with separate DirectML embeddings.

YuNet (MIT) and SFace (Apache-2.0), OpenCV Zoo. Preprocessing follows the
documented OpenCV FaceDetectorYN / FaceRecognizerSF input conventions.
"""
from pathlib import Path
import json
import numpy as np
import cv2
from PIL import Image
from .inference import GPUUnavailable
from .media import open_rgb


def similarity_transform(points):
    source = np.asarray(points, np.float64).reshape(5, 2)
    target = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                       [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float64)
    a, b = source - source.mean(0), target - target.mean(0)
    u, singular, vt = np.linalg.svd(b.T @ a / 5)
    signs = np.array([1., 1. if np.linalg.det(u @ vt) >= 0 else -1.])
    rotation = u @ np.diag(signs) @ vt
    scale = float(singular @ signs) / max(float((a * a).sum() / 5), 1e-10)
    linear = scale * rotation
    return np.column_stack([linear, target.mean(0) - linear @ source.mean(0)]).astype(np.float32)


def nms(boxes, scores, threshold=.3):
    order = np.argsort(scores)[::-1][:5000]
    keep = []
    while len(order):
        current = int(order[0]); keep.append(current)
        rest = order[1:]
        if not len(rest):
            break
        intersection = np.maximum(0, np.minimum(boxes[current, 2:], boxes[rest, 2:]) - np.maximum(boxes[current, :2], boxes[rest, :2])).prod(1)
        areas = np.maximum(0, boxes[:, 2:] - boxes[:, :2]).prod(1)
        overlap = intersection / np.maximum(areas[current] + areas[rest] - intersection, 1e-9)
        order = rest[overlap <= threshold]
    return keep


class FaceModels:
    def __init__(self, cfg, profile=False, provider='DmlExecutionProvider'):
        import onnxruntime as ort
        if provider not in ort.get_available_providers():
            raise GPUUnavailable("Для анализа лиц необходим DirectML.")
        self.sessions = {}
        for name in ("yunet", "sface"):
            options = ort.SessionOptions()
            options.enable_mem_pattern = False
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            options.intra_op_num_threads = 2
            options.enable_profiling = profile
            options.log_severity_level = 3
            options.profile_file_prefix = str(cfg.data_dir / "reports" / ("face_" + name))
            if name == "yunet":
                options.add_free_dimension_override_by_name("height", 960)
                options.add_free_dimension_override_by_name("width", 960)
            gpu_options = {"device_id": cfg.gpu_device}
            if provider == 'CUDAExecutionProvider':
                gpu_options.update(use_tf32=0, cudnn_conv_algo_search='HEURISTIC')
            session = ort.InferenceSession(str(cfg.data_dir / "models/faces" / (name + ".onnx")), sess_options=options,
                providers=[(provider, gpu_options), "CPUExecutionProvider"])
            session.disable_fallback()
            if session.get_providers()[0] != provider:
                raise GPUUnavailable("Модель лиц не запущена на видеокарте.")
            self.sessions[name] = session

    def detect(self, rgb):
        height, width = rgb.shape[:2]
        predictions = []
        for side in (960, 320):
            factor = side / max(height, width)
            w, h = max(1, round(width * factor)), max(1, round(height * factor))
            prepared = np.zeros((960, 960, 3), dtype=np.uint8)
            prepared[:h, :w] = cv2.resize(rgb[:, :, ::-1], (w, h), interpolation=cv2.INTER_AREA if factor < 1 else cv2.INTER_LINEAR)
            session = self.sessions["yunet"]
            output = dict(zip([s.name for s in session.get_outputs()], session.run(None, {"input": prepared.transpose(2, 0, 1)[None].astype(np.float32)})))
            for stride in (8, 16, 32):
                score = np.sqrt(np.clip(output[f"cls_{stride}"].reshape(-1), 0, 1) * np.clip(output[f"obj_{stride}"].reshape(-1), 0, 1))
                chosen = np.flatnonzero(score >= .8)
                if not len(chosen):
                    continue
                grid = np.column_stack([chosen % (960 // stride), chosen // (960 // stride)])
                bbox = output[f"bbox_{stride}"].reshape(-1, 4)[chosen]
                center = (grid + bbox[:, :2]) * stride
                size = np.exp(np.clip(bbox[:, 2:], -20, 20)) * stride
                boxes = np.column_stack([center - size / 2, center + size / 2]) / factor
                landmarks = (output[f"kps_{stride}"].reshape(-1, 5, 2)[chosen] + grid[:, None]) * stride / factor
                for box, points, confidence in zip(boxes, landmarks, score[chosen]):
                    box[[0, 2]] = np.clip(box[[0, 2]], 0, width)
                    box[[1, 3]] = np.clip(box[[1, 3]], 0, height)
                    if min(box[2:] - box[:2]) >= 16 and np.isfinite(points).all():
                        predictions.append((box, points, float(confidence)))
        if not predictions:
            return []
        keep = nms(np.array([p[0] for p in predictions]), np.array([p[2] for p in predictions]))
        return [predictions[i] for i in keep]

    def process(self, path):
        return self.process_pil(open_rgb(Path(path)))

    def process_pil(self, image):
        rgb = np.asarray(image)
        height, width = rgb.shape[:2]
        records = []
        for box, landmarks, confidence in self.detect(rgb):
            aligned = cv2.warpAffine(rgb, similarity_transform(landmarks), (112, 112), flags=cv2.INTER_LINEAR)
            vector = self.sessions["sface"].run(None, {"data": aligned.transpose(2, 0, 1)[None].astype(np.float32)})[0].reshape(-1)
            if not np.isfinite(vector).all() or np.linalg.norm(vector) < 1e-8:
                continue
            vector = (vector / np.linalg.norm(vector)).astype(np.float32)
            assert len(vector) == 128
            records.append({"box": (box / [width, height, width, height]).tolist(), "landmarks": (landmarks / [width, height]).tolist(),
                            "confidence": confidence, "vector": vector, "portrait": Image.fromarray(aligned)})
        return records

    def finish_profile(self):
        report = {}
        for name, session in self.sessions.items():
            path = session.end_profiling()
            counts = {}
            for event in json.loads(Path(path).read_text()):
                provider = event.get("args", {}).get("provider")
                if provider:
                    counts[provider] = counts.get(provider, 0) + 1
            report[name] = {"path": path, "providers": counts}
        return report
