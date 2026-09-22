from __future__ import annotations

import json
import math
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import lancedb
import numpy as np
import pyarrow as pa
from lancedb.index import BTree, FTS, IvfHnswSq

from .catalog import Catalog, Filters
from .config import EMBED_VERSION, FACE_VERSION, GEO_VERSION


SCHEMA = pa.schema([
    ("unit_id", pa.string()), ("asset_id", pa.int64()), ("version", pa.int64()),
    ("model_version", pa.string()), ("vector", pa.list_(pa.float32(), 768)),
    ("folder", pa.string()), ("captured_at", pa.string()), ("camera", pa.string()),
    ("extension", pa.string()), ("orientation", pa.string()), ("width", pa.int64()),
    ("height", pa.int64()), ("pixels", pa.int64()), ("size", pa.int64()),
    ("present", pa.int64()), ("metadata_ready", pa.int64()), ("search_text", pa.string()),
])
FACE_SCHEMA = pa.schema([(field.name, pa.list_(pa.float32(), 128) if field.name == "vector" else field.type) for field in SCHEMA])


class SearchIndex:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self.writer = None
        self.writing = None
        self.last_cleanup = time.monotonic()
        self.connection = lancedb.connect(str(catalog.cfg.data_dir / "vectors"))
        self.table = self.connection.create_table("photos", schema=SCHEMA, exist_ok=True)
        self.contexts = self.connection.create_table("places", schema=SCHEMA, exist_ok=True)
        self.faces = self.connection.create_table("faces", schema=FACE_SCHEMA, exist_ok=True)
        if not any("unit_id" in idx.columns for idx in self.table.list_indices()):
            self.table.create_index("unit_id", config=BTree())

    def auxiliary_rows(self, assets):
        assets = list({a['id']: a for a in assets if a}.values())
        ids = [a["id"] for a in assets]
        if not ids:
            return [], [], []
        context_rows, face_rows = [], []
        for asset in assets:
            if not asset or not asset["present"]:
                continue
            base = {name: asset[name] for name in ("version", "folder", "captured_at", "camera", "extension", "orientation", "width", "height", "pixels", "size", "present", "metadata_ready")}
            base.update(asset_id=asset["id"], search_text=asset.get("geo_text", ""))
            vector = self.catalog.db.execute("SELECT vector FROM geo_embeddings WHERE asset_id=? AND file_version=? AND model_version=? AND geo_version=?",
                                             (asset["id"], asset["version"], EMBED_VERSION, GEO_VERSION)).fetchone()
            if vector:
                context_rows.append(base | {"unit_id": f"{asset['id']}:place", "model_version": EMBED_VERSION,
                                            "vector": np.frombuffer(vector[0], dtype=np.float32).tolist()})
            for face in self.catalog.db.execute("SELECT id,vector FROM faces WHERE asset_id=? AND file_version=? AND model_version=?",
                                                 (asset["id"], asset["version"], FACE_VERSION)):
                face_rows.append(base | {"unit_id": face["id"], "model_version": FACE_VERSION,
                                         "vector": np.frombuffer(face["vector"], dtype=np.float32).tolist()})
        return ids, context_rows, face_rows

    def write_auxiliary(self, ids, context_rows, face_rows):
        if not ids:
            return
        condition = "asset_id IN (" + ",".join(str(i) for i in ids) + ")"
        for table, rows, schema in ((self.contexts, context_rows, SCHEMA), (self.faces, face_rows, FACE_SCHEMA)):
            if table.count_rows():
                table.delete(condition)
            if rows:
                table.add(pa.Table.from_pylist(rows, schema=schema))

    def prepare_flush(self, limit=128):
        pending = self.catalog.db.execute("SELECT * FROM outbox LIMIT ?", (limit,)).fetchall()
        if not pending:
            return None
        rows = []
        delete = []
        assets = []
        for item in pending:
            unit_id = item["unit_id"]
            unit = self.catalog.db.execute("SELECT asset_id FROM units WHERE id=?", (unit_id,)).fetchone()
            asset = self.catalog.get(unit[0] if unit else int(unit_id.split(':', 1)[0]))
            assets.append(asset)
            if asset and unit:
                asset = self.catalog.media_units.asset_at(asset, unit_id)
            vector = self.catalog.db.execute("SELECT * FROM embeddings WHERE unit_id=?", (unit_id,)).fetchone()
            if not asset or not asset["present"] or not vector or vector["file_version"] != asset["version"] or vector["model_version"] != EMBED_VERSION:
                delete.append(unit_id)
                continue
            values = np.frombuffer(vector["vector"], dtype=np.float32)
            if len(values) != 768:
                raise ValueError("Размерность эмбеддинга не соответствует версии индекса")
            observations = json.loads(asset["observations_json"] or "{}")
            place = json.loads(asset.get("geo_json") or "{}")
            text = " ".join([asset["filename"], asset["relative_path"], asset["description"], asset.get("geo_text", ""),
                             " ".join(place.get("alternate_names", [])),
                             " ".join(observations.get("objects", [])), " ".join(observations.get("actions", []))])
            row = {name: asset[name] for name in ("version", "folder", "captured_at", "camera", "extension", "orientation", "width", "height", "pixels", "size", "present", "metadata_ready")}
            row.update(unit_id=unit_id, asset_id=asset["id"], model_version=EMBED_VERSION, vector=values.tolist(), search_text=text)
            rows.append(row)
        return pending, rows, delete, self.auxiliary_rows(assets)

    def write_flush(self, payload):
        # Only LanceDB work runs here. SQLite remains owned by the coordinator.
        pending, rows, delete, auxiliary = payload
        self.write_auxiliary(*auxiliary)
        if delete:
            self.table.delete("unit_id IN (" + ",".join("'" + v.replace("'", "''") + "'" for v in delete) + ")")
        if rows:
            self.table.merge_insert("unit_id").when_matched_update_all().when_not_matched_insert_all().execute(pa.Table.from_pylist(rows, schema=SCHEMA))
        self.cleanup_history()

    def cleanup_history(self, force=False):
        # Runs in the index writer, never in the gallery reader or Qt thread.
        # Retain recent transactions for concurrent reads; do not accumulate
        # days of manifests while a large import keeps the worker busy.
        now = time.monotonic()
        if not force and now - self.last_cleanup < 600:
            return
        self.last_cleanup = now
        try:
            for table in (self.table, self.faces, self.contexts):
                table.optimize(cleanup_older_than=timedelta(minutes=10), delete_unverified=False)
        except Exception:
            # Committed index writes are still valid and can be acknowledged.
            # Retry housekeeping at the next interval without stalling jobs.
            logging.getLogger(__name__).exception('Search index history cleanup failed')

    def acknowledge_flush(self, payload):
        pending = payload[0]
        # Revision matching preserves changes made while the writer was busy.
        # A crash before acknowledgement safely replays the idempotent merge.
        with self.catalog.db:
            for item in pending:
                self.catalog.db.execute("DELETE FROM outbox WHERE unit_id=? AND revision=?", (item["unit_id"], item["revision"]))
        return len(pending)

    def collect_flush(self, wait=False):
        if self.writing is None:
            return 0
        future, payload = self.writing
        if not wait and not future.done():
            return 0
        self.writing = None
        future.result()
        return self.acknowledge_flush(payload)

    def start_flush(self, limit=128):
        self.collect_flush()
        if self.writing is not None:
            return False
        payload = self.prepare_flush(limit)
        if payload is None:
            return False
        if self.writer is None:
            self.writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="search-index")
        self.writing = (self.writer.submit(self.write_flush, payload), payload)
        return True

    def flush(self, limit=128):
        self.collect_flush(wait=True)
        payload = self.prepare_flush(limit)
        if payload is None:
            return 0
        self.write_flush(payload)
        return self.acknowledge_flush(payload)

    def flush_all(self):
        while self.flush():
            pass

    def maintain(self, force=False):
        self.collect_flush(wait=True)
        count = self.table.count_rows()
        if count == 0:
            return
        self.table.create_index("search_text", config=FTS(language="Russian", stem=True, memory_limit=256 * 1024**2, num_workers=4), replace=True)
        if count >= self.catalog.cfg.ann_threshold:
            previous = self.catalog.state("ann_rows", 0)
            if force or not previous or count > previous * 1.25:
                self.table.create_index("vector", config=IvfHnswSq(distance_type="cosine", num_partitions=max(1, round(math.sqrt(count)))), replace=True)
                self.catalog.set_state("ann_rows", count)
        self.cleanup_history(force=True)

    def close(self):
        try:
            self.collect_flush(wait=True)
        finally:
            if self.writer:
                self.writer.shutdown(wait=True)
                self.writer = None

    def _where(self, filters):
        where = self.filter_where(filters)
        return where + " AND model_version='" + EMBED_VERSION.replace("'", "''") + "'"

    def filter_where(self, filters):
        from dataclasses import replace
        geographic = bool(filters.place or filters.geo_bounds or filters.has_gps)
        where, _ = replace(filters, place='', geo_bounds='', has_gps=False).expression(literal=True)
        if geographic:
            sql, params = filters.expression()
            # Exact catalogue membership is applied BEFORE nearest neighbours.
            # No vector migration or re-embedding is needed for a map rectangle.
            ids = ','.join(str(row[0]) for row in self.catalog.db.execute('SELECT id FROM assets WHERE '+sql, params))
            where += ' AND asset_id IN (' + (ids or '-1') + ')'
        return where

    def candidates(self, vector, query: str, filters: Filters, limit=200, offset=0, merge_all=False, exact=False):
        # Interactive reads take over after the current bounded write completes.
        self.collect_flush(wait=True)
        where = self._where(filters)
        scores = {}
        versions = {}
        matches = {}
        moments = {}
        if self.table.count_rows() and vector is not None:
            builder = self.table.search(np.asarray(vector, dtype=np.float32)).metric("cosine").where(where, prefilter=True).select(["unit_id", "asset_id", "version", "_distance"]).limit(limit).offset(offset)
            if exact:
                builder = builder.bypass_vector_index()
            elif self.catalog.state("ann_rows", 0):
                builder = builder.nprobes(32).refine_factor(3)
            for rank, row in enumerate(builder.to_list()):
                moments.setdefault(row['asset_id'], []).append(row['unit_id'])
                if row['asset_id'] in matches:
                    continue
                scores[row["asset_id"]] = 1 / (60 + offset + rank)
                versions[row["asset_id"]] = row["version"]
                matches[row['asset_id']] = row['unit_id']
        terms = re.findall(r"[\w]+", query, flags=re.UNICODE)[:32]
        if query and vector is not None and hasattr(self, "contexts") and self.contexts.count_rows():
            for rank, row in enumerate(self.contexts.search(np.asarray(vector, dtype=np.float32)).metric("cosine").where(where, prefilter=True)
                                      .select(["asset_id", "version", "_distance"]).limit(limit).offset(offset).to_list()):
                scores[row["asset_id"]] = scores.get(row["asset_id"], 0) + 1 / (60 + offset + rank)
                versions[row["asset_id"]] = row["version"]
        if terms and any("search_text" in idx.columns for idx in self.table.list_indices()):
            # Query syntax is generated from literal tokens, never passed through as executable grammar.
            text = " ".join(terms)
            text_seen = set()
            for rank, row in enumerate(self.table.search(text, query_type="fts", fts_columns="search_text").where(where, prefilter=True).select(["unit_id", "asset_id", "version", "_score"]).limit(limit).offset(offset).to_list()):
                moments.setdefault(row['asset_id'], []).append(row['unit_id'])
                if row['asset_id'] in text_seen:
                    continue
                text_seen.add(row['asset_id'])
                scores[row["asset_id"]] = scores.get(row["asset_id"], 0) + 1 / (60 + offset + rank)
                versions[row["asset_id"]] = row["version"]
                matches.setdefault(row['asset_id'], row['unit_id'])
        if query:
            names = self.catalog.name_matches(query, filters, limit=limit, offset=offset)
            for rank, row in enumerate(names):
                scores[row["id"]] = scores.get(row["id"], 0) + 2 / (60 + offset + rank)
                versions[row["id"]] = row["version"]
        result = []
        ordered = sorted(scores, key=lambda key: (-scores[key], key))
        for asset_id in ordered if merge_all else ordered[:limit]:
            asset = self.catalog.get(asset_id)
            if asset and asset["version"] == versions[asset_id] and asset["present"]:
                asset = self.catalog.media_units.asset_at(asset, matches.get(asset_id))
                asset["rank_score"] = scores[asset_id]
                asset['matched_units'] = list(dict.fromkeys(moments.get(asset_id, [])))
                result.append(asset)
        return result

    def face_where(self, filters, excluded=()):
        where = self.filter_where(filters)
        where += " AND model_version='" + FACE_VERSION.replace("'", "''") + "'"
        if excluded:
            where += " AND unit_id NOT IN (" + ",".join("'" + key.replace("'", "''") + "'" for key in excluded) + ")"
        return where

    def face_hits(self, vector, filters, offset=0, limit=200, threshold=.363, excluded=()):
        self.collect_flush(wait=True)
        where = self.face_where(filters, excluded)
        rows = self.faces.search(np.asarray(vector, dtype=np.float32)).metric("cosine").bypass_vector_index().where(where, prefilter=True)
        rows = rows.select(["unit_id", "asset_id", "version", "_distance"]).offset(offset).limit(limit).to_list()
        hits = []
        for row in rows:
            score = min(1., max(-1., 1. - row["_distance"]))
            if score < threshold:
                continue
            hits.append(row | {"face_score": score})
        more = len(rows) == limit and 1. - rows[-1]["_distance"] >= threshold
        return hits, more

    def face_candidates(self, vector, filters, offset=0, limit=200, threshold=.363):
        rows, more = self.face_hits(vector, filters, offset, limit, threshold)
        items = []
        for row in rows:
            asset = self.catalog.get(row["asset_id"])
            if asset and asset['present'] and asset["version"] == row["version"]:
                items.append(asset | {"face_score": row["face_score"], "face_match_id": row["unit_id"]})
        return items, more
