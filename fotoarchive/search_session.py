"""A disk-backed search session: only one gallery page and 40 checks enter memory."""
from __future__ import annotations

import json


class SearchSession:
    PAGE_SIZE = 200

    def __init__(self, catalog, index, request_id, filters, query, vector, conditions, exclude_id=None, mode="semantic", excluded_faces=(), presentation=None, face_groups=None, people_mode='all'):
        self.catalog, self.index = catalog, index
        self.request_id, self.filters = request_id, filters
        self.query, self.vector, self.conditions = query, vector, conditions
        self.exclude_id = exclude_id
        self.mode = mode
        self.face_groups = face_groups or []
        self.people_mode = people_mode
        self.presentation = None
        self.presentation_options = presentation or {}
        self.face_stream = None
        if mode == "face":
            import numpy as np
            from .face_search import FaceSearchStream
            self.face_stream = FaceSearchStream(index, list(np.atleast_2d(vector)), filters, excluded_faces)
        self.ann = mode == "semantic" and vector is not None and bool(catalog.state("ann_rows", 0))
        self.exact_phase = False
        self.stream_offset = 0
        self.ordinal = 0
        self.exhausted = False
        self.view_offset = 0
        self.view_verdict = ""
        self.total = 0 if mode == "browse" else catalog.browse(filters, limit=0)[1]
        # SQLite's temporary tables spill to disk; no full-archive list lives in Python or Qt.
        catalog.db.execute("PRAGMA temp_store=FILE")
        catalog.db.executescript("""
            DROP TABLE IF EXISTS temp.active_search;
            CREATE TEMP TABLE active_search(
              asset_id INTEGER PRIMARY KEY, file_version INTEGER NOT NULL,
              ordinal INTEGER NOT NULL, verdict TEXT, result_json TEXT, face_score REAL, face_id TEXT,unit_id TEXT);
            CREATE INDEX IF NOT EXISTS active_search_order ON active_search(ordinal);
            CREATE INDEX IF NOT EXISTS active_search_verdict ON active_search(verdict,ordinal);
            DELETE FROM active_search;
            DROP TABLE IF EXISTS temp.search_moments;
            CREATE TEMP TABLE search_moments(unit_id TEXT PRIMARY KEY,asset_id INTEGER NOT NULL);
            CREATE INDEX search_moments_asset ON search_moments(asset_id);
        """)
        if mode == "browse":
            # Freeze membership and ordering on disk, not in Qt or Python.
            # New photos cannot move the boundary of an already viewed page.
            where, params = filters.expression()
            with catalog.db:
                from .presentation import SORTS
                order = SORTS.get(self.presentation_options.get('sort'), SORTS['newest'])
                catalog.db.execute(f"""INSERT INTO active_search(asset_id,file_version,ordinal)
                    SELECT id,version,row_number() OVER (ORDER BY {order})-1
                    FROM assets WHERE {where}""", params)
            self.total = catalog.db.execute("SELECT count(*) FROM active_search").fetchone()[0]
            self.metadata_count = catalog.db.execute(
                "SELECT count(*) FROM assets WHERE present=1 AND metadata_ready=1").fetchone()[0]
            self.exhausted = True
        else:
            self.load_more()
        if self.presentation_options.get('stacks'):
            from .presentation import Presentation
            self.presentation = Presentation(self, self.presentation_options)

    def configure_presentation(self, options):
        self.presentation_options = options
        if options.get('stacks'):
            from .presentation import Presentation
            if self.presentation is None:
                self.presentation = Presentation(self, options)
            else:
                self.presentation.configure(options)
        else:
            self.presentation = None

    def load_more(self):
        if self.exhausted:
            return
        if self.mode == "face":
            hits, more = self.face_stream.next_batch(self.PAGE_SIZE)
            items = []
            for hit in hits:
                # The global score order makes the first hit for a photograph its best
                # match, including when multiple examples or multiple faces match it.
                asset = self.catalog.db.execute("SELECT id,version FROM assets WHERE id=? AND present=1", (hit["asset_id"],)).fetchone()
                if asset and asset["version"] == hit["version"]:
                    face = self.catalog.face_info(hit['unit_id']) or {}
                    if self.face_groups:
                        from .face_search import matches_people
                        if not matches_people(self.catalog, asset['id'], face.get('unit_id'), self.face_groups, self.people_mode=='all'):
                            continue
                    items.append(dict(asset) | {"face_score": hit["face_score"], "face_match_id": hit["unit_id"], 'unit_id': face.get('unit_id')})
            self.exhausted = not more
        elif self.vector is None:
            items, _ = self.catalog.browse(self.filters, offset=self.stream_offset, limit=self.PAGE_SIZE)
        else:
            # Keep the whole union of this bounded chunk so fusion never drops an unseen candidate.
            items = self.index.candidates(self.vector, self.query, self.filters, limit=self.PAGE_SIZE,
                                          offset=self.stream_offset, merge_all=True, **({"exact": True} if self.exact_phase else {}))
        self.stream_offset += self.PAGE_SIZE
        if self.mode != "face":
            # Video frames outnumber files. Paging must exhaust the vector stream,
            # not stop when its offset reaches the number of source files.
            self.exhausted = not items
            if self.ann and not self.exact_phase and (self.stream_offset >= 1000 or self.exhausted):
                # ANN accelerates the first screen but does not enumerate every partition.
                # Continue with the exact stream and SQLite deduplication so deep scrolling
                # cannot silently truncate a large archive to the ANN candidate pool.
                self.exact_phase = True
                self.stream_offset = 0
                self.exhausted = False
        with self.catalog.db:
            for asset in items:
                if asset["id"] == self.exclude_id:
                    continue
                self.catalog.db.execute("INSERT OR IGNORE INTO active_search VALUES(?,?,?,NULL,NULL,?,?,?)",
                                        (asset["id"], asset["version"], self.ordinal, asset.get("face_score"), asset.get("face_match_id"), asset.get('unit_id')))
                self.ordinal += 1
                for unit in asset.get('matched_units', []) or [asset.get('unit_id')]:
                    if unit:
                        self.catalog.db.execute('INSERT OR IGNORE INTO search_moments VALUES(?,?)', (unit, asset['id']))

    def restore_position(self, anchor):
        if self.presentation:
            return self.presentation.restore(anchor)
        def position(asset_id):
            row = self.catalog.db.execute('SELECT ordinal FROM active_search WHERE asset_id=?', (asset_id,)).fetchone()
            return row[0] if row else None
        row = position(anchor.get('asset_id'))
        if row is None:
            row = min(max(0, anchor.get('row', 0)), max(0, self.total - 1))
        selected = position(anchor.get('selected_id'))
        return {'row': row, 'selected_row': row if selected is None else selected,
                'offset_y': anchor.get('offset_y', 0),
                'loaded_count': min(self.total, max(row + 1, anchor.get('loaded_count', 0)))}

    def pending(self, limit=40):
        while True:
            rows = self.catalog.db.execute("""SELECT a.*,s.unit_id FROM active_search s JOIN assets a ON a.id=s.asset_id
                WHERE s.verdict IS NULL AND s.file_version=a.version AND a.present=1
                ORDER BY s.ordinal LIMIT ?""", (limit,)).fetchall()
            if rows or self.exhausted:
                return [self.catalog.media_units.asset_at(dict(row), row['unit_id']) for row in rows]
            self.load_more()

    def mark(self, asset, result):
        with self.catalog.db:
            self.catalog.db.execute("UPDATE active_search SET verdict=?,result_json=? WHERE asset_id=? AND file_version=?",
                                    (result["verdict"], json.dumps(result, ensure_ascii=False), asset["id"], asset["version"]))

    def counts(self):
        if self.mode == "browse":
            return {"candidates": self.total, "checked": 0}
        return dict(self.catalog.db.execute("SELECT count(*) candidates,count(verdict) checked FROM active_search").fetchone())

    def groups(self):
        result = {"": 0, "pending": 0, "yes": 0, "no": 0, "uncertain": 0}
        for verdict, count in self.catalog.db.execute("SELECT verdict,count(*) FROM active_search GROUP BY verdict"):
            result[verdict or "pending"] = count
            result[""] += count
        return result

    def can_verify(self):
        return not self.exhausted or self.catalog.db.execute("SELECT 1 FROM active_search WHERE verdict IS NULL LIMIT 1").fetchone() is not None

    def page(self, offset=None, verdict=None, limit=200, expand=False):
        if self.presentation:
            if offset is not None:
                self.view_offset = max(0, offset)
            if verdict is not None:
                self.view_verdict = verdict
            result = self.presentation.page(self.view_offset, self.view_verdict, limit, expand)
        else:
            result = self.raw_page(offset, verdict, limit, expand)
        for asset in result['items']:
            if asset.get('media_kind') == 'video' and self.mode != 'browse':
                asset['matched_moments'] = [dict(row) for row in self.catalog.db.execute('''SELECT u.id unit_id,u.timestamp_ms,d.thumbnail
                    FROM search_moments m JOIN units u ON u.id=m.unit_id JOIN unit_details d ON d.unit_id=u.id
                    WHERE m.asset_id=? AND d.file_version=? ORDER BY u.timestamp_ms LIMIT 12''', (asset['id'], asset['version']))]
                asset['moment_count'] = self.catalog.db.execute('SELECT count(*) FROM search_moments WHERE asset_id=?', (asset['id'],)).fetchone()[0]
        return result

    def raw_page(self, offset=None, verdict=None, limit=200, expand=False):
        if offset is not None:
            self.view_offset = max(0, offset)
        if verdict is not None:
            self.view_verdict = verdict
        if self.mode == "browse":
            # Drive the join from the frozen order. Starting with assets makes
            # SQLite read/sort full metadata for the entire archive for every page.
            items = [self.catalog.media_units.asset_at(dict(row)) for row in self.catalog.db.execute("""SELECT a.*
                FROM active_search s INDEXED BY active_search_order CROSS JOIN assets a ON a.id=s.asset_id
                WHERE a.present=1 AND a.metadata_ready=1
                ORDER BY s.ordinal LIMIT ? OFFSET ?""", (limit, self.view_offset))]
            return {"items": items, "offset": self.view_offset, "page_total": self.total,
                    "metadata_count": self.metadata_count, "verdict": "", "has_more": False}
        params = []
        where = "s.file_version=a.version AND a.present=1"
        if self.view_verdict == "pending":
            where += " AND s.verdict IS NULL"
        elif self.view_verdict:
            where += " AND s.verdict=?"
            params.append(self.view_verdict)
        sql = " FROM active_search s JOIN assets a ON a.id=s.asset_id WHERE " + where
        total = self.catalog.db.execute("SELECT count(*)" + sql, params).fetchone()[0]
        if expand and self.view_verdict in ("", "pending"):
            # Sparse face/AND matches must not monopolize the model coordinator.
            # A page can be short; has_more lets scrolling continue the stream.
            for _ in range(4):
                if total >= self.view_offset + limit or self.exhausted:
                    break
                self.load_more()
                total = self.catalog.db.execute("SELECT count(*)" + sql, params).fetchone()[0]
        items = []
        for row in self.catalog.db.execute("SELECT a.*,s.result_json,s.face_score,s.face_id face_match_id,s.unit_id" + sql + " ORDER BY s.ordinal LIMIT ? OFFSET ?",
                                            params + [limit, self.view_offset]):
            asset = self.catalog.media_units.asset_at(dict(row), row['unit_id'])
            result = asset.pop("result_json")
            if result:
                asset["verification"] = json.loads(result)
            items.append(asset)
        return {"items": items, "offset": self.view_offset, "page_total": total, "verdict": self.view_verdict,
                "has_more": not self.exhausted and self.view_verdict in ("", "pending")}
