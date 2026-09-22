from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

from .config import RAW_FORMATS, VIDEO_FORMATS

register_heif_opener(thumbnails=False, decode_threads=2)


def raw_tags(path):
    import exifread
    with Path(path).open('rb') as stream:
        return exifread.process_file(stream, details=False)


def raw_image(path, full_resolution=False):
    import rawpy
    with rawpy.imread(str(path)) as raw:
        if not full_resolution:
            try:
                thumb = raw.extract_thumb()
                if thumb.format == rawpy.ThumbFormat.JPEG:
                    with Image.open(io.BytesIO(thumb.data)) as embedded:
                        embedded.load()
                        orientation = embedded.getexif().get(274, 1)
                        image = ImageOps.exif_transpose(embedded).convert('RGB')
                else:
                    image, orientation = Image.fromarray(thumb.data), 1
                if max(image.size) >= 1008:
                    if orientation == 1:
                        operation = {3: Image.Transpose.ROTATE_180, 5: Image.Transpose.ROTATE_90,
                                     6: Image.Transpose.ROTATE_270}.get(raw.sizes.flip)
                        if operation:
                            image = image.transpose(operation)
                    return image
            except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError, OSError):
                pass
        return Image.fromarray(raw.postprocess(use_camera_wb=True, half_size=not full_resolution, output_bps=8))


def open_rgb(path: Path, full_resolution=False) -> Image.Image:
    path = Path(path)
    if path.suffix.lower() in VIDEO_FORMATS:
        from .video import frame_at
        return frame_at(path)[0]
    if path.suffix.lower() in RAW_FORMATS:
        return raw_image(path, full_resolution)
    if path.suffix.lower() in {'.psd', '.psb'}:
        from .photoshop import open_image
        with open_image(path) as image:
            return ImageOps.exif_transpose(image).convert('RGB')
    with Image.open(path) as image:
        image.load()
        return ImageOps.exif_transpose(image).convert("RGB")


def extract_metadata(path: Path) -> dict:
    if path.suffix.lower() in RAW_FORMATS | VIDEO_FORMATS | {'.psd', '.psb'}:
        metadata, image = read_media(path)
        image.close()
        return metadata
    with Image.open(path) as im:
        return metadata_from_image(im)


def read_media(path):
    path = Path(path)
    if path.suffix.lower() == '.cpt':
        raise ValueError('Corel Photo-Paint CPT: требуется экспорт в TIFF или PNG. Оригинал сохранён.')
    if path.suffix.lower() in VIDEO_FORMATS:
        from .video import probe
        return probe(path)
    if path.suffix.lower() in {'.psd', '.psb'}:
        from .photoshop import open_image
        with open_image(path) as image:
            return metadata_from_image(image), ImageOps.exif_transpose(image).convert('RGB')
    if path.suffix.lower() in RAW_FORMATS:
        import rawpy
        tags = raw_tags(path)
        with rawpy.imread(str(path)) as raw:
            width, height = raw.sizes.width, raw.sizes.height
            if raw.sizes.flip in (5, 6):
                width, height = height, width
        date_raw = str(tags.get('EXIF DateTimeOriginal', '')).strip().strip('\x00')
        try:
            captured = datetime.strptime(date_raw, '%Y:%m:%d %H:%M:%S').isoformat()
        except ValueError:
            captured = None
        metadata = {'width': width, 'height': height, 'captured_at': captured, 'date_raw': date_raw,
                    'date_offset': str(tags.get('EXIF OffsetTimeOriginal', '')),
                    'camera': ' '.join(str(tags.get(key, '')).strip() for key in ('Image Make', 'Image Model')).strip(),
                    'orientation': 'landscape' if width > height else 'portrait' if height > width else 'square',
                    'metadata_json': json.dumps({str(k): str(v) for k, v in tags.items()}, ensure_ascii=False)}
        return metadata, raw_image(path)
    with Image.open(path) as image:
        metadata = metadata_from_image(image)
        return metadata, ImageOps.exif_transpose(image).convert('RGB')


