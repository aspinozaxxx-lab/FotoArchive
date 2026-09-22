"""Pinned face models and a local place-name directory; no photo data is sent out."""
import hashlib
import json
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path
import requests
import truststore
from .config import Settings, FACE_REV


def download(url, path, progress):
    if path.exists():
        return
    progress(f"Загрузка {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".download")
    with requests.get(url, stream=True, timeout=(15, 120)) as response:
        response.raise_for_status()
        with temporary.open("wb") as stream:
            for block in response.iter_content(1024 * 1024):
                stream.write(block)
    temporary.replace(path)


def setup(cfg=None, progress=print):
    truststore.inject_into_ssl()
    cfg = cfg or Settings.load()
    cfg.initialize()
    manifest_path = cfg.data_dir / "models/manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    for folder, filename, target in [
        ("face_detection_yunet", "face_detection_yunet_2026may.onnx", "yunet.onnx"),
        ("face_recognition_sface", "face_recognition_sface_2021dec.onnx", "sface.onnx"),
    ]:
        path = cfg.data_dir / "models/faces" / target
        url = f"https://media.githubusercontent.com/media/opencv/opencv_zoo/{FACE_REV}/models/{folder}/{filename}"
        download(url, path, progress)
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        manifest[f"faces/{target}"] = {"url": url, "revision": FACE_REV, "sha256": digest, "bytes": path.stat().st_size}
        download(f"https://raw.githubusercontent.com/opencv/opencv_zoo/{FACE_REV}/models/{folder}/LICENSE",
                 path.with_suffix(".LICENSE.txt"), progress)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    places = cfg.data_dir / "geonames"
    archive = places / "cities1000.zip"
    download("https://download.geonames.org/export/dump/cities1000.zip", archive, progress)
    download("https://download.geonames.org/export/dump/countryInfo.txt", places / "countryInfo.txt", progress)
    if not (places / "cities.sqlite3").exists():
        progress("Подготовка локального справочника мест")
        countries = {}
        for line in (places / "countryInfo.txt").read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                fields = line.split("\t")
                countries[fields[0]] = fields[4]
        connection = sqlite3.connect(places / "cities.building.sqlite3")
        connection.execute("DROP TABLE IF EXISTS cities")
        connection.execute("CREATE TABLE cities(id INTEGER PRIMARY KEY,name TEXT,aliases TEXT,country TEXT,latitude REAL,longitude REAL)")
        with zipfile.ZipFile(archive) as zipped, zipped.open("cities1000.txt") as stream, connection:
            batch = []
            for line in stream:
                fields = line.decode("utf-8").rstrip("\n").split("\t")
                batch.append((int(fields[0]), fields[1], fields[3], countries.get(fields[8], fields[8]), float(fields[4]), float(fields[5])))
                if len(batch) >= 2000:
                    connection.executemany("INSERT INTO cities VALUES(?,?,?,?,?,?)", batch)
                    batch.clear()
            connection.executemany("INSERT INTO cities VALUES(?,?,?,?,?,?)", batch)
        connection.close()
        (places / "cities.building.sqlite3").replace(places / "cities.sqlite3")
    (places / "manifest.json").write_text(json.dumps({"source": "https://www.geonames.org", "license": "CC BY 4.0",
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "downloaded_at": datetime.now(timezone.utc).isoformat()}, indent=2), encoding="utf-8")
    progress("Модели лиц и локальный справочник мест готовы")


if __name__ == "__main__":
    setup()
