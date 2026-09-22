"""Merge exact face-neighbour streams, retaining the best match to any example."""
from collections import deque
import heapq

import numpy as np


FACE_THRESHOLD = .363


def matches_people(catalog, asset_id, unit_id, groups, require_all=True):
    """All selected identities must match distinct faces in the same frame."""
    from .config import FACE_VERSION
    faces = [(row['id'],np.frombuffer(row['vector'],dtype=np.float32)) for row in catalog.db.execute('''
      SELECT f.id,f.vector FROM faces f JOIN assets a ON a.id=f.asset_id WHERE a.id=? AND a.present=1
      AND f.file_version=a.version AND f.model_version=? AND (? IS NULL OR f.id IN
        (SELECT face_id FROM face_units WHERE unit_id=?))''',(asset_id,FACE_VERSION,unit_id,unit_id))]
    choices = []
    for group in groups:
        rejected = set(group.get('rejected', []))
        matching = []
        for key,vector in faces:
            if key in rejected:
                continue
            if vector is not None and max(float(vector @ example) for example in group['vectors']) >= FACE_THRESHOLD:
                matching.append(key)
        if matching and not require_all:
            return True
        if not matching and require_all:
            return False
        choices.append(matching)
    def assign(index, used):
        return index == len(choices) or any(assign(index+1, used | {key}) for key in choices[index] if key not in used)
    return assign(0, set()) if require_all else False


class FaceSearchStream:
    BATCH_SIZE = 64

    def __init__(self, index, vectors, filters, excluded=()):
        self.index, self.filters = index, filters
        self.excluded = excluded
        self.vectors = vectors
        self.offsets = [0] * len(vectors)
        self.buffers = [deque() for _ in vectors]
        self.more = [True] * len(vectors)
        self.heap = []
        for i in range(len(vectors)):
            self.advance(i)

    def advance(self, i):
        while not self.buffers[i] and self.more[i]:
            rows, self.more[i] = self.index.face_hits(self.vectors[i], self.filters,
                                                    offset=self.offsets[i], limit=self.BATCH_SIZE,
                                                    excluded=self.excluded)
            self.offsets[i] += self.BATCH_SIZE
            self.buffers[i].extend(rows)
        if self.buffers[i]:
            row = self.buffers[i].popleft()
            heapq.heappush(self.heap, (-row["face_score"], row["asset_id"], row["unit_id"], i, row))

    def next_batch(self, limit=200):
        hits = []
        while self.heap and len(hits) < limit:
            *_, i, row = heapq.heappop(self.heap)
            hits.append(row)
            self.advance(i)
        return hits, bool(self.heap)


class InvalidFaceExamples(ValueError):
    def __init__(self, face_ids):
        self.invalid_face_ids = face_ids
        super().__init__("Некоторые примеры лица изменились или недоступны. Удалите отмеченные примеры в панели поиска и выберите их заново.")


def resolve_examples(catalog, face_ids):
    if not isinstance(face_ids, list) or not face_ids or any(not isinstance(i, str) for i in face_ids):
        raise ValueError("Выберите хотя бы один пример лица.")
    face_ids = list(dict.fromkeys(face_ids))
    examples, vectors, missing = [], [], []
    for face_id in face_ids:
        info = catalog.face_info(face_id)
        vector = catalog.face_vector(face_id)
        if info is None or vector is None:
            missing.append(face_id)
        else:
            examples.append(info)
            vectors.append(vector)
    if missing:
        raise InvalidFaceExamples(missing)
    return examples, vectors


def boundary_suggestions(index, vectors, filters, excluded=(), limit=6):
    """Yield between bounded scan batches, then return diverse threshold neighbours.

    Scores are the maximum over the confirmed examples, as in the main filter.
    Separate pools retain both sides of the boundary without materialising the
    archive. The worker can service searches or cancel this iterator between batches.
    """
    excluded = set(excluded)
    pools = [[], []]
    pool_size = 128
    query = (index.faces.search().where(index.face_where(filters)).limit(None)
             .select(["unit_id", "asset_id", "version", "vector"]))
    reader = query.to_batches(batch_size=2048)
    try:
        for batch in reader:
            matrix = batch.column("vector").flatten().to_numpy(zero_copy_only=False).reshape(-1, 128)
            scores = np.full(len(matrix), -1., dtype=np.float32)
            for vector in vectors:
                np.maximum(scores, matrix @ vector, out=scores)
            rows = batch.select(["unit_id", "asset_id", "version"]).to_pylist()
            for i in np.flatnonzero((scores >= .28) & (scores <= .48)):
                row = rows[i]
                if row["unit_id"] in excluded:
                    continue
                score = float(scores[i])
                side = int(score >= FACE_THRESHOLD)
                entry = (-abs(score - FACE_THRESHOLD), row["unit_id"], row, score, matrix[i].copy())
                if len(pools[side]) < pool_size:
                    heapq.heappush(pools[side], entry)
                elif entry[:2] > pools[side][0][:2]:
                    heapq.heapreplace(pools[side], entry)
            yield
    finally:
        reader.close()
    ordered = [sorted(pool, key=lambda entry: (-entry[0], entry[1])) for pool in pools]
    chosen, used_photos, chosen_vectors = [], set(), []

    def choose(entry):
        _, face_id, row, score, vector = entry
        if row["asset_id"] in used_photos or any(vector @ previous > .95 for previous in chosen_vectors):
            return False
        face = index.catalog.face_info(face_id)
        if face is None or face["file_version"] != row["version"]:
            return False
        asset = index.catalog.get(row["asset_id"])
        asset = index.catalog.media_units.asset_at(asset, face.get('unit_id'))
        chosen.append(face | {"face_score": score, "inside_filter": score >= FACE_THRESHOLD, "asset": asset})
        used_photos.add(row["asset_id"])
        chosen_vectors.append(vector)
        return True

    # Half from each side when available, then fill any unused places.
    for pool in ordered:
        count = 0
        for entry in pool:
            if count >= limit // 2:
                break
            count += int(choose(entry))
    for entry in sorted(ordered[0] + ordered[1], key=lambda entry: (-entry[0], entry[1])):
        if len(chosen) >= limit:
            break
        choose(entry)
    return sorted(chosen, key=lambda face: (not face["inside_filter"], abs(face["face_score"] - FACE_THRESHOLD)))
