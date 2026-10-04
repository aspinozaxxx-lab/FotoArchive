"""Real GPU/audio smoke test without changing the catalogue or original media."""
import argparse
import json
from pathlib import Path
import queue
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fotoarchive.config import Settings,VIDEO_PIXEL_BUDGET
from fotoarchive.catalog import Catalog
from fotoarchive.media import sha256
from fotoarchive.video import probe
from fotoarchive.video_archive import pipeline_version
from fotoarchive.video_content import decode_segment
from fotoarchive.video_people import people_snapshot,transport_people
from fotoarchive.remote_protocol import pack

parser=argparse.ArgumentParser()
parser.add_argument('path')
parser.add_argument('--device',choices=('local','server'),required=True)
parser.add_argument('--model',choices=('omni','internvideo'),default='omni')
parser.add_argument('--seconds',type=int,default=4)
parser.add_argument('--executable',help='Test the packaged hidden video worker')
args=parser.parse_args()
cfg=Settings.load();cfg.video_model=args.model
path=Path(args.path);stat=path.stat()
before=sha256(path)
metadata,_=probe(path)
asset=dict(path=str(path),filename=path.name,size=stat.st_size,mtime_ns=stat.st_mtime_ns,
           video_start_ms=0,video_end_ms=min(args.seconds*1000,metadata['duration_ms'],
               max(1,VIDEO_PIXEL_BUDGET//(metadata['width']*metadata['height']))*1000),video_model=args.model)
cat=Catalog.open_reader(cfg)
people=people_snapshot(cat);cat.close()
started=time.perf_counter()
if args.device=='local':
    from fotoarchive.video_runtime import VideoOwner
    if args.executable:
        sys.frozen=True
        sys.executable=str(Path(args.executable).resolve())
    owner=VideoOwner(cfg)
    try:
        data=decode_segment(asset)
        with tempfile.TemporaryDirectory(prefix='video-probe-',dir=cfg.data_dir/'previews') as folder:
            blob=Path(folder)/'input.bin';blob.write_bytes(pack(data.encode()))
            result=owner.run(dict(blob=str(blob),people=people,model=args.model))
    finally:owner.close()
else:
    from fotoarchive.remote_client import Transport
    cfg.remote_enabled=True
    transport=Transport(cfg,lambda:True,lambda event:None)
    transport.active=True
    try:
        if not transport.connected.wait(40):raise RuntimeError('No server connection')
        asset['video_people']=transport_people(people)
        transport.queue.put((1,asset,{'video':pipeline_version(cfg)}))
        serial,response,error=transport.results.get(timeout=1200)
        if error:raise RuntimeError(error)
        if 'video' not in response['stages']:raise RuntimeError(response['errors'])
        result=response['stages']['video']
        transport.acknowledge(response['key'],'saved')
    finally:transport.close()
elapsed=time.perf_counter()-started
assert sha256(path)==before,'Original video changed'
folder=cfg.data_dir/'reports/video-integration-2026-10-04';folder.mkdir(parents=True,exist_ok=True)
report=dict(device=args.device,model=args.model,elapsed=elapsed,original_unchanged=True,result=result)
(folder/(args.device+'-'+args.model+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(dict(device=args.device,model=args.model,elapsed=elapsed,frames=len(result['frames']),
    audio_used=result['audio_used'],gpu_proof=result.get('gpu_proof'),original_unchanged=True),ensure_ascii=False))
