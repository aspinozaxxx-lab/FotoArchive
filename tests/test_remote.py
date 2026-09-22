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
    fake=SimpleNamespace(results=queue.Queue(),close=lambda:None,cancel=lambda:None)
    monkeypatch.setattr('fotoarchive.remote_jobs.Transport',lambda *args:fake)
    engine=SimpleNamespace(catalog=cat,cfg=cat.cfg,emit=lambda e:None,flush_progress=lambda n:None)
    remote=RemoteJobs(engine,lambda:True)
    asset,jobs=claim_bundle(cat);remote.pending[1]=(asset,jobs)
    Path=__import__('pathlib').Path
    Image.new('RGB',(100,100),'blue').save(asset['path'])
    cat.register(Path(asset['path']))
    fake.results.put((1,dict(stages={'caption':{'description':'old'}},elapsed=1,key='old',providers={}),''))
    remote.collect()
    assert cat.get(asset['id'])['description']==''
    assert cat.get(asset['id'])['version']==2
    assert cat.db.execute("SELECT status FROM jobs WHERE asset_id=? AND stage='caption'",(asset['id'],)).fetchone()[0]=='pending'
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
