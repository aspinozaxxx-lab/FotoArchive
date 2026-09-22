import io
import json
import queue
import threading
import time
from types import SimpleNamespace
import numpy as np
import pytest
import requests
from PIL import Image, UnidentifiedImageError
from fotoarchive.config import Settings, EMBED_VERSION, FACE_VERSION, CAPTION_VERSION
from fotoarchive.remote_protocol import pack,unpack,digest,job_key
from fotoarchive.remote_store import Store
from fotoarchive.remote_service import Supervisor,serve,LEASE_SECONDS
from fotoarchive.remote_jobs import claim_bundle,RemoteJobs,decode_faces,vector
from fotoarchive.catalog import Catalog
from fotoarchive.media import extract_metadata,sha256


def photo_data():
    data=io.BytesIO()
    Image.new('RGB',(96,64),'red').save(data,format='JPEG')
    return data.getvalue()


def ready_catalog(tmp_path):
    root=tmp_path/'photos'
    root.mkdir()
    cfg=Settings(data_dir=tmp_path/'data',root=root)
    cat=Catalog(cfg)
    for i in range(6):
        path=root/f'{i}.jpg'
        path.write_bytes(photo_data())
        aid,_=cat.register(path)
        job=dict(asset_id=aid,file_version=1,stage='metadata')
        cat.complete_metadata(job,extract_metadata(path),str(path),sha256(path))
        cat.finish_job(job,0)
    return cat


def test_container_is_lossless_but_not_viewable(tmp_path):
    raw=photo_data()
    encoded=pack(raw)
    assert unpack(encoded)==raw
    with pytest.raises(UnidentifiedImageError):
        Image.open(io.BytesIO(encoded))
    with pytest.raises(ValueError):
        unpack(encoded+b'junk')
    with pytest.raises(ValueError):
        unpack(encoded[:-2])
    store=Store(tmp_path,budget=10000)
    store.put(digest(encoded),encoded)
    assert not list(tmp_path.glob('*.jpg'))
    with pytest.raises(ValueError):
        store.put('a'*64,encoded)
    with pytest.raises(ValueError):
        store.path('../photo.jpg')
    store.db.close()


def test_store_idempotency_model_revision_and_restart(tmp_path):
    store=Store(tmp_path)
    data=pack(photo_data()); blob=digest(data)
    store.put(blob,data)
    stages={'embedding':EMBED_VERSION,'faces':FACE_VERSION,'caption':CAPTION_VERSION}
    key=store.submit('session',blob,stages)
    assert store.submit('session',blob,stages)==key
    assert store.stats('session')['queued']==1
    assert store.take('session')['id']==key
    store.db.close()
    store=Store(tmp_path)
    assert store.result(key)['status']=='queued'
    store.finish(key,{'stages':{'caption':{'description':'red'}}})
    assert store.submit('new-session',blob,stages)==key
    assert store.result(key)['result']['stages']['caption']['description']=='red'
    assert store.take('new-session') is None
    with pytest.raises(ValueError):
        store.submit('session',blob,{'embedding':'wrong-model'})
    store.db.close()


def test_cache_budget_evicts_only_own_unpinned_objects(tmp_path):
    data=pack(photo_data()); blob=digest(data)
    store=Store(tmp_path,budget=len(data)+20)
    store.put(blob,data)
    key=store.submit('session',blob,{'embedding':EMBED_VERSION})
    store.take('session')
    new=pack(b'other data'*50)
    with pytest.raises(ValueError):
        store.put(digest(new),new)
    assert store.has(blob)
    store.finish(key,{'ok':True})
    store.put(digest(new),new)
    assert not store.has(blob) and store.has(digest(new))
    assert store.stats('session')['cache_bytes']<=store.budget
    store.db.close()


