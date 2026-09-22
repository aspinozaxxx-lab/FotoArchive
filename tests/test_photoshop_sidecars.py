import struct
import time

import numpy as np
import pytest
from PIL import Image
from psd_tools import PSDImage
from psd_tools.constants import Compression, Resource
from psd_tools.psd.image_resources import ImageResource

from fotoarchive.catalog import Catalog, enumerate_source
from fotoarchive.config import Settings
from fotoarchive.engine import Engine
from fotoarchive.inventory import SourceInventory, format_totals
from fotoarchive.location import extract_location
from fotoarchive.media import open_rgb, read_media, sha256
from fotoarchive.media_types import is_appledouble
from fotoarchive.media_upgrade import apply


def make_psd(path, depth=16, compression=Compression.RLE, version=1):
    document = PSDImage.new('RGB', (32, 24), depth=depth)
    record = document._record
    record.header.version = version
    record.image_data.compression = compression
    scale = 257 if depth == 16 else 1
    dtype = '>u2' if depth == 16 else 'u1'
    record.image_data.set_data([np.full((24, 32), n*scale, dtype=dtype).tobytes() for n in (64, 128, 192)], record.header)
    exif = Image.Exif()
    exif[36867] = '2007:02:10 12:30:45'
    exif[274] = 6
    record.image_resources[Resource.EXIF_DATA_1] = ImageResource(key=Resource.EXIF_DATA_1, data=exif.tobytes()[6:])
    record.image_resources[Resource.XMP_METADATA] = ImageResource(key=Resource.XMP_METADATA,
        data=b'<x:xmpmeta xmlns:x="adobe:ns:meta/" xmlns:e="http://ns.adobe.com/exif/1.0/"><item e:GPSLatitude="55.75" e:GPSLongitude="37.61"/></x:xmpmeta>')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('wb') as stream:
        record.header.write(stream)
        record.color_mode_data.write(stream)
        record.image_resources.write(stream)
        # The layer block is deliberately not decodable. Reading the saved
        # composite must skip it, instead of parsing every layer into memory.
        stream.write(struct.pack('>I' if version == 1 else '>Q', 128*1024))
        stream.write(b'x'*(128*1024))
        record.image_data.write(stream)
    return path


@pytest.mark.parametrize('depth', [8, 16])
@pytest.mark.parametrize('compression', list(Compression))
def test_saved_composite_depth_compression_exif_orientation_and_integrity(tmp_path, depth, compression):
    path = make_psd(tmp_path/'photo.psd', depth, compression)
    before = sha256(path)
    metadata, image = read_media(path)
    assert image.size == (24, 32)
    assert image.getpixel((10, 10)) == (64, 128, 192)
    assert (metadata['width'], metadata['height']) == (24, 32)
    assert metadata['captured_at'] == '2007-02-10T12:30:45'
    assert open_rgb(path).getpixel((10, 10)) == (64, 128, 192)
    assert extract_location(path)['latitude'] == 55.75
    assert sha256(path) == before


def test_psb_uses_64_bit_layer_length(tmp_path):
    path = make_psd(tmp_path/'photo.psb', version=2)
    assert open_rgb(path).getpixel((10, 10)) == (64, 128, 192)


def test_appledouble_is_excluded_but_real_image_with_same_prefix_is_counted(tmp_path):
    cfg = Settings(data_dir=tmp_path/'data', root=tmp_path/'photos', includes=['.'])
    cfg.root.mkdir()
    original = cfg.root/'photo.jpg'; Image.new('RGB', (8, 8)).save(original)
    hidden = cfg.root/'._real.jpg'; Image.new('RGB', (8, 8)).save(hidden)
    sidecar = cfg.root/'._photo.jpg'; sidecar.write_bytes(b'\x00\x05\x16\x07'+bytes(50))
    before = sha256(sidecar)
    assert is_appledouble(sidecar) and not is_appledouble(hidden)
    assert {path.name for path, supported in enumerate_source(cfg) if supported} == {'photo.jpg', '._real.jpg'}
    cat = Catalog(cfg); inventory = SourceInventory(cat)
    try:
        inventory.ensure()
        deadline = time.monotonic()+5
        while not inventory.collect():
            assert time.monotonic()<deadline
            time.sleep(.01)
        status = inventory.status()
        assert status['total'] == 2
        assert status['format_counts'] == {'.jpg': 2, '.appledouble': 1}
        assert format_totals(status['format_counts'])['sidecars'] == 1
        assert sha256(sidecar) == before
    finally:
        inventory.close(); cat.close()


def test_upgrade_removes_sidecar_errors_and_retries_only_failed_psd(tmp_path):
    cfg = Settings(data_dir=tmp_path/'data', root=tmp_path/'photos', includes=['.'])
    cfg.root.mkdir()
    cat = Catalog(cfg)
    paths = [cfg.root/name for name in ('._photo.psd', 'photo.psd', 'ready.jpg')]
    for path in paths: path.write_bytes(b'old importer accepted this')
    ids = [cat.register(path)[0] for path in paths]
    for asset_id in ids:
        job = cat.next_job(('metadata',), asset_id)
        cat.finish_job(job, .1, 'old error' if asset_id != ids[2] else None)
    paths[0].write_bytes(b'\x00\x05\x16\x07'+bytes(50))
    before = [sha256(path) for path in paths]
    try:
        assert apply(cat) == [ids[0]]
        assert cat.get(ids[0])['present'] == 0
        assert cat.db.execute('SELECT count(*) FROM jobs WHERE asset_id=?', [ids[0]]).fetchone()[0] == 0
        assert cat.db.execute("SELECT status FROM jobs WHERE asset_id=? AND stage='metadata'", [ids[1]]).fetchone()[0] == 'pending'
        assert cat.db.execute("SELECT status FROM jobs WHERE asset_id=? AND stage='metadata'", [ids[2]]).fetchone()[0] == 'done'
        assert apply(cat) == []
        assert [sha256(path) for path in paths] == before
    finally:
        cat.close()
