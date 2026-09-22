"""Lossless JPEG orientation metadata edits and reversible local file replacement."""
import os
from pathlib import Path
import struct

from PIL import Image

from .orientation import rotate_clockwise


CW_EXIF = {1:6, 2:7, 3:8, 4:5, 5:2, 6:3, 7:4, 8:1}


def compose_orientation(original, angle):
    if angle not in (90, 180, 270):
        raise ValueError("Поворот должен быть 90°, 180° или 270°")
    value = original if original in CW_EXIF else 1
    for _ in range(angle // 90):
        value = CW_EXIF[value]
    return value


def rotate_exif(payload, angle):
    """Preserve TIFF offsets, MakerNotes and all existing tags byte for byte.

    If Orientation is missing, append a new IFD0 and point the TIFF header to it.
    Existing tag values and all referenced IFDs retain their original offsets.
    """
    data = bytearray(payload)
    if data[:6] != b"Exif\0\0":
        raise ValueError("Некорректный EXIF")
    tiff = bytearray(data[6:])
    endian = "<" if tiff[:2] == b"II" else ">" if tiff[:2] == b"MM" else None
    if not endian or len(tiff) < 8 or struct.unpack_from(endian+"H", tiff, 2)[0] != 42:
        raise ValueError("Некорректный заголовок EXIF")
    offset = struct.unpack_from(endian+"I", tiff, 4)[0]
    if offset < 8 or offset+2 > len(tiff):
        raise ValueError("Некорректная таблица EXIF")
    count = struct.unpack_from(endian+"H", tiff, offset)[0]
    end = offset+2+12*count
    if end+4 > len(tiff):
        raise ValueError("Оборванная таблица EXIF")
    entries = []
    for i in range(count):
        position = offset+2+12*i
        tag, kind, size = struct.unpack_from(endian+"HHI", tiff, position)
        if tag == 274:
            if kind != 3 or size != 1:
                raise ValueError("Необычный формат EXIF Orientation; файл оставлен без изменений")
            old = struct.unpack_from(endian+"H", tiff, position+8)[0]
            struct.pack_into(endian+"H", tiff, position+8, compose_orientation(old, angle))
            return b"Exif\0\0" + tiff
        entries.append((tag, bytes(tiff[position:position+12])))
    if count == 65535:
        raise ValueError("Таблица EXIF переполнена")
    entry = struct.pack(endian+"HHIH", 274, 3, 1, compose_orientation(1, angle)) + b"\0\0"
    entries.append((274, entry))
    if len(tiff) % 2:
        tiff.append(0)
    new_offset = len(tiff)
    tiff += struct.pack(endian+"H", count+1) + b"".join(item[1] for item in sorted(entries)) + tiff[end:end+4]
    struct.pack_into(endian+"I", tiff, 4, new_offset)
    return b"Exif\0\0" + tiff


def rotate_jpeg_bytes(data, angle):
    if data[:2] != b"\xff\xd8":
        raise ValueError("Файл не является JPEG")
    position, found = 2, None
    while position < len(data):
        begin = position
        if data[position] != 255:
            raise ValueError("Некорректная структура JPEG")
        while position < len(data) and data[position] == 255:
            position += 1
        if position >= len(data):
            raise ValueError("Оборванный JPEG")
        marker = data[position]
        position += 1
        if marker in (0xDA, 0xD9):
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if position+2 > len(data):
            raise ValueError("Оборванный JPEG")
        length = int.from_bytes(data[position:position+2], "big")
        finish = position + length
        if length < 2 or finish > len(data):
            raise ValueError("Оборванный сегмент JPEG")
        if marker == 0xE1 and data[position+2:position+8] == b"Exif\0\0":
            if found is not None:
                raise ValueError("Несколько EXIF-блоков: требуется ручная проверка")
            found = (begin, finish, data[position+2:finish])
        position = finish
    if found:
        begin, finish, payload = found
    else:
        begin = finish = 2
        payload = b"Exif\0\0II" + struct.pack("<HIHI", 42, 8, 0, 0)
    payload = rotate_exif(payload, angle)
    if len(payload)+2 > 65535:
        raise ValueError("В EXIF нет места для тега ориентации; файл оставлен без изменений")
    return data[:begin] + b"\xff\xe1" + struct.pack(">H", len(payload)+2) + payload + data[finish:]


def write_rotated(source: Path, destination: Path, angle):
    """Write a staged file only. The caller journals and replaces the original."""
    extension = source.suffix.lower()
    if extension in (".jpg", ".jpeg"):
        destination.write_bytes(rotate_jpeg_bytes(source.read_bytes(), angle))
    elif extension == ".bmp":
        with Image.open(source) as image:
            image.load()
            if image.mode not in ("RGB", "P", "L", "1"):
                raise ValueError("Этот вариант BMP пока не поддерживает безопасный поворот")
            rotated = rotate_clockwise(image, angle)
            options = {"dpi": image.info["dpi"]} if "dpi" in image.info else {}
            rotated.save(destination, format="BMP", **options)
    else:
        raise ValueError("Поворот поддерживается для JPG, JPEG и BMP")
    with destination.open("rb+") as stream:
        os.fsync(stream.fileno())