def test_watchdog_waits_for_training_and_expires_without_gpu_import(tmp_path,monkeypatch):
    clock=[100.]
    gpu=dict(utilization=98,memory_mb=21000,total_mb=32000,foreign=[{'pid':123}])
    supervisor=Supervisor(tmp_path,clock=lambda:clock[0],probe=lambda *args:gpu)
    supervisor.lease('session-for-testing',True)
    supervisor.tick()
    assert supervisor.state=='waiting_training' and supervisor.worker is None
    gpu.update(utilization=0,foreign=[])
    supervisor.tick()
    assert supervisor.state=='ready'
    clock[0]+=LEASE_SECONDS+1
    supervisor.tick()
    assert supervisor.state=='disconnected' and supervisor.worker is None
    supervisor.store.db.close()


def test_authenticated_http_upload_works_while_gpu_is_busy(tmp_path):
    supervisor=Supervisor(tmp_path,probe=lambda *args:dict(utilization=99,memory_mb=21000,total_mb=32000,foreign=[{'pid':123}]))
    server=serve(supervisor,'secret-token',port=0)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url=f'http://127.0.0.1:{server.server_port}'
    client=requests.Session();client.trust_env=False
    try:
        assert client.get(url+'/status').status_code==401
        client.headers['Authorization']='Bearer secret-token'
        assert client.post(url+'/lease',json=dict(session='testing-session-1',active=True)).ok
        supervisor.tick()
        data=pack(photo_data());blob=digest(data)
        assert client.put(url+'/blobs/'+blob,data=data).ok
        assert client.head(url+'/blobs/'+blob).ok
        job=client.post(url+'/jobs',json=dict(session='testing-session-1',blob=blob,stages={'embedding':EMBED_VERSION})).json()
        assert job['key']==job_key(blob,{'embedding':EMBED_VERSION})
        supervisor.tick()
        status=client.get(url+'/status').json()
        assert status['state']=='waiting_training' and status['queued']==1 and not supervisor.worker
        assert client.post(url+'/lease',json=dict(session='testing-session-1',active=False)).ok
        assert supervisor.deadline==0
    finally:
        client.close();server.shutdown();server.server_close();thread.join(2);supervisor.store.db.close()


def test_local_remote_claims_do_not_overlap_and_recover(tmp_path):
    cat=ready_catalog(tmp_path)
    local=cat.next_job(('embedding',))
    asset,jobs=claim_bundle(cat)
    assert asset['id']!=local['asset_id']
    assert {j['stage'] for j in jobs}=={'embedding','faces','caption','location'}
    assert cat.next_job(('faces',),asset_id=asset['id']) is None
    cat.recover()
    assert cat.next_job(('embedding',),asset_id=asset['id']) is not None
    cat.close()


def test_video_bundle_uses_one_timestamp_for_all_stages(tmp_path):
    cat=ready_catalog(tmp_path)
    aid=6
    with cat.db:
        cat.db.execute("UPDATE assets SET media_kind='video',duration_ms=25000 WHERE id=?",(aid,))
        cat.media_units.create(dict(asset_id=aid,file_version=1),dict(media_kind='video',duration_ms=25000))
    asset,jobs=claim_bundle(cat)
    assert asset['media_kind']=='video' and len(jobs)==4
    assert len({j['unit_id'] for j in jobs if j.get('unit_id')})==1
    for job in jobs:
        cat.finish_job(job,1)
    next_asset,next_jobs=claim_bundle(cat)
    assert next_asset['id']==asset['id']
    assert next_jobs[0]['timestamp_ms']>jobs[0]['timestamp_ms']
    cat.close()


