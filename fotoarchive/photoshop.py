"""Read Photoshop's saved composite, skipping the potentially huge layer block."""
import os
import struct
from pathlib import Path

from PIL import Image
from psd_tools import PSDImage
from psd_tools.constants import Resource
from psd_tools.psd.document import PSD
from psd_tools.psd.header import FileHeader
from psd_tools.psd.color_mode_data import ColorModeData
from psd_tools.psd.image_resources import ImageResources
from psd_tools.psd.image_data import ImageData


def read_record(path, pixels=False):
    with Path(path).open('rb') as stream:
        size = os.fstat(stream.fileno()).st_size
        header = FileHeader.read(stream)
        Image._decompression_bomb_check((header.width, header.height))
        color_data = ColorModeData.read(stream)
        resources = ImageResources.read(stream)
        record = PSD(header=header, color_mode_data=color_data, image_resources=resources)
        if pixels:
            length_format = '>I' if header.version == 1 else '>Q'
            length_bytes = stream.read(struct.calcsize(length_format))
            length = struct.unpack(length_format, length_bytes)[0]
            end = stream.tell() + length
            if end + 2 > size:
                raise ValueError('PSD/PSB: блок слоёв выходит за конец файла')
            stream.seek(end)
            record.image_data = ImageData.read(stream)
        return record


def attach_metadata(image, record):
    resources = record.image_resources
    exif = resources.get_data(Resource.EXIF_DATA_1) or resources.get_data(Resource.EXIF_DATA_3)
    if isinstance(exif, bytes):
        image.info['exif'] = exif
    xmp = resources.get_data(Resource.XMP_METADATA)
    if isinstance(xmp, (bytes, str)):
        image.info['xmp'] = xmp
    return image


def metadata_image(path):
    # Location extraction needs only EXIF/XMP, never the document's pixels.
    return attach_metadata(Image.new('RGB', (1, 1)), read_record(path))


def open_image(path):
    record = read_record(path, pixels=True)
    document = PSDImage(record)
    document._max_alloc_bytes = 512 * 1024**2
    image = document.topil(apply_icc=True)
    if image is None:
        raise ValueError('PSD/PSB не содержит сведённого изображения. Сохраните его с включённой совместимостью Photoshop.')
    return attach_metadata(image, record)
