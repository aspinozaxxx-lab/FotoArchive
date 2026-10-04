from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

SIG_REPO = "onnx-community/siglip2-base-patch16-224-ONNX"
SIG_REV = "ba1f3b0843f24bc5417d38e19c37b287d719b2f4"
QWEN_REPO = "Qwen/Qwen3-VL-4B-Instruct-GGUF"
QWEN_REV = "1cd86afb9a95c410a6038ab3b40d8b578c892266"
LLAMA_RELEASE = "b11009"
EMBED_VERSION = f"siglip2-base-224:{SIG_REV}:fp16:rgb-v1"
CAPTION_VERSION = f"qwen3vl-4b:{QWEN_REV}:Q4_K_M:caption-v1"
VERIFY_VERSION = f"qwen3vl-4b:{QWEN_REV}:Q4_K_M:verify-v1"
FACE_REV = "47534e27c9851bb1128ccc0102f1145e27f23f98"
FACE_VERSION = f"yunet-sface:{FACE_REV}:960+320:score080:min16:align112:v1"
GEO_VERSION = "gps-exif-xmp:geonames-cities1000:v1"
RAW_FORMATS = {'.3fr', '.arw', '.cr2', '.cr3', '.crw', '.dng', '.erf', '.kdc', '.mos',
               '.mrw', '.nef', '.nrw', '.orf', '.pef', '.raf', '.raw', '.rw2', '.srw', '.x3f'}
IMAGE_FORMATS = {'.jpg', '.jpeg', '.bmp', '.png', '.gif', '.tif', '.tiff', '.heic', '.heif',
                 '.webp', '.avif', '.psd', '.psb', '.mpo', '.jxl', '.jp2', '.j2k', '.ico',
                 '.pcx', '.tga', '.ppm', '.pgm', '.pbm', '.pnm', '.dds', '.cpt'}
VIDEO_FORMATS = {'.mp4', '.mov', '.avi', '.mts', '.m2ts', '.mkv', '.wmv', '.mpg', '.mpeg',
                 '.3gp', '.3g2', '.vob', '.flv', '.m4v', '.webm', '.tod', '.mod'}
SUPPORTED = IMAGE_FORMATS | RAW_FORMATS | VIDEO_FORMATS
MEDIA_VERSION = 'media-v2:raw-heif-av:video-step10s'
VIDEO_INTERVAL_MS = 10000
VIDEO_SEGMENT_MS = 30_000
VIDEO_SAMPLE_MS = 1000
VIDEO_PIXEL_BUDGET = 64_000_000
VIDEO_OMNI_REPO = 'openbmb/MiniCPM-o-4_5'
VIDEO_OMNI_REV = '503e754207c94da6bb26850b4469f367c9ea3582'
VIDEO_INTERN_REPO = 'OpenGVLab/InternVideo2_5_Chat_8B'
VIDEO_INTERN_REV = '87680fe232af8681b55322f68541679b91f99df1'
VIDEO_RUNTIME_REV = '61d8393fdddff58e8f831ccfdf8a80ff989ebf9c'
VIDEO_GGUF_REV = 'db25077c33951fe163b42986fba0132e279872a2'
VIDEO_VERSION = f'video-av-v3:30s-max:1fps:lossless-original:mono16k:{VIDEO_OMNI_REV}:{VIDEO_GGUF_REV}:ru-{QWEN_REV}:people-tiles-v2:caption-vector-v1'


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("FOTOARCHIVE_DATA", r"D:\FotoArchiveData")))
    root: Path = field(default_factory=lambda: Path(r"F:\MyFoto"))
    includes: list[str] = field(default_factory=lambda: ["2003"])
    preview_budget: int = 20 * 1024**3
    gpu_device: int = 0
    ann_threshold: int = 10_000
    preparation_workers: int = 0
    remote_enabled: bool = False
    local_enabled: bool = True
    remote_host: str = 'gpu-mlserver'
    remote_port: int = 18765
    video_model: str = 'omni'

    def initialize(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for name in ("models/siglip", "models/qwen", "models/faces", "models/video-omni", "geonames", "faces", "runtime", "thumbnails", "previews", "logs", "reports", "vectors"):
            (self.data_dir / name).mkdir(parents=True, exist_ok=True)

    @classmethod
    def load(cls, data_dir=None):
        obj = cls()
        if data_dir:
            obj.data_dir = Path(data_dir)
        config = obj.data_dir / "settings.json"
        if config.exists():
            saved = json.loads(config.read_text(encoding="utf-8"))
            obj.root = Path(saved["root"])
            obj.includes = saved.get("includes", ["2003"])
            obj.preview_budget = int(saved.get("preview_budget", obj.preview_budget))
            obj.gpu_device = int(saved.get("gpu_device", 0))
            obj.preparation_workers = max(0, min(8, int(saved.get("preparation_workers", 0))))
            obj.remote_enabled = bool(saved.get('remote_enabled', False))
            obj.local_enabled = bool(saved.get('local_enabled', True))
            obj.remote_host = saved.get('remote_host', 'gpu-mlserver')
            obj.remote_port = int(saved.get('remote_port', 18765))
            obj.video_model = saved.get('video_model', 'omni')
            if obj.video_model not in ('omni', 'internvideo'):
                obj.video_model = 'omni'
        return obj

    def save(self):
        self.initialize()
        path = self.data_dir / "settings.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps({"root": str(self.root), "includes": self.includes,
                                   "preview_budget": self.preview_budget, "gpu_device": self.gpu_device,
                                   "preparation_workers": self.preparation_workers,
                                   "remote_enabled": self.remote_enabled, "remote_host": self.remote_host,
                                   "local_enabled": self.local_enabled,
                                   "remote_port": self.remote_port, "video_model": self.video_model},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
