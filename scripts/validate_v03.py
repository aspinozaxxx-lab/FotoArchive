"""Exercise multi-example face searches through the real, bounded worker protocol."""
import hashlib
import json
import multiprocessing as mp
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
    previous = json.loads((cfg.data_dir / "reports/v02_flow.json").read_text(encoding="utf-8"))
    examples = [previous["faces"]["source_face"], previous["faces"]["top20"][1]["face_match_id"]]
    runtime = mp.get_context("spawn")
    commands, events, shutdown = runtime.Queue(64), runtime.Queue(256), runtime.Event()
    worker = runtime.Process(target=worker_main, args=(str(cfg.data_dir), commands, events, shutdown))
    worker.start()
    report = {"passed": False, "examples": examples}

    def receive(kind, request=None):
        deadline = time.monotonic()+60
        while time.monotonic() < deadline:
            try:
                event = events.get(timeout=1)
            except queue.Empty:
                if not worker.is_alive():
                    raise RuntimeError("Worker exited")
                continue
            if event["type"] in {"error","fatal"}:
                raise RuntimeError(event)
            if event["type"] == kind and (request is None or event.get("id") == request):
                return event
        raise TimeoutError(kind)

    def search(request, face_ids, filters=None, excluded=()):
        start = time.perf_counter()
        commands.put({"action":"face_search","id":request,"face_ids":face_ids,"filters":filters or {},"excluded_faces":list(excluded)})
        response = receive("results",request)
        result = response["items"]
        first_seconds = time.perf_counter()-start
        assert [r["id"] for r in response["examples"]] == list(dict.fromkeys(face_ids))
        while len(result) < response["page_total"] or response["has_more"]:
            commands.put({"action":"search_page","id":request,"view":3,"offset":len(result),"verdict":""})
            response = receive("search_page",request)
            assert len(response["items"]) <= 200
            result += response["items"]
        assert len(result) == len({a["id"] for a in result})
        assert all(result[i]["face_score"] >= result[i+1]["face_score"] for i in range(len(result)-1))
        return {a["id"]:a["face_score"] for a in result}, first_seconds

    try:
        receive("ready")
        a, seconds_a = search(1, examples[:1])
        b, seconds_b = search(2, examples[1:])
        both, seconds_both = search(3, examples + examples)
        assert set(both) == set(a) | set(b)
        for asset_id, score in both.items():
            assert abs(score-max(a.get(asset_id,-1),b.get(asset_id,-1))) < 1e-6
        started = time.perf_counter()
        commands.put({"action":"face_suggestions","id":"review1","face_ids":examples,"filters":{},"excluded":[]})
        suggestions = receive("face_suggestions","review1")["items"]
        suggestion_seconds = time.perf_counter()-started
        assert len(suggestions) == 6 and {face["inside_filter"] for face in suggestions} == {True, False}
        assert len({face["asset_id"] for face in suggestions}) == len(suggestions)
        assert not set(examples) & {face["id"] for face in suggestions}
        # Reviewing must leave the existing paginated search usable.
        commands.put({"action":"search_page","id":3,"view":3,"offset":200,"verdict":""})
        existing_page = receive("search_page",3)
        assert {row["id"] for row in existing_page["items"]} <= set(both)
        excluded = [face["id"] for face in suggestions]
        commands.put({"action":"face_suggestions","id":"review2","face_ids":examples,"filters":{},"excluded":excluded})
        following = receive("face_suggestions","review2")["items"]
        assert not set(excluded) & {face["id"] for face in following}
        rejected, _ = search(6, examples, excluded=excluded)
        assert set(rejected) <= set(both)
        filtered, _ = search(4, examples, {"folder":"2003"})
        with sqlite3.connect(cfg.data_dir / "catalog.sqlite3") as database:
            in_folder = {r[0] for r in database.execute("SELECT id FROM assets WHERE folder='2003' OR folder LIKE '2003/%'")}
        assert set(filtered) == set(both) & in_folder
        commands.put({"action":"browse","id":5,"filters":{}})
        cleared = receive("results",5)
        assert cleared["mode"] == "browse" and not cleared["examples"]
        report.update(passed=True, first_example_matches=len(a), second_example_matches=len(b),
                      combined_matches=len(both), extra_vs_first=len(set(both)-set(a)), filtered_matches=len(filtered),
                      first_page_seconds={"one_a":seconds_a,"one_b":seconds_b,"two":seconds_both},
                      suggestions_seconds=suggestion_seconds,
                      suggestions=[{key:face[key] for key in ("id","asset_id","face_score","inside_filter")} for face in suggestions],
                      next_suggestions=[face["id"] for face in following],
                      rejected_face_matches=len(rejected), review_preserves_search_session=True,
                      reset_catalog_total=cleared["total"])
    finally:
        shutdown.set()
        worker.join(20)
        if worker.is_alive():
            worker.terminate(); worker.join(5)
        report["worker_exitcode"] = worker.exitcode
        before = json.loads((cfg.data_dir / "reports/v03_before.json").read_text())
        with sqlite3.connect(cfg.data_dir / "catalog.sqlite3") as database:
            for name,sql in (("visual","SELECT unit_id,vector FROM embeddings ORDER BY unit_id"),
                             ("face","SELECT id,vector FROM faces ORDER BY id")):
                digest = hashlib.sha256()
                for key,blob in database.execute(sql):
                    digest.update(key.encode()); digest.update(blob)
                report[name+"_unchanged"] = digest.hexdigest() == before[name+"_sha256"]
                assert report[name+"_unchanged"]
        (cfg.data_dir / "reports/v03_flow.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=True,indent=2))


if __name__ == "__main__":
    mp.freeze_support()
    main()
