"""Convert the unchanged generated PNG into a multi-resolution Windows container."""
from pathlib import Path
from PIL import Image


assets = Path(__file__).resolve().parents[1] / "fotoarchive/assets"
with Image.open(assets / "FotoArchive.png") as source:
    source.save(assets / "FotoArchive.ico", format="ICO",
                sizes=[(size, size) for size in (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)])
