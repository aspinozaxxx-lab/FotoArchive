"""Real PMTiles range reads, persistent caching and isolated Qt map geometry."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from threading import Thread
import gzip
import io
import pytest
import requests
from pmtiles.writer import Writer
from pmtiles.tile import Compression,TileType,zxy_to_tileid
from PySide6.QtCore import QPoint,Qt
from fotoarchive.map_tiles import TileCache


@pytest.fixture
def source():
    output=io.BytesIO();writer=Writer(output)
    for z,x,y in [(0,0,0),(1,0,0),(1,1,0)]:
        writer.write_tile(zxy_to_tileid(z,x,y),gzip.compress(f'tile:{z}:{x}:{y}'.encode()))
    writer.finalize(dict(tile_compression=Compression.GZIP,tile_type=TileType.MVT),{})
    body=output.getvalue()
    class Handler(BaseHTTPRequestHandler):
        hits=0
        def do_GET(self):
            Handler.hits+=1
            if self.path=='/gone':self.send_error(404);return
            start,end=map(int,self.headers['Range'].removeprefix('bytes=').split('-'))
            self.send_response(200 if self.path=='/ignores-range' else 206)
            self.send_header('Content-Range',f'bytes {start}-{end}/{len(body)}')
            data=body[start:end+1];self.send_header('Content-Length',str(len(data)))
            self.end_headers();self.wfile.write(data)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    yield f'http://127.0.0.1:{server.server_port}',Handler
    server.shutdown();server.server_close();thread.join(2)


def test_range_reader_deduplicates_concurrent_work_and_restarts_offline(tmp_path,source,monkeypatch):
    url,handler=source;cache=TileCache(tmp_path);cache.source=url+'/map'
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(lambda _:cache.tile(1,0,0),range(20)))==[b'tile:1:0:0']*20
    assert handler.hits==3  # header, directory, compressed tile; only once each
    cache.close();cache=TileCache(tmp_path)
    def offline(*args,**kwargs):raise AssertionError('Cached data must not access internet')
    monkeypatch.setattr(requests.Session,'get',offline)
    assert cache.tile(1,0,0)==b'tile:1:0:0'
    assert cache.cached('tile:1:0:0')==b'tile:1:0:0'
    cache.close()


def test_refuses_full_world_download_and_incomplete_ranges(tmp_path,source):
    url,_=source;cache=TileCache(tmp_path)
    with pytest.raises(ValueError,match='частичную'):cache.read_range(url+'/ignores-range',0,127)
    with pytest.raises(ValueError,match='прервана'):cache.read_range(url+'/map',0,100000)
    assert cache.cached(f'range:{url}/map:0:100000') is None
    with pytest.raises(ValueError):cache.read_range(url,-1,10)
    with pytest.raises(ValueError):cache.tile(20,0,0)
    cache.close()


def test_expired_archive_retries_including_header(tmp_path,source,monkeypatch):
    url,_=source;cache=TileCache(tmp_path);calls=[]
    def source_url(refresh=False):calls.append(refresh);return url+('/map' if refresh else '/gone')
    monkeypatch.setattr(cache,'source_url',source_url)
    assert cache.tile(0,0,0)==b'tile:0:0:0'
    assert calls==[False,True]
    cache.close()


def test_fonts_and_sprites_survive_restart_without_requests(tmp_path,monkeypatch):
    cache=TileCache(tmp_path)
    cache.save('fonts:Noto Sans Regular/1024-1279.pbf',b'font')
    cache.save('sprites:v4/light.png',b'sprite');cache.close()
    cache=TileCache(tmp_path)
    def offline(*args,**kwargs):raise AssertionError('No network for existing resources')
    monkeypatch.setattr(requests.Session,'get',offline)
    assert cache.resource('fonts','Noto Sans Regular/1024-1279.pbf')==b'font'
    assert cache.resource('sprites','v4/light.png')==b'sprite'
    cache.close()


def test_world_preparation_is_complete_and_resumable(tmp_path,monkeypatch):
    seen=[];cache=TileCache(tmp_path,seen.append);calls=[]
    monkeypatch.setattr(cache,'tile',lambda *xyz:calls.append(xyz) or b'')
    monkeypatch.setattr(cache,'resource',lambda *args:b'')
    cache.prepare_base();assert len(set(calls))==341 and cache.base_ready()
    cache.prepare_base();assert len(calls)==341 and seen[-1]['state']=='ready'
    cache.close()


def test_local_server_does_not_serve_files_or_accept_external_paths(tmp_path):
    from fotoarchive.map_server import MapServer
    server=MapServer(tmp_path)
    try:
        assert requests.get(server.url+'viewer.js').status_code==200
        assert requests.get(server.origin+'/viewer.js').status_code==404
        for name in ('%2e%2e/telegram_contacts.json','https://example.com','tiles/99/0/0.mvt'):
            assert requests.get(server.url+name).status_code in (404,503)
        server.cache.save('tile:0:0:0',b'MVT')
        assert requests.get(server.url+'tiles/0/0/0.mvt').content==b'MVT'
    finally:server.close()


def test_map_resizes_and_remembers_height(qtbot,tmp_path,monkeypatch):
    from fotoarchive.config import Settings
    from fotoarchive.ui import MainWindow
    from fotoarchive.map_view import MapView
    from test_ui import FakeBackend
    # Native D3D rendering has its own acceptance process. This test isolates
    # the real splitter/persistence from network and the offscreen GL backend.
    monkeypatch.setattr(MapView,'start',lambda self:None)
    cfg=Settings(data_dir=tmp_path/'data')
    w=MainWindow(cfg,FakeBackend());qtbot.addWidget(w);w.resize(1480,1100);w.show()
    w.map_toggle.setChecked(True);qtbot.wait(30)
    handle=w.canvas_splitter.handle(1);before=w.canvas_splitter.sizes()[0]
    qtbot.mousePress(handle,Qt.LeftButton,pos=QPoint(100,4))
    qtbot.mouseMove(handle,QPoint(100,134))
    qtbot.mouseRelease(handle,Qt.LeftButton,pos=QPoint(100,134))
    after=w.canvas_splitter.sizes()[0]
    assert after>before+80 and w.preferences.values['map_height']==after
    w.map_toggle.setChecked(False);w.map_toggle.setChecked(True)
    assert abs(w.canvas_splitter.sizes()[0]-after)<=1
    w.close()
    w=MainWindow(cfg,FakeBackend());qtbot.addWidget(w);w.resize(1480,1100);w.show()
    w.map_toggle.setChecked(True)
    assert abs(w.canvas_splitter.sizes()[0]-after)<=1
    w.close()
