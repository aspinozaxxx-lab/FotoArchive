"""Disposable Linux GPU owner. Unpacked photographs only exist in RAM."""
import base64
import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from PIL import Image, ImageOps
from .config import Settings, LLAMA_RELEASE
from .inference import VisionLanguage, Embedder, GPUUnavailable
from .faces import FaceModels
from .remote_protocol import unpack
from .remote_exec import die_with_parent


class CudaVision(VisionLanguage):
    def _start(self):
        if self.process and self.process.poll() is None:
            return
        self.close()
        executables = list((self.cfg.data_dir/'runtime'/LLAMA_RELEASE).rglob('llama-server'))
        if not executables:
            raise GPUUnavailable('CUDA llama-server is not installed')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0))
            port = sock.getsockname()[1]
        self.base_url = f'http://127.0.0.1:{port}'
        folder = self.cfg.data_dir/'models/qwen'
        self.log_path = self.cfg.data_dir/'logs/llama-cuda.log'
        self.log_handle = self.log_path.open('w')
        self.process = subprocess.Popen([sys.executable,'-m','fotoarchive.remote_exec',str(executables[0]),'-m',str(folder/'Qwen3VL-4B-Instruct-Q4_K_M.gguf'),
            '--mmproj',str(folder/'mmproj-Qwen3VL-4B-Instruct-F16.gguf'),'--host','127.0.0.1','--port',str(port),
            '--api-key',self.token,'-ngl','99','-c','8192','--parallel','1','--threads','6','--flash-attn','on',
            '--jinja','-lv','4'],stdout=self.log_handle,stderr=subprocess.STDOUT)
        deadline = time.monotonic()+180
        while time.monotonic()<deadline:
            if self.process.poll() is not None:
                raise GPUUnavailable('CUDA vision process stopped')
            try:
                if self.session.get(self.base_url+'/health',timeout=1).ok:
                    log = self.log_path.read_text(errors='replace')
                    layers = re.search(r'offloaded (\d+)/(\d+) layers to GPU',log)
                    if not layers or int(layers[1])==0 or layers[1]!=layers[2] or not re.search(r'CLIP using CUDA',log):
                        self.close()
                        raise GPUUnavailable('CUDA offload of vision and language models was not confirmed')
                    return
            except __import__('requests').RequestException:
                pass
            time.sleep(.25)
        self.close()
        raise TimeoutError('CUDA model startup timed out')


def main():
    die_with_parent()
    import onnxruntime as ort
    ort.preload_dlls(directory='')
    cfg = Settings(data_dir=Path(os.environ['FOTOARCHIVE_REMOTE_DATA']))
    cfg.initialize()
    embedder = faces = vision = None
    profiled = set()

    def proof(name,session):
        if name in profiled:
            return None
        path=session.end_profiling()
        events=json.loads(Path(path).read_text())
        counts={}
        for event in events:
            provider=event.get('args',{}).get('provider')
            if provider:
                counts[provider]=counts.get(provider,0)+1
        math_nodes = sum(1 for event in events if event.get('args',{}).get('provider')=='CUDAExecutionProvider'
                         and event['args'].get('op_name','').startswith(('Conv','FusedConv','MatMul','FusedMatMul','Gemm','Attention')))
        if not counts.get('CUDAExecutionProvider') or not math_nodes:
            raise GPUUnavailable(f'{name}: CUDA kernel execution was not confirmed')
        counts['CUDA_math_nodes'] = math_nodes
        profiled.add(name)
        return counts
    for line in sys.stdin:
        job = json.loads(line)
        started = time.monotonic()
        output = dict(id=job['id'],stages={},errors={},providers={})
        try:
            raw = unpack(Path(job['blob']).read_bytes())
            with Image.open(io.BytesIO(raw)) as source:
                source.load()
                image = ImageOps.exif_transpose(source).convert('RGB')
            del raw
            for stage in job['stages']:
                try:
                    if stage=='embedding':
                        embedder = embedder or Embedder(cfg,profile=True,provider='CUDAExecutionProvider')
                        output['stages'][stage] = embedder.prepared_images([embedder.prepare_pil(image)])[0].tolist()
                        output['providers'][stage] = embedder._session('vision').get_providers()[0]
                        evidence=proof('siglip',embedder._session('vision'))
                        if evidence:
                            output.setdefault('gpu_proof',{})['siglip']=evidence
                    elif stage=='faces':
                        faces = faces or FaceModels(cfg,profile=True,provider='CUDAExecutionProvider')
                        records = faces.process_pil(image)
                        for record in records:
                            stream = io.BytesIO()
                            record['portrait'].save(stream,format='PNG')
                            record['portrait'] = base64.b64encode(stream.getvalue()).decode()
                            record['vector'] = record['vector'].tolist()
                        output['stages'][stage] = records
                        output['providers'][stage] = {k:v.get_providers()[0] for k,v in faces.sessions.items()}
                        for name,session in faces.sessions.items():
                            if name=='yunet' or records:
                                evidence=proof(name,session)
                                if evidence:
                                    output.setdefault('gpu_proof',{})[name]=evidence
                    elif stage=='caption':
                        vision = vision or CudaVision(cfg)
                        preview = image.copy()
                        preview.thumbnail((1008,1008),Image.Resampling.LANCZOS)
                        stream = io.BytesIO()
                        preview.save(stream,format='JPEG',quality=90)
                        output['stages'][stage] = vision.describe(image_data=stream.getvalue())
                        output['providers'][stage] = 'CUDA (all layers + vision projector)'
                    elif stage=='location':
                        location = job['context']['location']
                        text = location['geo_text']
                        geo_vector = None
                        if text:
                            embedder = embedder or Embedder(cfg,profile=True,provider='CUDAExecutionProvider')
                            geo_vector = embedder.text(text).tolist()
                            evidence = proof('siglip-text',embedder._session('text'))
                            if evidence:
                                output.setdefault('gpu_proof',{})['siglip-text']=evidence
                        output['stages'][stage] = dict(location=location,vector=geo_vector)
                        output['providers'][stage] = 'CUDAExecutionProvider' if text else 'metadata'
                except Exception as exc:
                    output['stages'].pop(stage,None)
                    output['errors'][stage] = str(exc)[:500]
        except Exception as exc:
            output['errors'] = {stage:str(exc)[:500] for stage in job['stages']}
        output['elapsed'] = time.monotonic()-started
        print('RESULT\t'+json.dumps(output,ensure_ascii=False),flush=True)
        if vision and vision.log_handle and vision.log_path.stat().st_size>8*1024**2:
            vision.log_handle.seek(0)
            vision.log_handle.truncate()


if __name__=='__main__':
    main()