def test_stale_result_cannot_overwrite_replaced_file(tmp_path,monkeypatch):
    cat=ready_catalog(tmp_path)
    receipts=[]
    connected=threading.Event();connected.set()
    fake=SimpleNamespace(results=queue.Queue(),queue=queue.Queue(maxsize=64),active=True,connected=connected,
        close=lambda:None,cancel=lambda:None,acknowledge=lambda key,value:receipts.append((key,value)))
    monkeypatch.setattr('fotoarchive.remote_jobs.Transport',lambda *args:fake)
    engine=SimpleNamespace(catalog=cat,cfg=cat.cfg,emit=lambda e:None,flush_progress=lambda n:None)
    remote=RemoteJobs(engine,lambda:True)
    asset,jobs=claim_bundle(cat);remote.pending[1]=(asset,jobs)
    Path=__import__('pathlib').Path
    Image.new('RGB',(100,100),'blue').save(asset['path'])
    cat.register(Path(asset['path']))
    fake.results.put((1,dict(stages={'caption':{'description':'old'}},elapsed=1,key='old',providers={}),''))
    remote.collect()
    assert receipts==[('old','discarded')]
    assert cat.get(asset['id'])['description']==''
    assert cat.get(asset['id'])['version']==2
    assert cat.db.execute("SELECT status FROM jobs WHERE asset_id=? AND stage='caption'",(asset['id'],)).fetchone()[0]=='pending'
    cat.cfg.remote_enabled=True
    remote.fill()
    assert not fake.queue.empty() and remote.retry_at==0
    assert all(a['id']!=asset['id'] for a,_ in remote.pending.values())
    cat.close()


def test_reject_invalid_vectors():
    with pytest.raises(ValueError):
        vector([float('nan')]*768,768)
    with pytest.raises(ValueError):
        vector([1]*768,768)
    with pytest.raises(ValueError):
        decode_faces([dict(box=[-1,0,1,1],landmarks=[],confidence=1)])


def test_independent_controls_persist(tmp_path):
    cfg=Settings(data_dir=tmp_path)
    for local,remote in [(True,True),(True,False),(False,True),(False,False)]:
        cfg.local_enabled,cfg.remote_enabled=local,remote
        cfg.save()
        saved=Settings.load(tmp_path)
        assert (saved.local_enabled,saved.remote_enabled)==(local,remote)


def test_success_requires_all_stages_and_receipt_follows_commit(tmp_path):
    store=Store(tmp_path)
    data=pack(photo_data());blob=digest(data);store.put(blob,data)
    stages={'embedding':EMBED_VERSION,'caption':CAPTION_VERSION}
    key=store.submit('session',blob,stages)
    store.take('session')
    store.finish(key,{'stages':{'embedding':[1],'caption':{'description':'red'}},'errors':{}})
    assert store.stats('session')['completed']==1
    assert store.stats('session')['saved']==0
    assert store.stats('session')['awaiting_save']==1
    assert store.ready([key])[0]['key']==key
    assert store.stats('session')['saved']==0  # Download is not a commit.
    store.acknowledge('session',{key:'saved'});store.acknowledge('session',{key:'saved'})
    assert store.stats('session')['saved']==1 and store.stats('session')['awaiting_save']==0
    partial=store.submit('session',blob,{'caption':CAPTION_VERSION})
    store.take('session');store.finish(partial,{'stages':{},'errors':{'caption':'model error'}})
    assert store.stats('session')['completed']==1 and store.stats('session')['failed']==1
    store.submit('second-session',blob,stages)
    assert store.stats('second-session')['completed']==1 and store.stats('second-session')['saved']==0
    store.db.close()


def test_active_reserve_is_pinned_and_eviction_keeps_completed_totals(tmp_path):
    data=pack(photo_data());blob=digest(data)
    store=Store(tmp_path,budget=len(data)+250)
    store.active_session='session'
    store.put(blob,data)
    key=store.submit('session',blob,{'caption':CAPTION_VERSION})
    other=pack(b'other data'*500)
    store.budget=len(data)+len(other)-1
    with pytest.raises(ValueError):store.put(digest(other),other)
    assert store.has(blob)
    store.budget=10000
    store.take('session');store.finish(key,{'stages':{'caption':{}},'errors':{}})
    store.acknowledge('session',{key:'saved'})
    store.budget=len(other)+10
    store.put(digest(other),other)
    assert not store.has(blob)
    assert store.stats('session')['completed']==store.stats('session')['saved']==1
    store.db.close()


