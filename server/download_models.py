"""CPU/network-only provisioning of the exact Windows model revisions."""
import hashlib
import json
import os
from pathlib import Path
import tarfile
import requests
from huggingface_hub import hf_hub_download
from fotoarchive.config import SIG_REPO,SIG_REV,QWEN_REPO,QWEN_REV,FACE_REV,LLAMA_RELEASE

os.environ['HF_HUB_DISABLE_XET']='1'
root=Path('/data/fotoarchive')
manifest={}
for repo,rev,folder,files in [
    (SIG_REPO,SIG_REV,'siglip',['preprocessor_config.json','tokenizer_config.json','tokenizer.json','onnx/vision_model_fp16.onnx','onnx/text_model_fp16.onnx']),
    (QWEN_REPO,QWEN_REV,'qwen',['Qwen3VL-4B-Instruct-Q4_K_M.gguf','mmproj-Qwen3VL-4B-Instruct-F16.gguf'])]:
    for name in files:
        path=Path(hf_hub_download(repo,name,revision=rev,local_dir=root/'models'/folder))
        with path.open('rb') as stream:
            manifest[f'{folder}/{name}']=dict(revision=rev,sha256=hashlib.file_digest(stream,'sha256').hexdigest(),bytes=path.stat().st_size)

def download(url,path):
    path.parent.mkdir(parents=True,exist_ok=True)
    if not path.exists():
        with requests.get(url,stream=True,timeout=(15,120)) as response:
            response.raise_for_status()
            temp=path.with_suffix('.partial')
            with temp.open('wb') as stream:
                for chunk in response.iter_content(1024**2):
                    stream.write(chunk)
            temp.replace(path)

for folder,name,target in [('face_detection_yunet','face_detection_yunet_2026may.onnx','yunet.onnx'),
                           ('face_recognition_sface','face_recognition_sface_2021dec.onnx','sface.onnx')]:
    path=root/'models/faces'/target
    url=f'https://media.githubusercontent.com/media/opencv/opencv_zoo/{FACE_REV}/models/{folder}/{name}'
    download(url,path)
    manifest['faces/'+target]=dict(revision=FACE_REV,sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    download(f'https://raw.githubusercontent.com/opencv/opencv_zoo/{FACE_REV}/models/{folder}/LICENSE',path.with_suffix('.LICENSE.txt'))
for prefix in ('llama','cudart-llama'):
    name=f'{prefix}-{LLAMA_RELEASE}-bin-ubuntu-cuda-12.8-x64.tar.gz'
    path=root/'runtime'/name
    download(f'https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_RELEASE}/{name}',path)
    with path.open('rb') as stream:
        manifest[name]=dict(sha256=hashlib.file_digest(stream,'sha256').hexdigest())
    with tarfile.open(path) as archive:
        archive.extractall(root/'runtime'/LLAMA_RELEASE,filter='data')
(root/'models/manifest.json').write_text(json.dumps(manifest,indent=2))
print('Pinned models and CUDA runtime ready; GPU was not initialized.',flush=True)
