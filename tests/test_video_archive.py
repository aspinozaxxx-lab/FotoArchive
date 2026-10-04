import copy
import json
from pathlib import Path

import numpy as np
from PIL import Image
import pytest

from fotoarchive.catalog import Catalog, Filters
from fotoarchive.config import Settings, EMBED_VERSION, FACE_VERSION
from fotoarchive.engine import Engine
from fotoarchive.library import Library
from fotoarchive.media import sha256
from fotoarchive.remote_protocol import pack, unpack
from fotoarchive.search_session import SearchSession
from fotoarchive.video_archive import claim, complete, schedule, refresh_person_names
from fotoarchive.video_caption import validate_caption
from fotoarchive.video_content import VideoInput, decode_segment
from fotoarchive.video_people import match_identity, label_people


def prepared_video(cat, duration=32000):
    path = cat.cfg.root/'2003/clip.mp4'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'source media stays unchanged')
    asset_id, _ = cat.register(path)
    job = cat.next_job(('metadata',))
    metadata = dict(width=80, height=60, captured_at=None, date_raw=None, date_offset=None,
        camera='', orientation='landscape', metadata_json='{}', media_kind='video', duration_ms=duration)
    cat.complete_metadata(job, metadata, path, 'fixture')
    cat.finish_job(job, .1)
    return asset_id, path


def result(job, faces=None):
    vector = np.eye(1,768,dtype=np.float32)[0]
    return dict(vector=vector,text_vector=vector, caption=dict(description='[[P1]] обнимает [[P2]]', objects=['люди'],
        actions=['обнимает'], audio_description='музыка', speech='привет', uncertainties=[],
        events=[dict(start_ms=job['timestamp_ms'],end_ms=job['end_ms'],description='[[P1]] обнимает [[P2]]')]),
        bindings=[dict(token='P1',evidence=[dict(timestamp_ms=job['timestamp_ms'],face_index=0)]),
                  dict(token='P2',evidence=[dict(timestamp_ms=job['timestamp_ms']+1000,face_index=0)])],
        frames=[dict(timestamp_ms=stamp, vector=vector, faces=(faces or {}).get(stamp,[]),
                     thumbnail=Image.new('RGB',(80,60),'blue')) for stamp in range(job['timestamp_ms'],job['end_ms'],1000)],
        model='tested',backend='fixture',audio_used=True)


def face(axis):
    return dict(box=[.1,.1,.8,.8],landmarks=[[.2,.2]]*5,confidence=.99,
        vector=np.eye(1,128,k=axis,dtype=np.float32)[0],portrait=Image.new('RGB',(112,112),'green'))


@pytest.fixture
def engine(tmp_path):
    obj = Engine(Settings(data_dir=tmp_path/'data',root=tmp_path/'originals'))
    yield obj
    obj.close()


def test_segment_resume_rebuild_is_video_only_and_original_is_unchanged(engine):
    cat=engine.catalog
    asset_id,path=prepared_video(cat)
    before=sha256(path)
    asset,job=claim(cat)
    assert (job['timestamp_ms'],job['end_ms'])==(0,30000)
    assert cat.next_job(('embedding','faces','caption')) is None
    cat.recover()
    asset,resumed=claim(cat)
    assert resumed['unit_id']==job['unit_id']
    assert complete(cat,resumed,result(resumed))
    cat.finish_job(resumed,.2)
    assert cat.get(asset_id)['embed_version'] is None
    assert cat.stats()['video_segments']['done']==1
    # Reopen the DB connection while keeping all coordinator components bound
    # to the same catalog owner (as in process restart recovery).
    import sqlite3
    cat.db.close()
    cat.db=sqlite3.connect(engine.cfg.data_dir/'catalog.sqlite3')
    cat.db.row_factory=sqlite3.Row
    cat.db.execute('PRAGMA foreign_keys=ON')
    cat.db.create_function('lower',1,lambda s:s.casefold() if s else '',deterministic=True)
    cat.media_units.db=cat.db
    cat.recover()
    asset,last=claim(cat)
    assert last['timestamp_ms']==30000
    assert complete(cat,last,result(last))
    cat.finish_job(last,.2)
    assert cat.get(asset_id)['embed_version']==EMBED_VERSION
    assert cat.stats()['video_files']==dict(total=1,done=1)
    assert cat.stats()['captions']==1
    assert claim(cat) is None
    assert schedule(cat,force=True)==1
    assert sha256(path)==before
    assert cat.db.execute("SELECT count(*) FROM unit_jobs WHERE stage='video'").fetchone()[0]==2