def test_server_reserve_can_refill_beyond_128_without_unbounded_upload_queue(tmp_path,monkeypatch):
    cat=ready_catalog(tmp_path)
    fake=SimpleNamespace(queue=queue.Queue(maxsize=64),results=queue.Queue(),active=True,
        connected=threading.Event(),close=lambda:None,cancel=lambda:None)
    fake.connected.set()
    monkeypatch.setattr('fotoarchive.remote_jobs.Transport',lambda *args:fake)
    bundle=(dict(id=1),[dict(stage='caption',model_version=CAPTION_VERSION)])
    monkeypatch.setattr('fotoarchive.remote_jobs.claim_bundle',lambda *args:bundle)
    cat.cfg.remote_enabled=True
    engine=SimpleNamespace(catalog=cat,cfg=cat.cfg,emit=lambda e:None)
    remote=RemoteJobs(engine,lambda:True)
    for _ in range(16):
        remote.fill()
        assert fake.queue.qsize()<=64
        while not fake.queue.empty():fake.queue.get_nowait()
    assert len(remote.pending)>128
    cat.close()


def test_traffic_reports_bytes_during_upload_and_decays_at_idle():
    from fotoarchive.remote_client import Traffic,UploadBody
    clock=[0.];traffic=Traffic(clock=lambda:clock[0])
    body=UploadBody(b'x'*2000,traffic)
    assert len(body.read(1000))==1000
    clock[0]=2
    first=traffic.snapshot()
    assert first['sent']==1000 and first['upload_bps']==500
    body.read();traffic.add(received=100)
    clock[0]=4
    assert traffic.snapshot()['upload_bps']==500
    clock[0]=20;traffic.snapshot()
    clock[0]=32
    assert traffic.snapshot()['upload_bps']==0


def test_result_delivery_and_receipt_do_not_wait_for_slow_upload(tmp_path,monkeypatch):
    from fotoarchive.remote_client import Transport
    supervisor=Supervisor(tmp_path/'server',probe=lambda *args:dict(utilization=0,memory_mb=0,total_mb=32000,foreign=[{'pid':1}]))
    server=serve(supervisor,'s'*32,port=0)
    serving=threading.Thread(target=server.serve_forever,daemon=True);serving.start()
    cfg=Settings(data_dir=tmp_path/'client',remote_enabled=True);cfg.initialize()
    (cfg.data_dir/'remote-token').write_text('s'*32)
    release=threading.Event();preparing=threading.Event()
    def open_tunnel(self):
        self.base=f'http://127.0.0.1:{server.server_port}'
        self.tunnel=SimpleNamespace(poll=lambda:None)
    def close_tunnel(self):
        self.connected.clear();self.tunnel=None
    def slow_prepare(*_):
        preparing.set();release.wait(8)
        return pack(photo_data())
    monkeypatch.setattr(Transport,'open_tunnel',open_tunnel)
    monkeypatch.setattr(Transport,'close_tunnel',close_tunnel)
    monkeypatch.setattr('fotoarchive.remote_client.prepare',slow_prepare)
    transport=Transport(cfg,lambda:True,lambda e:None);transport.active=True
    try:
        assert transport.connected.wait(5)
        data=pack(photo_data());blob=digest(data);supervisor.store.put(blob,data)
        key=supervisor.store.submit(transport.session_id,blob,{'caption':CAPTION_VERSION})
        supervisor.store.take(transport.session_id)
        supervisor.store.finish(key,dict(stages={'caption':{'description':'red'}},errors={},elapsed=1))
        with transport.pending_lock:transport.pending[1]=key
        transport.queue.put((2,dict(id=2),{'caption':CAPTION_VERSION}))
        assert preparing.wait(2)
        started=time.monotonic()
        serial,result,error=transport.results.get(timeout=2)
        assert serial==1 and not error and result['key']==key
        assert time.monotonic()-started<1.5
        assert supervisor.store.stats(transport.session_id)['saved']==0
        transport.acknowledge(key,'saved')
        deadline=time.monotonic()+2
        while supervisor.store.stats(transport.session_id)['saved']==0 and time.monotonic()<deadline:
            time.sleep(.01)
        assert supervisor.store.stats(transport.session_id)['saved']==1
        assert not release.is_set()  # Slow input still blocked, independently.
    finally:
        transport.active=False;release.set();transport.close()
        transport.upload_thread.join(3);transport.result_thread.join(3)
        server.shutdown();server.server_close();serving.join(2);supervisor.store.db.close()
