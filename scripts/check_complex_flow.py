"""Offline acceptance of real multiprocessing, query parsing, batches, cancel and filtering."""
import json
import multiprocessing as mp
import os
import queue
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fotoarchive.config import Settings
from fotoarchive.engine import worker_main


def main():
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HTTP_PROXY"] = os.environ["HTTPS_PROXY"] = "http://127.0.0.1:9"
    cfg = Settings.load()
    context = mp.get_context("spawn")
    commands, events, shutdown = context.Queue(64), context.Queue(256), context.Event()
    process = context.Process(target=worker_main, args=(str(cfg.data_dir), commands, events, shutdown))
    process.start()
    report = {"passed": False, "offline_environment": True}
    started = time.monotonic()

    def receive(kind, request=None, timeout=180):
        deadline = time.monotonic() + timeout
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

    try:
        receive("ready")
        commands.put({"action": "pause"})
        commands.put({"action": "search", "id": 1, "complex": True,
                      "query": "три человека за столом без автомобиля в кадре", "filters": {"folder": "not-present"}})
        parsed = receive("results", 1)
        report["parsed_conditions"] = parsed["conditions"]
        assert any("три" in value.lower() or "3" in value for value in parsed["conditions"])
        assert any("автомоб" in value.lower() for value in parsed["conditions"])
        assert parsed["items"] == []
        print(json.dumps({"phase": "parsed", "conditions": parsed["conditions"]}, ensure_ascii=True), flush=True)
        commands.put({"action": "search", "id": 2, "complex": True,
                      "query": "На фотографии нет автомобиля", "filters": {"folder": "2003"}})
        initial = receive("results", 2)
        report["initial_results_seconds"] = time.monotonic() - started
        assert initial["items"] and len(initial["items"]) <= 200
        first = receive("verification_done", 2)
        assert first["checked"] == 40 and not first["exhausted"]
        report["first_batch"] = first
        print(json.dumps({"phase": "first_batch", "checked": first["checked"]}), flush=True)
        commands.put({"action": "verify_more", "id": 2})
        next_result = receive("verified", 2)
        assert next_result["checked"] == 41
        commands.put({"action": "cancel", "id": 2})
        commands.put({"action": "search_page", "id": 2, "offset": 0, "verdict": "yes"})
        page = receive("search_page", 2)
        assert 41 <= page["checked"] <= 42
        assert all(a["verification"]["verdict"] == "yes" for a in page["items"])
        report["checked_when_cancelled"] = page["checked"]
        report["confirmed_results"] = page["page_total"]
        commands.put({"action": "browse", "id": 3, "filters": {"folder": "2003", "unknown_date": True}})
        assert receive("results", 3)["total"] == 32
        commands.put({"action": "browse", "id": 4, "filters": {"folder": "2003", "date_from": "2002-01-01", "date_to": "2002-12-31"}})
        assert receive("results", 4)["total"] == 23
        report["date_filters"] = True
        report["passed"] = True
    finally:
        shutdown.set()
        process.join(timeout=30)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        report["elapsed_seconds"] = time.monotonic() - started
        (cfg.data_dir / "reports/complex_flow.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2), flush=True)


if __name__ == "__main__":
    mp.freeze_support()
    main()