def visual_path(asset, cfg=None):
    path = Path(asset['path'])
    if path.suffix.lower() not in RAW_FORMATS | VIDEO_FORMATS | {'.psd', '.psb'}:
        return path
    directory = cfg.data_dir / 'previews' if cfg else Path(asset['_cache_dir'])
    budget = cfg.preview_budget if cfg else asset.get('_preview_budget', 20*1024**3)
    return PreviewCache(directory, budget).get(asset)


def metadata_from_image(im: Image.Image) -> dict:
    exif = im.getexif()
    try:
        tags = dict(exif) | dict(exif.get_ifd(34665))
    except (ValueError, KeyError, TypeError):
        tags = dict(exif)
    date_raw = str(tags.get(36867, "")).strip().strip("\x00")
    captured = None
    try:
        captured = datetime.strptime(date_raw, "%Y:%m:%d %H:%M:%S").isoformat()
    except ValueError:
        pass
    width, height = im.size
    if tags.get(274) in (5, 6, 7, 8):
        width, height = height, width
    im.load()
    return {"width": width, "height": height, "captured_at": captured,
            "date_raw": date_raw, "date_offset": str(tags.get(36881, "")),
            "camera": " ".join(str(tags.get(t, "")).strip().strip("\x00") for t in (271, 272)).strip(),
            "orientation": "landscape" if width > height else "portrait" if height > width else "square",
            "metadata_json": json.dumps({str(k): str(v) for k, v in tags.items() if k != 37500}, ensure_ascii=False)}


def atomic_thumbnail(source: Path, target: Path, side=448):
    image = open_rgb(source)
    save_thumbnail(image, target, side)


def save_thumbnail(image: Image.Image, target: Path, side=448):
    image.thumbnail((side, side), Image.Resampling.LANCZOS)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(suffix='.tmp', dir=target.parent)
    os.close(handle)
    temp = Path(name)
    try:
        image.save(temp, format="WEBP", quality=82)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def image_bytes(path: Path, side=1008) -> bytes:
    image = open_rgb(path)
    image.thumbnail((side, side), Image.Resampling.LANCZOS)
    stream = io.BytesIO()
    image.save(stream, format="JPEG", quality=90)
    return stream.getvalue()


def sha256(path: Path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class PreviewCache:
    def __init__(self, directory: Path, budget: int):
        self.directory, self.budget = directory, budget
        directory.mkdir(parents=True, exist_ok=True)

    def get(self, asset: dict) -> Path:
        is_video = Path(asset['path']).suffix.lower() in VIDEO_FORMATS
        timestamp = int(asset.get('timestamp_ms') or 0)
        key = f"{asset['id']}_{asset['version']}" + (f'_frame{timestamp}' if is_video else '') + '.jpg'
        target = self.directory / key
        if not target.exists():
            if is_video:
                from .video import frame_at
                image, _ = frame_at(Path(asset['path']), timestamp)
            else:
                image = open_rgb(Path(asset["path"]))
            image.thumbnail((2560, 2560), Image.Resampling.LANCZOS)
            handle, name = tempfile.mkstemp(suffix='.tmp', dir=self.directory)
            os.close(handle)
            temp = Path(name)
            try:
                image.save(temp, format="JPEG", quality=93)
                os.replace(temp, target)
            finally:
                temp.unlink(missing_ok=True)
            if is_video and asset.get('unit_thumbnail'):
                save_thumbnail(image.copy(), Path(asset['unit_thumbnail']))
            self.trim(exclude=target)
        elif is_video and asset.get('unit_thumbnail') and not Path(asset['unit_thumbnail']).exists():
            with Image.open(target) as image:
                save_thumbnail(image.copy(), Path(asset['unit_thumbnail']))
        os.utime(target, None)
        return target

    def trim(self, exclude=None):
        files = [(p.stat().st_mtime, p.stat().st_size, p) for p in self.directory.glob("*.jpg")]
        total = sum(s for _, s, _ in files)
        for _, size, path in sorted(files):
            if total <= self.budget:
                break
            if path != exclude:
                path.unlink(missing_ok=True)
                total -= size
