import json
import os
import subprocess
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fotoarchive.config import Settings
from fotoarchive.inference import Embedder, VisionLanguage

cfg = Settings.load()
cfg.initialize()
image = Path(r"F:\MyFoto\2003\08_08_03\007.JPG")
embedder = Embedder(cfg, profile=True)
start = time.perf_counter()
vec = embedder.image(image)
report = {"image_cold_seconds": time.perf_counter() - start, "embedding_dimensions": len(vec)}
start = time.perf_counter()
embedder.text("люди на улице")
report["text_cold_seconds"] = time.perf_counter() - start
start = time.perf_counter()
for _ in range(3):
    embedder.image(image)
report["image_warm_seconds"] = (time.perf_counter() - start) / 3
start = time.perf_counter()
for _ in range(3):
    embedder.text("люди на улице")
report["text_warm_seconds"] = (time.perf_counter() - start) / 3
report["directml"] = embedder.finish_profile()
assert report["directml"]["provider_events"].get("DmlExecutionProvider", 0) > 0
print(json.dumps(report, ensure_ascii=True, indent=2), flush=True)
if "--embedding-only" not in sys.argv:
    vlm = VisionLanguage(cfg)
    try:
        start = time.perf_counter()
        vlm.start()
        report["vlm_start_seconds"] = time.perf_counter() - start
        start = time.perf_counter()
        report["description"] = vlm.describe(image)
        report["description_seconds"] = time.perf_counter() - start
        report["vlm_log"] = str(vlm.log_path)
        ids = [os.getpid(), vlm.process.pid]
        command = ("Get-CimInstance Win32_PerfFormattedData_GPUPerformanceCounters_GPUProcessMemory | "
                   f"Where-Object {{ $_.Name -match '^pid_({ids[0]}|{ids[1]})_' }} | "
                   "Select-Object Name,DedicatedUsage,SharedUsage,TotalCommitted | ConvertTo-Json -Compress")
        measured = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                  capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        measured.check_returncode()
        memory = json.loads(measured.stdout)
        report["gpu_memory_sample"] = memory if isinstance(memory, list) else [memory]
        report["combined_dedicated_gpu_bytes"] = sum(p["DedicatedUsage"] for p in report["gpu_memory_sample"])
        report["memory_measurement"] = "Windows GPU process counters after both SigLIP towers and Qwen image inference; snapshot, not peak"
        print(json.dumps(report, ensure_ascii=True, indent=2), flush=True)
    finally:
        vlm.close()
(cfg.data_dir / "reports/gpu_probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
