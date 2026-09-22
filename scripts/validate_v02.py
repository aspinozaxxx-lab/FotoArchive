"""Real worker acceptance: exhaustive paging, local face search and source integrity."""
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import queue
import sqlite3
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fotoarchive.config import Settings
from fotoarchive.engine import worker_main


def main():
    cfg = Settings.load()
    context = mp.get_context("spawn")
    commands, events, shutdown = context.Queue(64), context.Queue(256), context.Event()
    process = context.Process(target=worker_main, args=(str(cfg.data_dir), commands, events, shutdown))
    process.start()
    report = {"passed": False}
    start = time.perf_counter()

    def receive(kind, request=None):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                event = events.get(timeout=1)
            except queue.Empty:
                if not process.is_alive():
                    raise RuntimeError("Worker stopped")
                continue
            if event["type"] in {"error", "fatal"}:
                raise RuntimeError(event)
            if event["type"] == kind and (request is None or event.get("id") == request):
                return event
        raise TimeoutError(kind)

    def collect(action, request, **extra):
        commands.put({"action": action, "id": request, **extra})
        response = receive("results", request)
        ids = [a["id"] for a in response["items"]]
        top = response["items"][:20]
        calls = 1
        while len(ids) < response["page_total"] or response["has_more"]:
            commands.put({"action":"search_page", "id":request, "view":17, "offset":len(ids), "verdict":""})
            response = receive("search_page", request)
            assert response["view"] == 17
            assert len(response["items"]) <= 200
            ids.extend(a["id"] for a in response["items"])
            calls += 1
            assert calls < 200
        assert len(ids) == len(set(ids))
        return ids, top, calls

    try:
        receive("ready")
        commands.put({"action":"pause"})
        browse, _, pages = collect("browse", 1, filters={})
        report["browse"] = {"photos":len(browse), "pages":pages}
        semantic, _, pages = collect("search", 2, query="люди на природе", filters={})
        assert set(semantic) == set(browse), "Semantic paging omitted indexed photos"
        report["semantic"] = {"photos":len(semantic), "pages":pages}
        with sqlite3.connect(cfg.data_dir / "catalog.sqlite3") as db:
            row = db.execute("SELECT f.id,f.asset_id FROM faces f JOIN assets a ON a.id=f.asset_id WHERE a.relative_path='2003/08_08_03/007.JPG' ORDER BY f.id LIMIT 1").fetchone()
            if row is None:
                row = db.execute("SELECT id,asset_id FROM faces ORDER BY id LIMIT 1").fetchone()
            face_id, asset_id = row
        commands.put({"action":"faces", "asset_id":asset_id})
        listed = receive("face_list")
        assert any(f["id"] == face_id for f in listed["faces"])
        matches, top, pages = collect("face_search", 3, face_id=face_id, filters={})
        assert asset_id in matches
        assert all(a["face_score"] >= .363 for a in top)
        assert all(top[i]["face_score"] >= top[i+1]["face_score"] for i in range(len(top)-1))
        report["faces"] = {"source_face":face_id, "photos":len(matches), "pages":pages,
                           "top20":[{k:a[k] for k in ("id", "face_score", "face_match_id", "thumbnail")} for a in top]}
        missing, _, _ = collect("face_search", 4, face_id=face_id, filters={"folder":"not-present"})
        assert missing == []
        unknown, _, _ = collect("browse", 5, filters={"folder":"2003", "unknown_date":True})
        year, _, _ = collect("browse", 6, filters={"folder":"2003", "date_from":"2002-01-01", "date_to":"2002-12-31"})
        assert len(unknown) == 32 and len(year) == 23
        report["date_filters"] = {"2003_unknown":len(unknown), "2003_with_2002_exif":len(year)}
        commands.put({"action":"scan"})
        scan = receive("scan_done")
        commands.put({"action":"pause"})
        assert scan["scan"]["changed"] == 0
        report["rescan"] = scan["scan"]
        report["passed"] = True
    finally:
        shutdown.set()
        process.join(30)
        if process.is_alive():
            process.terminate(); process.join(5)
        report["worker_seconds"] = time.perf_counter() - start
        report["worker_exitcode"] = process.exitcode
        (cfg.data_dir / "reports/v02_flow.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    before = json.loads((cfg.data_dir / "reports/v02_before.json").read_text(encoding="utf-8"))
    for asset in before["originals"].values():
        with Path(asset["path"]).open("rb") as stream:
            assert hashlib.file_digest(stream,"sha256").hexdigest() == asset["sha256"], asset["path"]
    vectors = hashlib.sha256()
    with sqlite3.connect(cfg.data_dir / "catalog.sqlite3") as db:
        for unit_id, blob in db.execute("SELECT unit_id,vector FROM embeddings ORDER BY unit_id"):
            vectors.update(unit_id.encode()); vectors.update(blob)
        assert vectors.hexdigest() == before["visual_embeddings_sha256"]
        assert all(before["originals"][str(i)]["version"] == v for i,v in db.execute("SELECT id,version FROM assets"))
    report["unchanged_originals"] = len(before["originals"])
    report["visual_embeddings_sha256"] = vectors.hexdigest()
    (cfg.data_dir / "reports/v02_flow.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k != "faces"}, ensure_ascii=True,indent=2))
    print("Face matches:",report["faces"]["photos"])


if __name__ == "__main__":
    mp.freeze_support()
    main()
