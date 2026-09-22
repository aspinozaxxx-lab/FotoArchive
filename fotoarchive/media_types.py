"""Recognize sidecars without mistaking their apparent suffix for an image."""
from pathlib import Path

SOURCE_RULES_VERSION = 'appledouble-v1'
APPLEDOUBLE = '.appledouble'


def is_appledouble(path):
    path = Path(path)
    if not path.name.startswith('._'):
        return False
    # RFC 1740: the resource fork is a separate file, with no image data fork.
    with path.open('rb') as stream:
        return stream.read(4) == b'\x00\x05\x16\x07'


def format_key(path):
    return APPLEDOUBLE if is_appledouble(path) else path.suffix.lower()
