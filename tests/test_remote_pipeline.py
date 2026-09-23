"""A slow source must not starve uploads, and cached results stay evictable."""
import io
import queue
import threading
import time
from types import SimpleNamespace

import pytest
from PIL import Image
from pillow_heif import from_pillow

from fotoarchive.config import Settings, CAPTION_VERSION
from fotoarchive.remote_client import Transport, prepare, PREPARE_WORKERS, PREPARED_AHEAD
from fotoarchive.remote_protocol import pack, unpack, digest
from fotoarchive.remote_service import Supervisor, serve
from fotoarchive.remote_store import Store


@pytest.mark.parametrize('extension', ['heic','heif','avif'])
def test_encoded_phone_image_is_sent_without_decoding_or_reencoding(tmp_path, monkeypatch, extension):
    image = Image.new('RGB', (80, 48), '#314159')
    path = tmp_path / ('phone.' + extension)
    if extension in ('heic','heif'):
        from_pillow(image).save(path, quality=90)
    else:
        image.save(path, format='AVIF', quality=90)
    original = path.read_bytes()
    info = path.stat()
    monkeypatch.setattr('fotoarchive.remote_client.open_rgb', lambda *_: pytest.fail('Unnecessary local conversion'))
    encoded = prepare(dict(path=str(path),size=info.st_size,mtime_ns=info.st_mtime_ns),Settings(data_dir=tmp_path))
    assert unpack(encoded) == original
    with Image.open(io.BytesIO(unpack(encoded))) as received, Image.open(path) as source:
        assert received.convert('RGB').tobytes() == source.convert('RGB').tobytes()
    assert path.read_bytes() == original


@pytest.fixture
def transport_pair(tmp_path, monkeypatch):
    supervisor = Supervisor(tmp_path/'server',probe=lambda *_:dict(utilization=0,memory_mb=0,total_mb=32000,foreign=[]))
    server = serve(supervisor, 't'*32, port=0)
    serving = threading.Thread(target=server.serve_forever,daemon=True);serving.start()
    cfg = Settings(data_dir=tmp_path/'client',remote_enabled=True);cfg.initialize()
    (cfg.data_dir/'remote-token').write_text('t'*32)
    def open_tunnel(self):
        self.base=f'http://127.0.0.1:{server.server_port}'
        self.tunnel=SimpleNamespace(poll=lambda:None)
    monkeypatch.setattr(Transport,'open_tunnel',open_tunnel)
    monkeypatch.setattr(Transport,'close_tunnel',lambda self:(self.connected.clear(),setattr(self,'tunnel',None)))
    transport=Transport(cfg,lambda:True,lambda _:None);transport.active=True
    assert transport.connected.wait(5)
    yield transport,supervisor
    transport.close()
    server.shutdown();server.server_close();serving.join(2);supervisor.store.db.close()


def wait_until(predicate, timeout=3):
    deadline=time.monotonic()+timeout
    while not predicate():
        assert time.monotonic()<deadline, 'Pipeline did not make progress'
        time.sleep(.01)


def test_fast_photo_reaches_server_while_another_source_is_still_preparing(transport_pair,monkeypatch):
    transport,supervisor=transport_pair
    release,slow=threading.Event(),threading.Event()
    def prepare_input(asset,*_):
        if asset['id']==1:
            slow.set();assert release.wait(6)
        return pack(f"source-{asset['id']}".encode())
    monkeypatch.setattr('fotoarchive.remote_client.prepare',prepare_input)
    stages={'caption':CAPTION_VERSION}
    try:
        transport.queue.put((1,dict(id=1,filename='slow.heic'),stages))
        assert slow.wait(2)
        transport.queue.put((2,dict(id=2,filename='fast.jpg'),stages))
        wait_until(lambda:supervisor.store.stats(transport.session_id)['queued']==1)
        assert not release.is_set()
        with transport.pending_lock:assert 2 in transport.pending and 1 not in transport.pending
        assert transport.transfer_status()['preparing']==1
    finally:release.set()