def test_bad_or_stale_segment_is_atomic_and_does_not_block_later_segments(engine):
    cat=engine.catalog
    asset_id,path=prepared_video(cat)
    _,job=claim(cat)
    bad=result(job);bad['frames'][-1]['vector']=np.full(768,np.nan)
    before=cat.db.execute('SELECT count(*) FROM embeddings').fetchone()[0]
    with pytest.raises(ValueError):complete(cat,job,bad)
    assert cat.db.execute('SELECT count(*) FROM embeddings').fetchone()[0]==before
    cat.finish_job(job,.1,'corrupt result')
    _,next_job=claim(cat)
    assert next_job['timestamp_ms']==30000
    cat.cfg.video_model='internvideo'
    assert not complete(cat,next_job,result(next_job))
    schedule(cat)
    assert claim(cat)[1]['timestamp_ms']==0
    path.write_bytes(b'replaced longer source')
    cat.register(path)
    assert not complete(cat,job,result(job))


def test_names_search_and_multiple_people_across_different_video_moments(engine):
    cat=engine.catalog
    examples=[]
    for axis,name in enumerate(('Олег','Юля')):
        path=cat.cfg.root/'2003'/f'{axis}.jpg';path.parent.mkdir(parents=True,exist_ok=True)
        Image.new('RGB',(80,60)).save(path)
        asset_id,_=cat.register(path)
        cat.complete_faces(dict(asset_id=asset_id,file_version=1),[face(axis)])
        key=cat.faces_for(asset_id)[0]['id']
        person=Library(cat).command(dict(action='library_save_person',name=name,examples=[key]))['person_id']
        examples.append((person,key))
        cat.db.execute("UPDATE jobs SET status='done' WHERE asset_id=? AND stage='metadata'",(asset_id,));cat.db.commit()
    video_id,_=prepared_video(cat,3000)
    _,job=claim(cat)
    complete(cat,job,result(job,{0:[face(0)],1000:[face(1)]}));cat.finish_job(job,.2)
    engine.index.flush_all()
    asset=cat.media_units.asset_at(cat.get(video_id),f'{video_id}:frame:1000')
    assert 'Олег обнимает Юля' in asset['description']
    assert {p['name'] for p in json.loads(asset['observations_json'])['people']}=={'Олег','Юля'}
    groups=[dict(vectors=[face(i)['vector']],rejected=[]) for i in range(2)]
    search=SearchSession(cat,engine.index,1,Filters(media_kind='video'),'',face(0)['vector'],[],
                         mode='face',face_groups=groups,people_mode='all')
    assert search.page(expand=True)['items'][0]['id']==video_id
    # Full text contains names/actions/audio, with matched moment retained.
    semantic=SearchSession(cat,engine.index,2,Filters(media_kind='video'),'Олег',np.eye(1,768,dtype=np.float32)[0],[])
    assert semantic.page(expand=True)['items'][0]['id']==video_id
    person,key=examples[0]
    Library(cat).command(dict(action='library_save_person',person_id=person,name='Алексей',examples=[key]))
    list(refresh_person_names(cat))
    assert 'Алексей' in cat.get(video_id)['description'] and 'Олег' not in cat.get(video_id)['description']
    engine.index.flush_all();engine.index.maintain()
    renamed=engine.index.table.search('Алексей',query_type='fts',fts_columns='search_text').limit(10).to_list()
    assert renamed and all('Олег' not in row['search_text'] for row in renamed)
    assert engine.load_faces(video_id,f'{video_id}:segment:0')
    Library(cat).command(dict(action='library_delete_person',person_id=person))
    list(refresh_person_names(cat))
    assert 'Алексей' not in cat.get(video_id)['description']


