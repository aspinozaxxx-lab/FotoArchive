"""Vector-only scale check on synthetic faces; no archive images are opened."""
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import numpy as np
import pyarrow as pa
import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fotoarchive.catalog import Filters
from fotoarchive.config import Settings, FACE_VERSION
from fotoarchive.face_search import FaceSearchStream, boundary_suggestions
from fotoarchive.search import SearchIndex, FACE_SCHEMA


class SyntheticCatalog:
    def __init__(self, data_dir):
        self.cfg = SimpleNamespace(data_dir=data_dir)

    def face_info(self, face_id):
        return {"id":face_id,"asset_id":int(face_id),"file_version":1}

    def get(self, asset_id):
        return {"id":asset_id,"version":1}


def main():
    output = Settings.load().data_dir / "reports/scale_faces_v03"
    output.mkdir(parents=True, exist_ok=True)
    index = SearchIndex(SyntheticCatalog(output))
    count = 200000
    if index.faces.count_rows() != count:
        if index.faces.count_rows():
            raise RuntimeError("Incomplete synthetic fixture; choose a new benchmark directory")
        random = np.random.default_rng(303)
        for start in range(0, count, 4000):
            n = min(4000, count-start)
            vectors = random.normal(size=(n,128)).astype(np.float32)
            scores = random.uniform(.25,.5,n).astype(np.float32)
            vectors[:,1:] /= np.linalg.norm(vectors[:,1:],axis=1,keepdims=True)
            vectors[:,1:] *= np.sqrt(1-scores*scores)[:,None]
            vectors[:,0] = scores
            base = {"unit_id":[str(i) for i in range(start,start+n)], "asset_id":list(range(start,start+n)),
                    "version":1,"model_version":FACE_VERSION,"folder":"synthetic","captured_at":"2003-05-01",
                    "camera":"test","extension":".jpg","orientation":"landscape","width":640,"height":480,
                    "pixels":307200,"size":1,"present":1,"metadata_ready":1,"search_text":""}
            arrays = []
            for field in FACE_SCHEMA:
                if field.name == "vector":
                    arrays.append(pa.FixedSizeListArray.from_arrays(pa.array(vectors.ravel()),128))
                else:
                    value = base[field.name]
                    arrays.append(pa.array(value if isinstance(value,list) else [value]*n, type=field.type))
            index.faces.add(pa.Table.from_arrays(arrays,schema=FACE_SCHEMA))
    references = np.eye(128,dtype=np.float32)[:2]
    timings, pages = [], []
    rss_start = psutil.Process().memory_info().rss
    rss_peak = rss_start
    batches = 0
    for run in range(6):
        begin = time.perf_counter()
        iterator = boundary_suggestions(index,references,Filters())
        while True:
            try:
                next(iterator)
                batches += 1
                rss_peak = max(rss_peak,psutil.Process().memory_info().rss)
            except StopIteration as finished:
                assert len(finished.value) == 6
                break
        timings.append(time.perf_counter()-begin)
        begin = time.perf_counter()
        stream = FaceSearchStream(index,references,Filters())
        hits, more = stream.next_batch(200)
        assert len(hits) == 200 and more
        pages.append(time.perf_counter()-begin)
    result = {"synthetic_faces":count,"examples":len(references),"suggestion_seconds":timings,
              "suggestion_warm_p95":float(np.percentile(timings[1:],95)),
              "first_200_hits_seconds":pages,"first_200_warm_p95":float(np.percentile(pages[1:],95)),
              "scan_batches_per_run":batches//6,"rss_extra_peak_mb":(rss_peak-rss_start)/1024**2,
              "scope":"LanceDB vectors and bounded merge; excludes SQLite materialisation and UI/image decoding"}
    (output / "result.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
    print(json.dumps(result,indent=2))


if __name__ == "__main__":
    main()
