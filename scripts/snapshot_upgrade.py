import hashlib
import json
import sqlite3
from pathlib import Path
root = Path(r"D:\FotoArchiveData")
connection = sqlite3.connect(root / "catalog.sqlite3")
records = {}
for asset_id, path, version in connection.execute("SELECT id,path,version FROM assets"):
    target = Path(path)
    with target.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    records[str(asset_id)] = {"path": path, "version": version, "sha256": digest}
embeddings = hashlib.sha256()
for asset_id, blob in connection.execute("SELECT unit_id,vector FROM embeddings ORDER BY unit_id"):
    embeddings.update(asset_id.encode()); embeddings.update(blob)
result = {"originals": records, "visual_embeddings_sha256": embeddings.hexdigest()}
(root / "reports/v02_before.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
print("Originals protected:", len(records), "Visual vectors:", embeddings.hexdigest(), flush=True)