def test_opaque_input_roundtrip_bounds_and_audio_absence():
    metadata=dict(start_ms=0,end_ms=2000,frame_count=2,requested_times_ms=[0,1000],frame_times_ms=[0,1000],has_audio=True)
    source=VideoInput(metadata,[Image.new('RGB',(80,60)),Image.new('RGB',(80,60),'red')],np.arange(32000,dtype=np.float32)/32000)
    packed=pack(source.encode())
    assert not packed.startswith(b'\x89PNG')
    decoded=VideoInput.decode(unpack(packed))
    assert np.array_equal(decoded.audio,source.audio) and decoded.images[1].getpixel((0,0))[0]>=250
    with pytest.raises(ValueError):VideoInput.decode(source.encode()[:-1])
    invalid=copy.deepcopy(metadata);invalid['requested_times_ms']=[0,1200]
    with pytest.raises(ValueError):VideoInput.decode(VideoInput(invalid,source.images,source.audio).encode())
    caption=validate_caption(dict(description='сцена',audio_description='придуманный звук',speech='слова'),0,2000,False)
    assert caption['speech']==caption['audio_description']==''
    with pytest.raises(ValueError):validate_caption('{"description":"оборвано',0,2000)


def test_decoding_real_video_is_read_only_and_cancellable(tmp_path):
    from test_media_formats import make_video
    path=make_video(tmp_path/'clip.mp4',duration=4)
    stat=path.stat();before=sha256(path)
    asset=dict(path=str(path),size=stat.st_size,mtime_ns=stat.st_mtime_ns,video_start_ms=1000,video_end_ms=4000)
    data=decode_segment(asset)
    assert len(data.images)==3 and data.metadata['requested_times_ms']==[1000,2000,3000]
    assert not data.has_audio and np.count_nonzero(data.audio)==0
    assert sha256(path)==before
    with pytest.raises(InterruptedError):decode_segment(asset,lambda:True)


def test_ambiguous_identity_stays_anonymous_and_cross_frame_tracks_remain_stable():
    vector=face(0)['vector']
    people=[dict(id='a',vectors=[vector.tolist()]),dict(id='b',vectors=[vector.tolist()])]
    assert match_identity(vector,people) is None
    images=[Image.new('RGB',(80,60))]*2
    _,bindings=label_people(images,[[face(0)],[face(0)]],[0,1000],people)
    assert len(bindings)==1 and bindings[0]['person_id'] is None


def test_full_resolution_pixels_survive_transport_and_large_video_is_split(engine):
    rng=np.random.default_rng(7)
    pixels=rng.integers(0,256,(1000,1560,3),dtype=np.uint8)
    metadata=dict(start_ms=0,end_ms=1000,frame_count=1,requested_times_ms=[0],frame_times_ms=[0],has_audio=False)
    source=VideoInput(metadata,[Image.fromarray(pixels)],np.zeros(16000,dtype=np.float32))
    received=VideoInput.decode(unpack(pack(source.encode())))
    assert received.images[0].size==(1560,1000)
    assert np.array_equal(np.asarray(received.images[0]),pixels)
    cat=engine.catalog
    video_id,_=prepared_video(cat,10000)
    cat.db.execute('UPDATE assets SET width=3840,height=2160 WHERE id=?',(video_id,));cat.db.commit()
    _,job=claim(cat)
    assert job['end_ms']==7000
    assert complete(cat,job,result(job));cat.finish_job(job,.1)
    _,tail=claim(cat)
    assert (tail['timestamp_ms'],tail['end_ms'])==(7000,10000)


def test_user_approved_face_template_survives_changed_detections(engine):
    from fotoarchive.video_people import people_snapshot
    cat=engine.catalog
    asset_id,_=prepared_video(cat,1000)
    _,job=claim(cat)
    assert complete(cat,job,result(job,{0:[face(0)]}));cat.finish_job(job,.1)
    example=cat.faces_for(asset_id)[0]['id']
    library=Library(cat)
    person=library.command(dict(action='library_save_person',name='Олег',examples=[example]))['person_id']
    schedule(cat,force=True)
    _,job=claim(cat)
    assert complete(cat,job,result(job,{0:[face(1)]}));cat.finish_job(job,.1)
    assert np.array_equal(cat.face_vector(example),face(1)['vector'])
    assert match_identity(face(0)['vector'],people_snapshot(cat))==person
    assert match_identity(face(1)['vector'],people_snapshot(cat)) is None
    library.command(dict(action='library_save_person',person_id=person,name='Алексей',examples=[example]))
    assert match_identity(face(0)['vector'],people_snapshot(cat))==person


