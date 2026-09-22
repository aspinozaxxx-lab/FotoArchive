"""GPS and place context kept apart from visual content; entirely local lookup."""
import json
import math
import re
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path
import numpy as np
from PIL import Image, IptcImagePlugin
from .config import RAW_FORMATS, VIDEO_FORMATS


def coordinate(value, reference="", latitude=True):
    try:
        reference = str(reference).strip("b'\" ").upper()
        if reference and reference not in ({"N", "S"} if latitude else {"E", "W"}):
            return None
        if isinstance(value, (tuple, list)):
            parts = [float(v) for v in value]
        else:
            text = str(value).strip()
            if text[-1:].upper() in {"N", "S", "E", "W"}:
                reference, text = text[-1].upper(), text[:-1]
                if reference not in ({"N", "S"} if latitude else {"E", "W"}):
                    return None
            parts = [float(p) for p in re.split(r"[, :]+", text) if p]
        if len(parts) not in (1, 2, 3):
            return None
        result = abs(parts[0]) + (parts[1] / 60 if len(parts) > 1 else 0) + (parts[2] / 3600 if len(parts) > 2 else 0)
        if reference in {"S", "W"} or parts[0] < 0:
            result = -result
        if not math.isfinite(result) or abs(result) > (90 if latitude else 180):
            return None
        if any(not 0 <= p < 60 for p in parts[1:]):
            return None
        return result
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None


def extract_location(path):
    result = {"latitude": None, "longitude": None, "altitude": None, "geo_text": "", "geo_source": "", "geo_json": "{}"}
    path = Path(path)
    if path.suffix.lower() in RAW_FORMATS | VIDEO_FORMATS:
        if path.suffix.lower() in RAW_FORMATS:
            from .media import raw_tags
            tags = raw_tags(path)
            def values(name):
                tag = tags.get(name)
                return tag.values if tag else None
            lat = coordinate(values('GPS GPSLatitude'), str(tags.get('GPS GPSLatitudeRef', '')))
            lon = coordinate(values('GPS GPSLongitude'), str(tags.get('GPS GPSLongitudeRef', '')), latitude=False)
            if lat is not None and lon is not None:
                result.update(latitude=lat, longitude=lon, geo_source='EXIF GPS')
            try:
                result['altitude'] = float(values('GPS GPSAltitude')[0]) * (-1 if values('GPS GPSAltitudeRef') == [1] else 1)
            except (ValueError, TypeError, IndexError, ZeroDivisionError):
                pass
        else:
            import av
            with av.open(str(path), options={'protocol_whitelist': 'file,crypto,data'}) as video:
                tags = dict(video.metadata)
                for stream in video.streams.video:
                    tags.update(stream.metadata)
            location = tags.get('com.apple.quicktime.location.ISO6709') or tags.get('location', '')
            match = re.fullmatch(r'([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)?/?', location)
            if match:
                lat, lon = coordinate(match[1]), coordinate(match[2], latitude=False)
                if lat is not None and lon is not None:
                    result.update(latitude=lat, longitude=lon, altitude=float(match[3]) if match[3] else None, geo_source='GPS видео')
        result['geo_json'] = json.dumps({'gps_source': result['geo_source']}, ensure_ascii=False)
        return sidecar_location(path, result)
    if path.suffix.lower() in {'.psd', '.psb'}:
        from .photoshop import metadata_image
        source_image = metadata_image(path)
    else:
        source_image = Image.open(path)
    with source_image as image:
        exif = image.getexif()
        try:
            gps = dict(exif.get_ifd(34853))
        except (ValueError, KeyError, TypeError):
            gps = {}
        lat = coordinate(gps.get(2), gps[1]) if gps.get(1) else None
        lon = coordinate(gps.get(4), gps[3], latitude=False) if gps.get(3) else None
        if lat is not None and lon is not None:
            result.update(latitude=lat, longitude=lon, geo_source="EXIF GPS")
            try:
                altitude = float(gps[6])
                if gps.get(5) in (1, b"\x01"):
                    altitude = -altitude
                if math.isfinite(altitude):
                    result["altitude"] = altitude
            except (KeyError, ValueError, TypeError, ZeroDivisionError):
                pass
        labels = []
        xmp = image.info.get("xmp") or exif.get(700)
        tags = {}
        if isinstance(xmp, (bytes, str)) and len(xmp) <= 2 * 1024**2:
            try:
                root = ET.fromstring(xmp)
                for element in root.iter():
                    for key, value in list(element.attrib.items()) + [(element.tag, element.text or "")]:
                        tags[key.split("}")[-1]] = value.strip()
                for key in ("Location", "City", "State", "Country", "CountryName"):
                    if tags.get(key):
                        labels.append(tags[key])
            except ET.ParseError:
                pass
        if result["latitude"] is None:
            lat = coordinate(tags.get("GPSLatitude"))
            lon = coordinate(tags.get("GPSLongitude"), latitude=False)
            if lat is not None and lon is not None:
                result.update(latitude=lat, longitude=lon, geo_source="XMP GPS")
        try:
            iptc = IptcImagePlugin.getiptcinfo(image) or {}
            for key in ((2, 92), (2, 90), (2, 95), (2, 101)):
                value = iptc.get(key)
                if isinstance(value, bytes):
                    labels.append(value.decode("utf-8", errors="replace").strip())
        except (OSError, ValueError, TypeError):
            pass
        labels = list(dict.fromkeys(x for x in labels if x))
        if labels:
            result["geo_text"] = "Место съёмки: " + ", ".join(labels)
            result["geo_source"] = (result["geo_source"] + "; " if result["geo_source"] else "") + "XMP/IPTC"
        result["geo_json"] = json.dumps({"metadata_place_names": labels, "gps_source": result["geo_source"]}, ensure_ascii=False)
    return sidecar_location(path, result)


