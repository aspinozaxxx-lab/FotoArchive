"""Versioned, opaque cache containers. Obfuscation, deliberately not encryption."""
import hashlib
import json
import zlib

MAGIC = b'FotoArchive-Cache\x00\x01'
MAX_INPUT = 64 * 1024**2
MAX_PACKED = MAX_INPUT + 65536
MAX_PENDING = 8192
UPLOAD_AHEAD = 64


def pack(data):
    if len(data) > MAX_INPUT:
        raise ValueError('Снимок слишком велик для серверного кэша')
    return MAGIC + zlib.compress(data, 1)


def unpack(data):
    if not data.startswith(MAGIC) or len(data) > MAX_PACKED:
        raise ValueError('Invalid cache container')
    decoder = zlib.decompressobj()
    result = decoder.decompress(data[len(MAGIC):], MAX_INPUT+1)
    if len(result) > MAX_INPUT or not decoder.eof or decoder.unused_data:
        raise ValueError('Invalid or oversized cache payload')
    return result


def digest(data):
    return hashlib.sha256(data).hexdigest()


def job_key(blob, stages, context=None):
    return digest(json.dumps([blob, stages, context or {}], sort_keys=True).encode())


def valid_key(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)
