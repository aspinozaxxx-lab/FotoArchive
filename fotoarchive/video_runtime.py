"""Killable video GPU owners. No model can outlive the application/lease."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from .inference import GPUUnavailable


class VideoOwner:
    def __init__(self, cfg, cuda=False):
        self.cfg, self.cuda = cfg, cuda
        self.process = self.job = self.log = None
        self.responses = None
        self.model = None
        self.lock = threading.RLock()

    def start(self, model):
        if self.process and self.process.poll() is None and self.model == model:
            return
        self.close()
        env = os.environ.copy()
        env.update(PYTHONIOENCODING='utf-8',PYTHONUTF8='1')
        if self.cuda:
            root = self.cfg.data_dir/'runtime/video-torch'
            executable = root/('omni' if model == 'omni' else 'internvideo')/'bin/python'
            if not executable.exists():
                raise GPUUnavailable('Не установлено серверное окружение модели видео')
            command = [sys.executable, '-m', 'fotoarchive.remote_exec', str(executable), '-u', '-m', 'fotoarchive.video_torch']
            env.update(FOTOARCHIVE_VIDEO_MODELS=str(self.cfg.data_dir/'models/video-torch'),
                       HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false')
        else:
            command = ([sys.executable, '--video-worker', '--data-dir', str(self.cfg.data_dir)]
                       if getattr(sys, 'frozen', False) else
                       [sys.executable, '-u', '-m', 'fotoarchive.video_worker', str(self.cfg.data_dir)])
        self.log = (self.cfg.data_dir/'logs'/('video-cuda.log' if self.cuda else 'video-local.log')).open('a', encoding='utf-8')
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
            text=True, encoding='utf-8', bufsize=1, env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if os.name == 'nt':
            from .windows_job import ModelJob
            try:
                self.job = ModelJob(self.process)
            except Exception:
                self.close()
                raise
        self.model, self.responses = model, queue.Queue(maxsize=2)
        process, responses, log = self.process, self.responses, self.log
        def read():
            try:
                for line in process.stdout:
                    if line.startswith('RESULT\t'):
                        responses.put(json.loads(line[7:]))
                    else:
                        log.write(line[:4096]); log.flush()
            finally:
                try: responses.put_nowait(dict(error='Процесс видео остановился'))
                except queue.Full: pass
                process.stdout.close()
                log.close()
        threading.Thread(target=read, name='video-output', daemon=True).start()

    def run(self, request, cancelled=lambda: False):
        with self.lock:
            if cancelled():
                raise InterruptedError('Обработка видео отменена')
            self.start(request['model'])
            process, responses = self.process, self.responses
            process.stdin.write(json.dumps(request, ensure_ascii=False)+'\n')
            process.stdin.flush()
        deadline = time.monotonic()+1200
        while time.monotonic() < deadline:
            if cancelled():
                self.close()
                raise InterruptedError('Обработка видео отменена')
            try:
                result = responses.get(timeout=.2)
                if result.get('error'):
                    raise ValueError(result['error'])
                return result
            except queue.Empty:
                if self.process is not process or process.poll() is not None:
                    raise InterruptedError('Обработка видео остановлена')
        self.close()
        raise TimeoutError('Истекло время обработки отрезка видео')

    def close(self):
        with self.lock:
            self._close()

    def _close(self):
        # Never acquire an inference lock here: pause/close must interrupt an
        # active GPU call immediately, not wait for its 20-minute deadline.
        process, job, log = self.process, self.job, self.log
        self.process = self.job = self.log = None
        if job:
            job.close()
        if process:
            if process.poll() is None:
                process.kill()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: pass
            if process.stdin: process.stdin.close()
        # stdout reader owns the remaining read until the process exits.
        if log:
            try: log.flush()
            except ValueError: pass
