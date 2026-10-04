"""Bounded, synchronized video/audio inputs. Source media is opened read-only."""
from dataclasses import dataclass
from pathlib import Path
import io
import json
import math
import struct
import time

import numpy as np
from PIL import Image

from .config import VIDEO_SEGMENT_MS, VIDEO_SAMPLE_MS, VIDEO_PIXEL_BUDGET

MAGIC = b'FotoArchive-AV\x00\x01'
SAMPLE_RATE = 16000
MAX_FRAMES = VIDEO_SEGMENT_MS // VIDEO_SAMPLE_MS


@dataclass
class VideoInput:
    metadata: dict
    images: list
    audio: np.ndarray

    @property
    def has_audio(self):
        return bool(self.metadata.get('has_audio')) and self.metadata.get('audio_peak',1) > 1e-7

    def audio_part(self, index):
        start = index * SAMPLE_RATE
        return self.audio[start:min(start + SAMPLE_RATE, len(self.audio))]

    def encode(self):
        output = io.BytesIO()
        header = json.dumps(self.metadata, ensure_ascii=False).encode('utf-8')
        output.write(MAGIC)
        output.write(struct.pack('<I', len(header)))
        output.write(header)
        for image in self.images:
            stream = io.BytesIO()
            image.save(stream, format='PNG', compress_level=1)
            data = stream.getvalue()
            output.write(struct.pack('<I', len(data)))
            output.write(data)
        output.write(np.asarray(self.audio, dtype='<f4').tobytes())
        return output.getvalue()

    @classmethod
    def decode(cls, payload):
        from .remote_protocol import MAX_INPUT
        if len(payload) > MAX_INPUT or not payload.startswith(MAGIC):
            raise ValueError('Некорректный пакет видео')
        stream = io.BytesIO(payload[len(MAGIC):])
        def read_block(limit):
            size = stream.read(4)
            if len(size) != 4:
                raise ValueError('Оборванный пакет видео')
            length = struct.unpack('<I', size)[0]
            if length > limit:
                raise ValueError('Слишком большой блок видео')
            block = stream.read(length)
            if len(block) != length:
                raise ValueError('Оборванный блок видео')
            return block
        metadata = json.loads(read_block(128 * 1024))
        start, end = metadata['start_ms'], metadata['end_ms']
        count = metadata['frame_count']
        if (not isinstance(start, int) or not isinstance(end, int) or start < 0 or
                not 0 < end-start <= VIDEO_SEGMENT_MS or not isinstance(count, int) or
                not 0 < count <= MAX_FRAMES or len(metadata['frame_times_ms']) != count or
                len(metadata['requested_times_ms']) != count):
            raise ValueError('Некорректный диапазон видео')
        times = metadata['requested_times_ms']
        if times != list(range(start, end, VIDEO_SAMPLE_MS)):
            raise ValueError('Нарушена последовательность кадров')
        if any(not isinstance(t, int) or t < start or t > end + 1000 for t in metadata['frame_times_ms']):
            raise ValueError('Некорректные временные отметки видео')
        images, pixels = [], 0
        for _ in range(count):
            with Image.open(io.BytesIO(read_block(MAX_INPUT))) as image:
                pixels += image.width*image.height
                if image.format not in ('PNG','JPEG') or max(image.size) > 16384 or pixels > VIDEO_PIXEL_BUDGET:
                    raise ValueError('Некорректный кадр видео')
                images.append(image.convert('RGB'))
        samples = math.ceil((end-start) * SAMPLE_RATE / 1000)
        raw = stream.read()
        if len(raw) != samples * 4:
            raise ValueError('Некорректная аудиодорожка')
        audio = np.frombuffer(raw, dtype='<f4').copy()
        if not np.isfinite(audio).all() or np.max(np.abs(audio), initial=0) > 4:
            raise ValueError('Некорректные аудиоданные')
        return cls(metadata, images, audio)


