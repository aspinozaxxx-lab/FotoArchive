"""Interrupt real GPU inference without a graceful application shutdown."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

import psutil

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fotoarchive.media import sha256

parser=argparse.ArgumentParser()
parser.add_argument('path')
parser.add_argument('--device',choices=('local','server'),required=True)
args=parser.parse_args()
folder=Path(r'D:\FotoArchiveData\reports\video-integration-2026-10-04')
before=sha256(Path(args.path))
log=(folder/('crash-'+args.device+'.log')).open('w',encoding='utf-8')
child=subprocess.Popen([sys.executable,'-u','scripts/probe_video_pipeline.py',args.path,
    '--device',args.device,'--seconds','30'],stdout=log,stderr=log)
started=time.monotonic()
def server_memory():
    result=subprocess.run(['ssh','gpu-mlserver','nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits'],
        capture_output=True,text=True,check=True,timeout=8)
    return int(result.stdout.strip())
try:
    baseline=server_memory() if args.device=='server' else None
    while time.monotonic()-started<100:
        if child.poll() is not None:raise RuntimeError('Probe exited before GPU interruption')
        descendants=psutil.Process(child.pid).children(recursive=True)
        if args.device=='local':
            active=any(p.name().lower()=='llama-server.exe' for p in descendants)
        else:
            active=server_memory()>baseline+4096
        if active:break
        time.sleep(.5)
    else:raise TimeoutError('GPU process did not start')
    pids=[p.pid for p in descendants]
    stop=time.monotonic();child.kill();child.wait(5)
    if args.device=='server':
        while time.monotonic()-stop<32:
            if server_memory()<=baseline+64:break
            time.sleep(.5)
        else:raise RuntimeError('Server retained GPU allocations after lease expiry')
    else:
        while time.monotonic()-stop<5 and any(psutil.pid_exists(pid) for pid in pids):time.sleep(.1)
        assert not any(psutil.pid_exists(pid) for pid in pids),'Local GPU descendant survived owner crash'
    result=dict(device=args.device,passed=True,release_seconds=time.monotonic()-stop,
        baseline_mb=baseline,original_unchanged=sha256(Path(args.path))==before)
    (folder/('crash-'+args.device+'.json')).write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result))
finally:
    if child.poll() is None:child.kill();child.wait(5)
    log.close()