def test_cancelling_during_preparation_never_submits_old_generation(transport_pair,monkeypatch):
    transport,supervisor=transport_pair
    release,started,returned=threading.Event(),threading.Event(),threading.Event()
    def prepare_input(*_):
        started.set();assert release.wait(6);returned.set()
        return pack(b'stale source')
    monkeypatch.setattr('fotoarchive.remote_client.prepare',prepare_input)
    try:
        transport.queue.put((1,dict(id=1),{'caption':CAPTION_VERSION}))
        assert started.wait(2)
        transport.cancel();release.set();assert returned.wait(2)
        wait_until(lambda:transport.transfer_status()['preparing']==0)
        assert not transport.pending and transport.prepared.empty() and transport.results.empty()
        assert supervisor.store.stats(transport.session_id)['queued']==0
    finally:release.set()


def test_prepared_bytes_are_bounded_while_upload_is_blocked(transport_pair,monkeypatch):
    transport,_=transport_pair
    release=threading.Event()
    request=transport.request
    def blocked_request(session,method,path,**kwargs):
        if method=='PUT':assert release.wait(6)
        return request(session,method,path,**kwargs)
    monkeypatch.setattr(transport,'request',blocked_request)
    monkeypatch.setattr('fotoarchive.remote_client.prepare',lambda asset,*_:pack(str(asset['id']).encode()))
    try:
        for i in range(20):transport.queue.put((i,dict(id=i),{'caption':CAPTION_VERSION}))
        wait_until(lambda:transport.prepared.full())
        time.sleep(.1)
        status=transport.transfer_status()
        assert status['prepared']==PREPARED_AHEAD and status['preparing']<=PREPARE_WORKERS
        assert transport.queue.qsize()>=20-PREPARED_AHEAD-PREPARE_WORKERS-len(transport.upload_threads)
    finally:release.set()


def test_cache_clears_a_batch_but_keeps_active_input_and_saved_totals(tmp_path):
    import random
    rng=random.Random(3)
    objects=[pack(rng.randbytes(2048)) for _ in range(10)]
    store=Store(tmp_path,budget=100000);store.active_session='active-session'
    for data in objects[:8]:
        blob=digest(data);store.put(blob,data)
        key=store.submit('active-session',blob,{'caption':CAPTION_VERSION})
        store.take('active-session');store.finish(key,dict(stages={'caption':{}},errors={}))
        store.acknowledge('active-session',{key:'saved'})
    pinned=digest(objects[8]);store.put(pinned,objects[8])
    store.submit('active-session',pinned,{'caption':CAPTION_VERSION})
    store.budget=store.stats('active-session')['cache_bytes']+len(objects[9])-1
    store.put(digest(objects[9]),objects[9])
    stats=store.stats('active-session')
    assert store.has(pinned) and stats['queued']==1
    assert stats['cache_bytes']<=store.budget*.85
    assert stats['saved']==stats['completed']==8
    assert stats['cache_pending_bytes']>0 and stats['cache_reusable_bytes']>0
    store.db.close()


def test_status_explains_preparation_instead_of_empty_server_queue(qtbot,tmp_path):
    from fotoarchive.remote_panel import RemotePanel
    panel=RemotePanel(Settings(data_dir=tmp_path),SimpleNamespace(send=lambda **_:None));qtbot.addWidget(panel)
    panel.update_status(dict(state='ready',queued=0,running=0,upload_queue=61,preparing=3,prepared=0,
                             uploading='preparing',transfer_file='IMG_001.HEIC',cache_bytes=20*1024**3,
                             cache_limit=20*1024**3,cache_pending_bytes=0,cache_reusable_bytes=20*1024**3))
    assert panel.state.text()=='Ожидает подготовку снимков'
    assert panel.job_values['upload_queue'].text()=='61' and panel.job_values['preparing'].text()=='3'
    assert 'автоматически: 20.0 ГБ' in panel.cache_details.text()
    assert panel.transfer_file.toolTip()=='IMG_001.HEIC'


