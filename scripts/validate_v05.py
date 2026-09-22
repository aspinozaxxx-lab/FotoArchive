"""Measure real CPU preparation and verify overlapping GPU work on local copies."""
import json
import multiprocessing as mp
import os
from pathlib import Path
import queue
import shutil
import sqlite3
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from fotoarchive.catalog import Catalog
from fotoarchive.config import Settings
from fotoarchive.engine import worker_main
from fotoarchive.media import sha256
from fotoarchive.preparation import PreparationPool


def read_db(data):
    db=sqlite3.connect(f'file:{(data/"catalog.sqlite3").as_posix()}?mode=ro',uri=True)
    db.row_factory=sqlite3.Row
    return db


def main():
    base=Settings.load()
    output=Path(tempfile.mkdtemp(prefix='v05-pipeline-',dir=base.data_dir/'reports'))
    source=output/'source';source.mkdir()
    with read_db(base.data_dir) as db:
        samples=[Path(r[0]) for r in db.execute("SELECT path FROM assets WHERE metadata_ready=1 AND extension='.jpg' AND width*height>=5000000 ORDER BY id LIMIT 8")]
    if not samples:
        raise RuntimeError('Need indexed full-size photos for the benchmark')
    original_hashes={str(p):sha256(p) for p in samples}
    # Copies only. Hard links between test copies save space; originals are never linked.
    for i in range(2048):
        parent=source/f'{i//32:02d}';parent.mkdir(exist_ok=True)
        target=parent/f'{i:04d}.jpg'
        if i<len(samples):
            shutil.copyfile(samples[i],target)
        else:
            os.link(source/'00'/f'{i%len(samples):04d}.jpg',target)
    paths=sorted(source.rglob('*.jpg'))
    report={'passed':False,'directory':str(output),'sample_count':len(paths),'cpu':{}}

    def benchmark(workers):
        cfg=Settings(data_dir=output/f'cpu-{workers}',root=source,includes=['.'],preparation_workers=workers)
        catalog=Catalog(cfg)
        for path in paths[:128]:catalog.register(path)
        pool=PreparationPool(catalog)
        started=time.perf_counter()
        try:
            while True:
                pool.collect();pool.fill()
                if not pool.pending:break
                time.sleep(.005)
            elapsed=time.perf_counter()-started
            assert catalog.stats()['metadata']==128 and not catalog.stats()['errors']
            return pool.status()|{'seconds':elapsed,'photos_per_second':128/elapsed,
                                  'average_cpu_cores':pool.cpu_seconds/elapsed}
        finally:
            pool.close();catalog.close()

    runtime=mp.get_context('spawn')
    worker=None;shutdown=None
    try:
        for count in (1,6):
            report['cpu'][str(count)]=benchmark(count)
            print('CPU '+str(count)+': '+json.dumps(report['cpu'][str(count)]),flush=True)
        assert len(report['cpu']['6']['process_ids'])==6
        report['speedup']=report['cpu']['1']['seconds']/report['cpu']['6']['seconds']
        data=output/'gpu-data'
        for name in ('models','runtime','geonames'):
            for path in (base.data_dir/name).rglob('*'):
                if path.is_file():
                    destination=data/name/path.relative_to(base.data_dir/name)
                    destination.parent.mkdir(parents=True,exist_ok=True)
                    os.link(path,destination)
        cfg=Settings(data_dir=data,root=source,includes=['.'],preparation_workers=6);cfg.save()
        commands,events,shutdown=runtime.Queue(64),runtime.Queue(256),runtime.Event()
        worker=runtime.Process(target=worker_main,args=(str(data),commands,events,shutdown));worker.start()
        deadline=time.monotonic()+240
        saw_gpu_during_scan=saw_gpu_during_cpu=False
        paused=False;resumed=False;search_requested=False;search_ok=False
        last_pipeline={};first_embedding=None;first_faces=None;first_caption=None
        while time.monotonic()<deadline:
            try:event=events.get(timeout=1)
            except queue.Empty:
                if not worker.is_alive():raise RuntimeError('Worker died')
                continue
            if event['type'] in {'fatal','error','job_error'}:raise RuntimeError(event)
            if event['type']=='ready':commands.put({'action':'scan'})
            if event['type']=='working' and event['stage'] in ('embedding','faces','caption'):
                with read_db(data) as db:
                    total,ready=db.execute('SELECT count(*),sum(metadata_ready) FROM assets').fetchone()
                saw_gpu_during_scan |= total<len(paths)
                saw_gpu_during_cpu |= ready<total
                if event['stage']=='embedding' and first_embedding is None:first_embedding={'registered':total,'prepared':ready}
                if event['stage']=='faces' and first_faces is None:first_faces={'registered':total,'prepared':ready}
                if event['stage']=='caption' and first_caption is None:first_caption={'registered':total,'prepared':ready}
            if event['type']=='status':
                last_pipeline=event['pipeline']
                stats=event['stats']
                if stats['embeddings']>=8 and not paused:
                    commands.put({'action':'pause'});paused=True
                elif paused and not resumed and event['paused'] and not last_pipeline['in_flight']:
                    report['pause_drained']=True
                    commands.put({'action':'resume'});resumed=True
                if resumed and stats['embeddings']>=16 and not search_requested:
                    commands.put({'action':'search','id':'probe','query':'люди','filters':{}});search_requested=True
                if search_ok and stats['captions']>=1 and stats['faces']>=8 and stats['embeddings']>=24 and not event['scanning']:
                    report['progress']=stats
                    break
            if event['type']=='results' and event.get('id')=='probe':
                assert event['items'];search_ok=True
        else:raise TimeoutError('Pipeline validation timed out')
        report.update(overlap_gpu_scan=saw_gpu_during_scan,overlap_gpu_cpu=saw_gpu_during_cpu,
                      first_embedding=first_embedding,first_faces=first_faces,first_caption=first_caption,
                      pipeline=last_pipeline,interactive_search=True,
                      originals_unchanged=all(sha256(Path(p))==h for p,h in original_hashes.items()))
        assert saw_gpu_during_scan and saw_gpu_during_cpu and search_ok and resumed
        assert last_pipeline['cpu_completed_during_gpu']>0 and last_pipeline['gpu_jobs_during_scan']>0
        assert len(last_pipeline['process_ids'])==6
        report['passed']=True
    finally:
        if shutdown:shutdown.set()
        if worker:
            # Drain events while the owner completes its current file.
            stop=time.monotonic()+30
            while worker.is_alive() and time.monotonic()<stop:
                try:events.get(timeout=.1)
                except queue.Empty:pass
            worker.join(1)
            if worker.is_alive():raise RuntimeError('Worker failed to exit')
        (base.data_dir/'reports/v05_pipeline.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=True,indent=2),flush=True)


if __name__=='__main__':
    mp.freeze_support()
    main()
