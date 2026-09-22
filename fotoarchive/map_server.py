"""Loopback-only, token-scoped map resources; no photos or general file serving."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import unquote,urlsplit

from .map_tiles import TileCache


class MapServer:
    def __init__(self,directory,notify=lambda _:None):
        self.cache=TileCache(directory,notify);self.token=secrets.token_urlsafe(24)
        self.assets=Path(__file__).with_name('assets')/'map';self.errors=[];self.last_notice=0
        owner=self;self.slots=threading.BoundedSemaphore(8)
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                prefix='/'+owner.token+'/'
                path=unquote(urlsplit(self.path).path)
                if not path.startswith(prefix):self.send_error(404);return
                path=path[len(prefix):]
                try:
                    if path in ('index.html','maplibre-gl.js','maplibre-gl.css','basemaps.js','viewer.js'):
                        data=(owner.assets/path).read_bytes();kind=mimetypes.guess_type(path)[0] or 'application/octet-stream'
                    elif path=='qwebchannel.js':
                        from PySide6.QtCore import QFile,QIODevice
                        file=QFile(':/qtwebchannel/qwebchannel.js')
                        if not file.open(QIODevice.ReadOnly):raise OSError('Missing Qt web channel')
                        data=bytes(file.readAll());file.close();kind='text/javascript'
                    elif match:=re.fullmatch(r'tiles/(\d+)/(\d+)/(\d+)\.mvt',path):
                        with owner.slots:data=owner.cache.tile(*map(int,match.groups()))
                        kind='application/vnd.mapbox-vector-tile'
                    elif match:=re.fullmatch(r'fonts/(Noto Sans (?:Regular|Medium|Italic))/(\d{1,5}-\d{1,5}\.pbf)',path):
                        with owner.slots:data=owner.cache.resource('fonts','/'.join(match.groups()))
                        kind='application/x-protobuf'
                    elif re.fullmatch(r'sprites/v4/(light|dark)(@2x)?\.(json|png)',path):
                        with owner.slots:data=owner.cache.resource('sprites',path[len('sprites/'):])
                        kind='application/json' if path.endswith('.json') else 'image/png'
                    else:self.send_error(404);return
                    self.send_response(200);self.send_header('Content-Type',kind)
                    self.send_header('Content-Length',str(len(data)))
                    self.send_header('Access-Control-Allow-Origin',owner.origin)
                    self.send_header('Cache-Control','public,max-age=31536000,immutable' if path.endswith(('.mvt','.pbf')) else 'no-cache')
                    self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(data)
                except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
                except Exception as exc:
                    owner.errors.append(path+': '+type(exc).__name__+' '+str(exc)[:180])
                    owner.errors=owner.errors[-20:]
                    if not owner.cache.stopped.is_set() and time.monotonic()-owner.last_notice>3:
                        owner.last_notice=time.monotonic()
                        notify(dict(state='error',message='Сохранённая карта доступна. Новые участки пока не загрузились.'))
                    try:self.send_error(503,'Map data unavailable')
                    except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
            def log_message(self,*args):pass
        self.http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.http.daemon_threads=True
        self.origin=f'http://127.0.0.1:{self.http.server_port}'
        self.url=self.origin+'/'+self.token+'/'
        self.thread=threading.Thread(target=lambda:self.http.serve_forever(poll_interval=.05),name='map-http',daemon=True);self.thread.start()
        self.preparing=None

    def ensure(self):
        if self.preparing and self.preparing.is_alive():return
        self.preparing=threading.Thread(target=self.cache.prepare_base,name='map-world',daemon=True)
        self.preparing.start()

    def close(self):
        self.cache.close();self.http.shutdown();self.http.server_close();self.thread.join(1)
