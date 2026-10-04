"""Extract official SDK payloads for a portable build; no registry installation."""
from pathlib import Path
import io
import struct
import py7zr

source=Path(r'D:\FotoArchiveData\runtime\vulkan-sdk.exe')
target=Path(r'D:\FotoArchiveData\runtime\vulkan-sdk')
data=source.read_bytes()
offset=0
count=0
while True:
    offset=data.find(b'7z\xbc\xaf\x27\x1c',offset)
    if offset<0:break
    try:
        header_offset,header_size=struct.unpack_from('<QQ',data,offset+12)
        end=offset+32+header_offset+header_size
        if end>len(data) or end<=offset:raise ValueError('invalid archive header')
        with py7zr.SevenZipFile(io.BytesIO(data[offset:end])) as archive:
            names=archive.getnames()
            if any(Path(name).is_absolute() or '..' in Path(name).parts for name in names):
                raise ValueError('unsafe SDK archive path')
            # Only development headers, import libraries and shader compiler.
            wanted=[name for name in names if name.startswith(('Include/','Lib/','Bin/')) or
                    '/Include/' in name or '/Lib/' in name or '/Bin/' in name]
            if wanted:
                archive.extract(targets=wanted,path=target)
                count+=len(wanted)
                print(f'Extracted {len(wanted)} development files',flush=True)
    except (py7zr.Bad7zFile,ValueError,struct.error):
        pass
    offset+=6
print(f'Portable SDK: {count} files')