def decode_segment(asset, cancelled=lambda: False):
    import av
    from .video import _stream
    path = Path(asset['path'])
    expected = asset['size'], asset['mtime_ns']
    before = path.stat()
    if (before.st_size, before.st_mtime_ns) != expected:
        raise ValueError('Видео изменилось перед обработкой')
    start, end = asset['video_start_ms'], asset['video_end_ms']
    if not 0 < end-start <= VIDEO_SEGMENT_MS:
        raise ValueError('Некорректная длительность отрезка')
    times = list(range(start, end, VIDEO_SAMPLE_MS))
    images, actual = [], []
    last_frame, last_timestamp = None, None
    deadline = time.monotonic() + 120
    def check():
        if cancelled():
            raise InterruptedError('Обработка видео отменена')
        if time.monotonic() > deadline:
            raise TimeoutError('Слишком долгое декодирование отрезка видео')
    with av.open(str(path), options={'protocol_whitelist': 'file,crypto,data'}) as container:
        stream = _stream(container)
        origin = float((stream.start_time or 0) * stream.time_base)
        if start:
            container.seek(int((origin+start/1000)/stream.time_base), stream=stream, backward=True)
        for frame in container.decode(stream):
            check()
            timestamp = round(((float(frame.time) if frame.time is not None else origin) - origin) * 1000)
            if timestamp < start-1000:
                continue
            if timestamp >= end:
                break
            last_frame,last_timestamp=frame,timestamp
            if timestamp < times[len(images)]:
                continue
            image = frame.to_image()
            rotation = frame.rotation or float(stream.metadata.get('rotate', 0))
            if rotation:
                image = image.rotate(rotation, expand=True)
            while len(images)<len(times) and timestamp>=times[len(images)]:
                images.append(image)
                actual.append(max(start,timestamp))
            if len(images) == len(times):
                break
    # Containers often round duration past the final frame. Hold only the last
    # available frame for a partial final second; do not invent a missing tail.
    if len(images)<len(times) and last_frame is not None and times[len(images)]-last_timestamp<=1000:
        image=last_frame.to_image()
        rotation=last_frame.rotation or float(stream.metadata.get('rotate',0))
        if rotation:
            image=image.rotate(rotation,expand=True)
        while len(images)<len(times) and times[len(images)]-last_timestamp<=1000:
            images.append(image)
            actual.append(max(start,last_timestamp))
    if len(images) != len(times):
        raise ValueError(f'Не удалось декодировать все кадры отрезка: {len(images)}/{len(times)}')
    samples = math.ceil((end-start) * SAMPLE_RATE/1000)
    audio = np.zeros(samples, dtype='<f4')
    covered = np.zeros(samples, dtype=bool)
    has_audio = False
    with av.open(str(path), options={'protocol_whitelist': 'file,crypto,data'}) as container:
        if container.streams.audio:
            has_audio = True
            stream = container.streams.audio[0]
            if start:
                container.seek(int((origin+start/1000)/stream.time_base), stream=stream, backward=True)
            resampler = av.AudioResampler(format='flt', layout='mono', rate=SAMPLE_RATE)
            cursor = 0
            def place(frame):
                nonlocal cursor
                values = frame.to_ndarray().reshape(-1).astype('<f4')
                offset = round((float(frame.time)-origin-start/1000)*SAMPLE_RATE) if frame.time is not None else cursor
                cursor = offset + len(values)
                lo, hi = max(0, offset), min(samples, cursor)
                if hi > lo:
                    audio[lo:hi] = values[lo-offset:hi-offset]
                    covered[lo:hi] = True
            for frame in container.decode(stream):
                check()
                for part in resampler.resample(frame):
                    place(part)
                if cursor >= samples:
                    break
            for part in resampler.resample(None):
                place(part)
    after = path.stat()
    if (after.st_size, after.st_mtime_ns) != expected:
        raise ValueError('Видео изменилось во время обработки')
    metadata = dict(start_ms=start, end_ms=end, frame_count=len(images),
        frame_times_ms=actual, requested_times_ms=times, has_audio=has_audio,
        audio_coverage=float(covered.mean()), audio_rms=float(np.sqrt(np.mean(audio**2))),
        audio_peak=float(np.max(np.abs(audio), initial=0)), sample_rate=SAMPLE_RATE)
    return VideoInput(metadata, images, audio)
