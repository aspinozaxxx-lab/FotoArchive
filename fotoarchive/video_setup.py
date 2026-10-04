"""Install the bundled Vulkan runtime; download pinned model weights on request."""
import hashlib
import json
from pathlib import Path
import tempfile
import zipfile

from filelock import FileLock
from .config import VIDEO_RUNTIME_REV,VIDEO_GGUF_REV

RUNTIME_SHA256='68c9cc5d6f64d29e8ebe16c19652675f3a31d38fafb4aa9b1cae22e21fe701be'
FILES=('MiniCPM-o-4_5-Q4_K_M.gguf','vision/MiniCPM-o-4_5-vision-F16.gguf','audio/MiniCPM-o-4_5-audio-F16.gguf')


def install_runtime(cfg):
    target=cfg.data_dir/'runtime/video-omni'
    stamp=target/'revision.txt'
    if stamp.is_file() and stamp.read_text().strip()==VIDEO_RUNTIME_REV and (target/'llama-server.exe').is_file():
        return
    archive=Path(__file__).with_name('assets')/'video-vulkan-v1.0.22.zip'
    if not archive.is_file():
        raise FileNotFoundError('В поставке отсутствует модуль обработки видео Vulkan')
    with FileLock(str(cfg.data_dir/'runtime/video-install.lock'),timeout=15):
        if hashlib.sha256(archive.read_bytes()).hexdigest()!=RUNTIME_SHA256:
            raise ValueError('Контрольная сумма модуля видео не совпала')
        with tempfile.TemporaryDirectory(prefix='video-install-',dir=cfg.data_dir/'runtime') as temporary:
            staging=Path(temporary)
            with zipfile.ZipFile(archive) as source:
                for member in source.infolist():
                    if not (staging/member.filename).resolve().is_relative_to(staging.resolve()):
                        raise ValueError('Некорректный путь в модуле видео')
                source.extractall(staging)
            target.mkdir(exist_ok=True)
            for file in staging.iterdir():
                file.replace(target/file.name)


def download_models(cfg,progress=print):
    import os
    os.environ.setdefault('HF_HUB_DISABLE_XET','1')
    from huggingface_hub import hf_hub_download
    import truststore
    truststore.inject_into_ssl()
    install_runtime(cfg)
    folder=cfg.data_dir/'models/video-omni'
    records=[]
    for name in FILES:
        progress('Загрузка видео/'+name)
        file=Path(hf_hub_download('openbmb/MiniCPM-o-4_5-gguf',name,revision=VIDEO_GGUF_REV,local_dir=folder))
        with file.open('rb') as source:digest=hashlib.file_digest(source,'sha256').hexdigest()
        records.append(dict(name=name,size=file.stat().st_size,sha256=digest))
    (folder/'manifest.json').write_text(json.dumps(dict(repo='openbmb/MiniCPM-o-4_5-gguf',revision=VIDEO_GGUF_REV,files=records),indent=2),encoding='utf-8')
    return records
