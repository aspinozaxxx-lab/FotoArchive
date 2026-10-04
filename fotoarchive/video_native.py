"""AMD/Vulkan MiniCPM-o, isolated behind the pinned native streaming API."""
import json
from pathlib import Path
import re
import secrets
import socket
import subprocess
import tempfile
import time
import wave

import numpy as np
import requests

from .config import VIDEO_RUNTIME_REV
from .inference import GPUUnavailable
from .video_caption import prompt, validate_caption


class NativeOmni:
    def __init__(self, cfg):
        self.cfg, self.process, self.job, self.log = cfg, None, None, None
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers['Authorization'] = 'Bearer '+secrets.token_urlsafe(32)
        self.base = ''

    def available(self):
        folder = self.cfg.data_dir/'models/video-omni'
        return (any((self.cfg.data_dir/'runtime/video-omni').rglob('llama-server.exe')) and
                all((folder/file).is_file() for file in ('MiniCPM-o-4_5-Q4_K_M.gguf',
                     'vision/MiniCPM-o-4_5-vision-F16.gguf', 'audio/MiniCPM-o-4_5-audio-F16.gguf')))

    def post(self, endpoint, body, timeout=180):
        response = self.session.post(self.base+'/v1/stream/'+endpoint, json=body, timeout=(3, timeout))
        response.raise_for_status()
        return response.json()

    def start(self):
        if self.process and self.process.poll() is None:
            return
        if not self.available():
            raise GPUUnavailable('Модель видео MiniCPM-o или её модуль Vulkan ещё не установлены')
        self.close()
        folder = self.cfg.data_dir/'models/video-omni'
        executable = next((self.cfg.data_dir/'runtime/video-omni').rglob('llama-server.exe'))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.base = f'http://127.0.0.1:{port}'
        self.log_path = self.cfg.data_dir/'logs/video-vulkan.log'
        self.log = self.log_path.open('w', encoding='utf-8')
        self.process = subprocess.Popen([str(executable), '-m', str(folder/'MiniCPM-o-4_5-Q4_K_M.gguf'),
            '--host', '127.0.0.1', '--port', str(port), '--api-key', self.session.headers['Authorization'][7:],
            '-ngl', '99', '-c', '8192', '--parallel', '1', '--threads', '6', '--temp', '0',
            '--repeat-penalty','1.12','--repeat-last-n','256', '-lv', '4','--cache-ram','0'],
            stdout=self.log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        from .windows_job import ModelJob
        try:
            self.job = ModelJob(self.process)
            deadline = time.monotonic()+240
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise GPUUnavailable('Модуль видео Vulkan остановился; см. '+str(self.log_path))
                try:
                    if self.session.get(self.base+'/health', timeout=1).ok:
                        break
                except requests.RequestException:
                    pass
                time.sleep(.2)
            else:
                raise TimeoutError('Истекло время запуска MiniCPM-o')
            self.post('omni_init', dict(media_type=2, use_tts=False, duplex_mode=False,
                model_dir=str(folder), output_dir=str(self.cfg.data_dir/'previews'), n_predict=1000,
                voice_clone_prompt='<|im_start|>system\nYou are a precise audiovisual analyst. Describe only evidence, without repetition.\n<|audio_start|>',
                assistant_prompt='<|audio_end|>Do not imitate voices. Answer the video analysis request factually.<|im_end|>\n<|im_start|>user\n'))
            self.proof()
        except Exception:
            self.close()
            raise

    def proof(self):
        log = self.log_path.read_text(encoding='utf-8', errors='replace')
        layers = re.search(r'offloaded (\d+)/(\d+) layers to GPU', log)
        if (not layers or layers[1] != layers[2] or int(layers[1]) == 0 or
                'vision using Vulkan' not in log or 'FotoArchive audio backend: Vulkan' not in log):
            raise GPUUnavailable('Не подтверждён Vulkan для всех слоёв, зрения и аудио MiniCPM-o')
        return dict(language_layers=layers[0], vision='Vulkan', audio='Vulkan', runtime=VIDEO_RUNTIME_REV,
                    audio_compute_calls=log.count('FotoArchive audio computed: Vulkan'))

    def describe(self, data, images, bindings):
        self.start()
        before_audio=self.proof()['audio_compute_calls']
        self.post('reset', {})
        with tempfile.TemporaryDirectory(prefix='video-av-', dir=self.cfg.data_dir/'previews') as temporary:
            folder = Path(temporary)
            instruction = prompt(data, bindings,narrative=True)
            # cnt=0 is the native system/voice prefill, not the first video
            # frame. Never consume the user's first second as reference audio.
            reference = folder/'reference.wav'
            with wave.open(str(reference), 'wb') as stream:
                stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
                stream.writeframes(bytes(32000))
            self.post('prefill', dict(cnt=0, audio_path_prefix=str(reference), text=' '))
            # This pinned native API handles either media or text per prefill.
            # Text supplied alongside an image is ignored by its LLM thread.
            self.post('prefill', dict(cnt=1, text=instruction))
            for number, image in enumerate(images):
                picture = folder/f'{number}.png'
                image.save(picture, format='PNG')
                audio_path = ''
                if data.has_audio:
                    audio = folder/f'{number}.wav'
                    with wave.open(str(audio), 'wb') as stream:
                        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
                        stream.writeframes((np.clip(data.audio_part(number), -1, 1)*32767).astype('<i2').tobytes())
                    audio_path = str(audio)
                timestamp = data.metadata['requested_times_ms'][number]
                self.post('prefill', dict(cnt=2+number*2, text=f'Frame {number+1}, t={timestamp/1000:.1f}s:'))
                self.post('prefill', dict(cnt=3+number*2, img_path_prefix=str(picture), audio_path_prefix=audio_path,
                    max_slice_nums=1))
            self.post('prefill', dict(cnt=2+len(images)*2,
                text='Describe this video now in Russian. Write only the requested factual narrative.'))
            parts, done = [], False
            with self.session.post(self.base+'/v1/stream/decode', json=dict(stream=True, debug_dir=str(folder), round_idx=0),
                                   stream=True, timeout=(3, 600)) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith(b'data:'):
                        continue
                    value = line[5:].strip()
                    if value == b'[DONE]':
                        done = True
                        break
                    record = json.loads(value)
                    parts.append(record.get('content', ''))
                    if sum(map(len, parts)) > 24000:
                        raise ValueError('Слишком длинный ответ модели видео')
            if not done:
                raise ValueError('Ответ модели видео оборвался')
        self.proof()  # A real multimodal call must have completed before it is saved.
        if self.proof()['audio_compute_calls']-before_audio < (len(images)+1 if data.has_audio else 1):
            raise GPUUnavailable('Не подтверждено вычисление всей аудиодорожки на Vulkan')
        narrative=''.join(parts).strip()
        if not narrative:
            raise ValueError('Модель вернула пустое описание видео')
        return dict(narrative=narrative)

    def close(self):
        if self.job:
            self.job.close(); self.job = None
        if self.process:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=5)
            self.process = None
        if self.log:
            self.log.close(); self.log = None
