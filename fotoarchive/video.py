"""Read-only FFmpeg decoding through PyAV; bounded random access to sampled frames."""
from datetime import datetime
import json
import math
import time

import av

from .config import VIDEO_INTERVAL_MS


def timestamp_text(milliseconds):
    seconds = max(0, int(milliseconds or 0) // 1000)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f'{hours}:{minutes:02}:{seconds:02}' if hours else f'{minutes:02}:{seconds:02}'


def asset_at_moment(asset, moment):
    """Do not carry a different frame's caption or verdict into the viewer."""
    result = dict(asset)
    if moment.get('unit_id') != asset.get('unit_id'):
        for key in ('description','observations_json','verification','face_score','face_match_id'):
            result.pop(key,None)
    result.update(unit_id=moment['unit_id'],timestamp_ms=moment['timestamp_ms'])
    result['thumbnail'] = result['unit_thumbnail'] = moment.get('thumbnail','')
    return result


def search_coverage_text(asset):
    from .config import EMBED_VERSION
    state = ('Поисковая обработка ещё не завершена; доступны готовые кадры. '
             if asset.get('embed_version') != EMBED_VERSION else '')
    return state+f'Поиск по кадрам через {VIDEO_INTERVAL_MS//1000} секунд; короткие события между ними могут быть пропущены. Проверка условий относится к отдельному кадру.'


def _stream(container):
    stream = next((s for s in container.streams.video if not (s.disposition & s.disposition.attached_pic)), None)
    if stream is None:
        raise ValueError('В файле не найдена видеодорожка')
    stream.thread_count = 2
    return stream


def frame_at(path, milliseconds=0, max_side=2560):
    with av.open(str(path), options={'protocol_whitelist': 'file,crypto,data'}) as container:
        stream = _stream(container)
        start = int(stream.start_time or 0)
        target = start + int(milliseconds / 1000 / stream.time_base)
        if milliseconds:
            container.seek(target, stream=stream, backward=True)
        deadline = time.monotonic() + 60
        last = None
        for frame in container.decode(stream):
            if time.monotonic() > deadline:
                raise TimeoutError('Слишком долгое декодирование кадра видео')
            last = frame
            if frame.pts is None or frame.pts >= target:
                break
        if last is None:
            raise ValueError('Не удалось прочитать кадр видео')
        rotation = last.rotation or float(stream.metadata.get('rotate', 0))
        scale = min(1, max_side / max(last.width, last.height))
        image = last.reformat(width=max(1, round(last.width*scale)), height=max(1, round(last.height*scale)), format='rgb24').to_image()
        if rotation:
            image = image.rotate(rotation, expand=True)
        actual_ms = round((int(last.pts or start) - start) * float(stream.time_base) * 1000)
        return image, max(0, actual_ms)


def probe(path):
    with av.open(str(path), options={'protocol_whitelist': 'file,crypto,data'}) as container:
        stream = _stream(container)
        seconds = (float(stream.duration * stream.time_base) if stream.duration is not None else
                   float(container.duration or 0) / av.time_base)
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError('Не удалось определить длительность видео; файл сохранён в списке ошибок')
        tags = dict(container.metadata) | dict(stream.metadata)
        duration = max(1, round(seconds * 1000))
        width, height = stream.codec_context.width, stream.codec_context.height
    poster, _ = frame_at(path)
    if (width > height) != (poster.width > poster.height):
        width, height = height, width
    raw_date = tags.get('com.apple.quicktime.creationdate') or tags.get('creation_time', '')
    captured = None
    try:
        date = datetime.fromisoformat(raw_date.replace('Z', '+00:00'))
        if 1900 <= date.year <= 2200:
            captured = date.isoformat()
    except (ValueError, TypeError):
        pass
    return {'width': width, 'height': height, 'captured_at': captured, 'date_raw': raw_date,
            'date_offset': '', 'camera': ' '.join(tags.get(key, '') for key in
                ('com.apple.quicktime.make', 'com.apple.quicktime.model')).strip(),
            'orientation': 'landscape' if width > height else 'portrait' if height > width else 'square',
            'metadata_json': json.dumps({'video_tags': tags, 'sample_interval_ms': VIDEO_INTERVAL_MS}, ensure_ascii=False),
            'duration_ms': duration, 'media_kind': 'video'}, poster


def sample_times(duration_ms):
    # A generator, without a cap that could silently omit the end of long clips.
    yield from range(0, max(1, duration_ms), VIDEO_INTERVAL_MS)