def test_dispatch_does_not_wait_one_second_between_jobs_or_reprobe_gpu(tmp_path):
    calls=[]
    clock=[100.]
    def probe(*_):
        calls.append(clock[0]);return dict(utilization=0,memory_mb=0,total_mb=32000,foreign=[])
    supervisor=Supervisor(tmp_path,clock=lambda:clock[0],probe=probe)
    supervisor.lease('dispatch-test-session',True)
    for i in range(2):
        data=pack(str(i).encode());blob=digest(data);supervisor.store.put(blob,data)
        supervisor.store.submit(supervisor.session,blob,{'caption':CAPTION_VERSION})
    worker=SimpleNamespace(pid=123,stdin=io.StringIO(),poll=lambda:None)
    supervisor.start=lambda:setattr(supervisor,'worker',worker)
    supervisor.tick()
    first=supervisor.current['id']
    clock[0]+=.05
    supervisor.messages.put((123,dict(id=first,stages={'caption':{}},errors={})))
    supervisor.tick()
    assert supervisor.current['id']!=first and len(calls)==1
    assert supervisor.status()['current_stages']==['caption']
    assert supervisor.store.stats(supervisor.session)['completed']==1
    supervisor.worker=None;supervisor.store.db.close()


@pytest.mark.parametrize('infrastructure', [False,True])
def test_bad_description_skips_photo_but_only_gpu_failure_restarts_worker(tmp_path,infrastructure):
    from fotoarchive.remote_worker import record_stage_error
    from fotoarchive.inference import GPUUnavailable
    supervisor=Supervisor(tmp_path,clock=lambda:100.,
        probe=lambda *_:dict(utilization=0,memory_mb=0,total_mb=32000,foreign=[]))
    supervisor.lease('error-test-session',True)
    for i in range(2):
        data=pack(str(i).encode());blob=digest(data);supervisor.store.put(blob,data)
        supervisor.store.submit(supervisor.session,blob,{'caption':CAPTION_VERSION})
    worker=SimpleNamespace(pid=123,stdin=io.StringIO(),poll=lambda:None)
    supervisor.start=lambda:setattr(supervisor,'worker',worker)
    def stop():
        supervisor.worker=None;supervisor.current=None;supervisor.store.reset()
    supervisor.stop=stop
    supervisor.tick()
    first=supervisor.current['id']
    result=dict(id=first,stages={},errors={})
    exc=GPUUnavailable('CUDA failed') if infrastructure else ValueError('Модель не завершила ответ')
    record_stage_error(result,'caption',exc)
    supervisor.messages.put((123,result));supervisor.tick()
    assert supervisor.store.stats(supervisor.session)['failed']==1
    if infrastructure:
        assert supervisor.state=='retrying' and supervisor.worker is None and supervisor.retry_at==160
    else:
        assert supervisor.state=='working' and supervisor.worker is worker
        assert supervisor.current['id']!=first and supervisor.retry_at==0
    supervisor.worker=None;supervisor.store.db.close()


@pytest.mark.parametrize('size',[(1480,940),(1060,680)])
def test_open_processing_drawer_keeps_report_and_footer_separate(qtbot,tmp_path,size):
    from PySide6.QtCore import QPoint
    from fotoarchive.ui import MainWindow
    from test_ui import FakeBackend
    window=MainWindow(Settings(data_dir=tmp_path),FakeBackend());qtbot.addWidget(window)
    window.resize(*size);window.show()
    window.pipeline_label.setText('Подготовка CPU: до 0 из 6 процессов · в очереди 0 · GPU: локальная обработка выключена')
    window.source_label.setText('Источник: выбрано папок с подпапками: 156 · файлов изображений: 125 379 · видео: 1 167 · всего для каталога: 126 546')
    window.updates_label.setText('Проверка завершена · новых: 0 · изменённых: 0 · возвращённых: 0 · удалено из каталога: 0')
    window.updates_label.show()
    window.remote_panel.update_status(dict(state='working',queued=1200,partial=1,discarded=2,
        cache_bytes=18*1024**3,cache_limit=20*1024**3,cache_pending_bytes=2*1024**3))
    window.processing_toggle.setChecked(True)
    qtbot.wait(100)
    panel=window.remote_panel
    content=window.processing_panel.widget()
    def top(widget):return widget.mapTo(content,QPoint()).y()
    assert top(window.source_label)>top(panel)+panel.height()-1
    assert top(window.updates_label)>top(window.source_label)+window.source_label.height()-1
    drawer=window.processing_panel
    assert drawer.geometry().bottom()<window.compact_status.geometry().top()
    assert window.compact_status.geometry().bottom()<window.centralWidget().height()
    drawer.ensureWidgetVisible(window.pause_button)
    qtbot.wait(20)
    assert drawer.viewport().rect().contains(window.pause_button.mapTo(drawer.viewport(),window.pause_button.rect().center()))
    window.close()
