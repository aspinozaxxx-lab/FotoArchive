"""Download pinned inference artifacts. Never reads the photo archive."""
import os
os.environ["HF_HUB_DISABLE_XET"] = "1"
import truststore
truststore.inject_into_ssl()
import hashlib
import json
import sys
import zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
from huggingface_hub import hf_hub_download
from .config import Settings, SIG_REPO, SIG_REV, QWEN_REPO, QWEN_REV, LLAMA_RELEASE


def main(cfg=None, progress=None):
    cfg = cfg or Settings.load()
    progress = progress or (lambda message: print(message, flush=True))
    cfg.initialize()
    manifest_path = cfg.data_dir / "models/manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    for repo, rev, folder, files in [
        (SIG_REPO, SIG_REV, "siglip", ["config.json", "preprocessor_config.json", "tokenizer_config.json", "tokenizer.json", "special_tokens_map.json", "onnx/vision_model_fp16.onnx", "onnx/text_model_fp16.onnx"]),
        (QWEN_REPO, QWEN_REV, "qwen", ["Qwen3VL-4B-Instruct-Q4_K_M.gguf", "mmproj-Qwen3VL-4B-Instruct-F16.gguf"]),
    ]:
        for name in files:
            progress(f"Загрузка {folder}/{name}")
            path = Path(hf_hub_download(repo, name, revision=rev, local_dir=cfg.data_dir / "models" / folder))
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            manifest[f"{folder}/{name}"] = {"repo": repo, "revision": rev, "bytes": path.stat().st_size, "sha256": digest}
    asset = f"llama-{LLAMA_RELEASE}-bin-win-vulkan-x64.zip"
    target = cfg.data_dir / "runtime" / asset
    if not target.exists():
        progress(f"Загрузка {asset}")
        with requests.get(f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_RELEASE}/{asset}", stream=True, timeout=90) as response:
            response.raise_for_status()
            with target.with_suffix(".partial").open("wb") as out:
                for chunk in response.iter_content(1024 * 1024):
                    out.write(chunk)
        target.with_suffix(".partial").replace(target)
    runtime = cfg.data_dir / "runtime" / LLAMA_RELEASE
    runtime.mkdir(exist_ok=True)
    with zipfile.ZipFile(target) as archive:
        for member in archive.infolist():
            destination = (runtime / member.filename).resolve()
            if not destination.is_relative_to(runtime.resolve()):
                raise ValueError("Unsafe archive path")
        archive.extractall(runtime)
    manifest["llama.cpp"] = {"release": LLAMA_RELEASE, "archive_sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
    (cfg.data_dir / "models" / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    cfg.save()
    from .extra_models import setup
    setup(cfg, progress)
    progress("Модели и локальный модуль готовы")


if __name__ == "__main__":
    main()