def sidecar_location(path, result):
    for sidecar in dict.fromkeys((path.with_suffix('.xmp'), path.with_name(path.name + '.xmp'))):
        try:
            if not sidecar.is_file() or sidecar.stat().st_size > 2*1024**2:
                continue
            tags = {}
            for element in ET.fromstring(sidecar.read_bytes()).iter():
                for key, value in list(element.attrib.items()) + [(element.tag, element.text or '')]:
                    tags[key.split('}')[-1]] = value.strip()
            lat, lon = coordinate(tags.get('GPSLatitude')), coordinate(tags.get('GPSLongitude'), latitude=False)
            if result['latitude'] is None and lat is not None and lon is not None:
                result.update(latitude=lat, longitude=lon, geo_source='XMP GPS')
            labels = list(dict.fromkeys(tags[key] for key in ('Location', 'City', 'State', 'Country', 'CountryName') if tags.get(key)))
            if labels and not result['geo_text']:
                result['geo_text'] = 'Место съёмки: ' + ', '.join(labels)
            result['geo_json'] = json.dumps(json.loads(result['geo_json']) | {'sidecar': sidecar.name, 'metadata_place_names': labels}, ensure_ascii=False)
        except (OSError, ET.ParseError, ValueError):
            continue
    return result


class Places:
    def __init__(self, cfg):
        self.path = cfg.data_dir / "geonames/cities.sqlite3"
        self.connection = None
        self.xyz = self.ids = None
        manifest = cfg.data_dir / "geonames/manifest.json"
        self.revision = json.loads(manifest.read_text())["archive_sha256"] if manifest.exists() else "unavailable"

    def enrich(self, location):
        if location["geo_text"] or location["latitude"] is None or not self.path.exists():
            return location
        if self.connection is None:
            self.connection = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
            records = np.array(self.connection.execute("SELECT id,latitude,longitude FROM cities").fetchall(), dtype=np.float64)
            self.ids = records[:, 0].astype(np.int64)
            lat, lon = np.radians(records[:, 1]), np.radians(records[:, 2])
            self.xyz = np.column_stack([np.cos(lat)*np.cos(lon), np.cos(lat)*np.sin(lon), np.sin(lat)])
        lat, lon = math.radians(location["latitude"]), math.radians(location["longitude"])
        point = np.array([math.cos(lat)*math.cos(lon), math.cos(lat)*math.sin(lon), math.sin(lat)])
        dots = self.xyz @ point
        nearest = int(np.argmax(dots))
        city_id = int(self.ids[nearest])
        name, aliases, country, city_lat, city_lon = self.connection.execute("SELECT name,aliases,country,latitude,longitude FROM cities WHERE id=?", (city_id,)).fetchone()
        # Haversine is numerically stable for a photograph close to the city centre.
        dlat, dlon = math.radians(city_lat)-lat, math.radians(city_lon)-lon
        hav = math.sin(dlat/2)**2 + math.cos(lat)*math.cos(math.radians(city_lat))*math.sin(dlon/2)**2
        distance = 6371 * 2 * math.asin(min(1, math.sqrt(hav)))
        # The compact GeoNames alias field has no language tags. Keep all names
        # searchable instead of mislabelling its first Cyrillic variants as Russian.
        alternate_names = list(dict.fromkeys(a for a in aliases.split(",") if a))
        location["geo_text"] = f"Ближайший город: {name}, {country}. Примерно {distance:.1f} км от центра."
        location["geo_json"] = json.dumps(json.loads(location["geo_json"]) | {"nearest_city_id": city_id, "nearest_city_km": distance,
            "gazetteer_sha256": self.revision, "place_names_are_approximate": True, "alternate_names": alternate_names}, ensure_ascii=False)
        return location

    def close(self):
        if self.connection:
            self.connection.close()