def test_audio_is_synchronized_for_a_nonzero_segment(tmp_path):
    import av
    from fractions import Fraction
    path=tmp_path/'sound.mkv'
    with av.open(str(path),'w') as output:
        video=output.add_stream('ffv1',rate=2);video.width=320;video.height=200;video.pix_fmt='yuv420p'
        sound=output.add_stream('pcm_s16le',rate=16000);sound.layout='mono'
        for i in range(8):
            frame=av.VideoFrame.from_image(Image.new('RGB',(320,200),'red'))
            for packet in video.encode(frame):output.mux(packet)
        for packet in video.encode():output.mux(packet)
        for second in range(4):
            amplitude=.1 if second<2 else .3
            values=(np.sin(np.arange(16000)*2*np.pi*440/16000)*amplitude*32767).astype('<i2')[None]
            frame=av.AudioFrame.from_ndarray(values,format='s16',layout='mono')
            frame.sample_rate=16000;frame.pts=second*16000;frame.time_base=Fraction(1,16000)
            for packet in sound.encode(frame):output.mux(packet)
        for packet in sound.encode():output.mux(packet)
    stat=path.stat()
    data=decode_segment(dict(path=str(path),size=stat.st_size,mtime_ns=stat.st_mtime_ns,video_start_ms=1000,video_end_ms=3000))
    assert data.has_audio and data.metadata['audio_coverage']>.99
    assert data.images[0].size==(320,200)
    assert np.max(np.abs(data.audio_part(0)))==pytest.approx(.1,abs=.001)
    assert np.max(np.abs(data.audio_part(1)))==pytest.approx(.3,abs=.001)


def test_new_video_text_is_searchable_before_index_maintenance(engine):
    cat=engine.catalog
    video_id,_=prepared_video(cat,1000)
    _,job=claim(cat)
    complete(cat,job,result(job));cat.finish_job(job,.1)
    engine.index.flush_all();engine.index.maintain()
    schedule(cat,force=True)
    _,job=claim(cat)
    value=result(job);value['caption']['description']='Артист играет на саксофоне'
    complete(cat,job,value);cat.finish_job(job,.1)
    engine.index.flush_all()
    hits=engine.index.table.search('саксофоне',query_type='fts',fts_columns='search_text').limit(10).to_list()
    assert hits and hits[0]['asset_id']==video_id


def test_pause_interrupts_an_inflight_owner_without_waiting_for_inference(engine):
    import queue,subprocess,sys,threading,time
    from fotoarchive.video_runtime import VideoOwner
    owner=VideoOwner(engine.cfg)
    child=subprocess.Popen([sys.executable,'-u','-c','import sys,time; sys.stdin.readline(); time.sleep(120)'],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    owner.process=child;owner.responses=queue.Queue();owner.start=lambda model:None
    failures=[]
    def run():
        try:owner.run(dict(model='omni',blob='unused'))
        except InterruptedError:failures.append('interrupted')
    thread=threading.Thread(target=run);thread.start()
    time.sleep(.1);start=__import__('time').perf_counter();owner.close();thread.join(2)
    assert time.perf_counter()-start<2 and not thread.is_alive()
    assert child.poll() is not None and failures==['interrupted']
    child.stdout.close()


def test_unreadable_video_is_an_error_instead_of_an_unclaimable_pending_job(engine):
    cat=engine.catalog
    path=cat.cfg.root/'2003/broken.mp4';path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'broken')
    asset_id,_=cat.register(path)
    metadata=cat.next_job(('metadata',));cat.finish_job(metadata,.1,'No video stream')
    assert cat.db.execute("SELECT status FROM jobs WHERE asset_id=? AND stage='video'",(asset_id,)).fetchone()[0]=='error'
    schedule(cat,force=True)
    assert claim(cat) is None
    cat.retry_errors()
    assert cat.next_job(('metadata',))


def test_description_vectors_find_video_without_a_matching_word(engine):
    cat=engine.catalog
    video_id,_=prepared_video(cat,1000)
    _,job=claim(cat);value=result(job)
    value['text_vector']=np.eye(1,768,k=1,dtype=np.float32)[0]
    value['caption']['description']='человек ведёт беседу'
    complete(cat,job,value);cat.finish_job(job,.1)
    engine.index.flush_all()
    hits=engine.index.candidates(value['text_vector'],'разговаривает',Filters(media_kind='video'),limit=10)
    assert hits and hits[0]['id']==video_id and job['unit_id'] in hits[0]['matched_units']
    assert not engine.index.candidates(value['text_vector'],'разговаривает',Filters(media_kind='photo'),limit=10)
