"""Persistent, read-through vector map cache. Existing tiles never re-download."""
from collections import OrderedDict
from contextlib import contextmanager
import gzip
import json
from pathlib import Path
import sqlite3
import threading
import queue
from urllib.parse import quote

import requests
from pmtiles.reader import Reader


class TileCache:
    def __init__(self,directory,notify=lambda status:None):
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
        self.path=self.directory/'tiles.sqlite3';self.notify=notify
        self.stopped=threading.Event();self.local=threading.local()
        self.source_lock=threading.Lock();self.locks=[threading.Lock() for _ in range(32)]
        self.memory=OrderedDict();self.memory_lock=threading.Lock();self.source=None
        with self.db() as db:
            db.executescript('''PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS content(key TEXT PRIMARY KEY,data BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY,value TEXT NOT NULL);''')

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=10)
        try:
            with db:yield db
        finally:db.close()

    def cached(self,key):
        with self.memory_lock:
            if key in self.memory:
                self.memory.move_to_end(key);return self.memory[key]
        with self.db() as db:row=db.execute('SELECT data FROM content WHERE key=?',(key,)).fetchone()
        if row is not None:
            self.remember(key,row[0]);return row[0]

    def remember(self,key,value):
        with self.memory_lock:
            self.memory[key]=value
            while len(self.memory)>128:self.memory.popitem(last=False)

    def save(self,key,value):
        with self.db() as db:db.execute('INSERT OR IGNORE INTO content VALUES(?,?)',(key,value))
        self.remember(key,value)
        return value

    def http(self):
        if not hasattr(self.local,'session'):
            self.local.session=requests.Session()
            self.local.session.headers['User-Agent']='FotoArchive/0.8.2 offline map cache'
        return self.local.session

    def source_url(self,refresh=False):
        with self.source_lock:
            if self.source and not refresh:return self.source
            with self.db() as db:
                old=db.execute("SELECT value FROM config WHERE key='source'").fetchone()
                if old and not refresh:self.source=old[0];return self.source
                if self.stopped.is_set():raise InterruptedError()
                response=self.http().get('https://build-metadata.protomaps.dev/builds.json',timeout=(5,12))
                response.raise_for_status()
                builds=[item for item in response.json() if str(item.get('version','')).startswith('4.')
                        and len(item.get('key',''))==16 and item['key'].endswith('.pmtiles') and item['key'][:8].isdigit()]
                if not builds:raise ValueError('Нет подходящего пакета карты')
                latest=max(builds,key=lambda item:item['key'])
                self.source='https://build.protomaps.com/'+latest['key']
                db.execute("INSERT OR REPLACE INTO config VALUES('source',?)",(self.source,))
                return self.source

    def read_range(self,url,offset,length,persist=True):
        if offset<0 or not 0<length<=16*1024**2:raise ValueError('Некорректный участок карты')
        key=f'range:{url}:{offset}:{length}'
        if persist:
            cached=self.cached(key)
            if cached is not None:return cached
        if self.stopped.is_set():raise InterruptedError()
        with self.http().get(url,headers={'Range':f'bytes={offset}-{offset+length-1}'},timeout=(5,12),stream=True) as response:
            response.raise_for_status()
            if response.status_code!=206 or not response.headers.get('Content-Range','').startswith(f'bytes {offset}-{offset+length-1}/'):
                raise ValueError('Сервер не поддерживает частичную загрузку карты')
            data=response.raw.read(length+1)
            if len(data)!=length:raise ValueError('Загрузка участка карты прервана')
        return self.save(key,data) if persist else data

    def tile(self,z,x,y):
        if not 0<=z<=15 or not (0<=x<2**z and 0<=y<2**z):raise ValueError('Некорректный тайл')
        key=f'tile:{z}:{x}:{y}'
        value=self.cached(key)
        if value is not None:return value
        with self.locks[hash(key)%len(self.locks)]:
            value=self.cached(key)
            if value is not None:return value
            for attempt in range(2):
                url=self.source_url(refresh=bool(attempt))
                try:
                    header=Reader(lambda offset,length:self.read_range(url,offset,length)).header()
                    reader=Reader(lambda offset,length:self.read_range(url,offset,length,offset<header['tile_data_offset']))
                    value=reader.get(z,x,y) or b''
                except requests.HTTPError as exc:
                    if exc.response.status_code==404 and attempt==0:continue
                    raise
                if value[:2]==b'\x1f\x8b':value=gzip.decompress(value)
                return self.save(key,value)
        raise ValueError('Не удалось загрузить участок карты')

    def resource(self,kind,name):
        key=f'{kind}:{name}'
        cached=self.cached(key)
        if cached is not None:return cached
        with self.locks[hash(key)%len(self.locks)]:
            cached=self.cached(key)
            if cached is not None:return cached
            if self.stopped.is_set():raise InterruptedError()
            url='https://protomaps.github.io/basemaps-assets/'+kind+'/'+quote(name,safe='/')
            with self.http().get(url,timeout=(5,12),stream=True) as response:
                response.raise_for_status();content=response.raw.read(12*1024**2+1,decode_content=True)
                if len(content)>12*1024**2:raise ValueError('Слишком большой ресурс карты')
                length=response.headers.get('Content-Length')
                if length and not response.headers.get('Content-Encoding') and len(content)!=int(length):
                    raise ValueError('Загрузка ресурса карты прервана')
            return self.save(key,content)

    def base_ready(self):
        with self.db() as db:
            return db.execute("SELECT value FROM config WHERE key='base_ready'").fetchone() is not None

    def prepare_base(self):
        if self.base_ready():
            self.notify(dict(state='ready',message='Базовая карта сохранена'));return
        total=sum(4**z for z in range(5));done=0
        try:
            # Daemon workers never hold application exit while a network read
            # is pending. Only 341 base tiles, at most two downloads in flight.
            jobs=queue.Queue();results=queue.Queue();aborted=threading.Event()
            for z in range(5):
                for x in range(2**z):
                    for y in range(2**z):jobs.put((z,x,y))
            def download():
                while not self.stopped.is_set() and not aborted.is_set():
                    try:tile=jobs.get_nowait()
                    except queue.Empty:return
                    try:self.tile(*tile);results.put(None)
                    except Exception as exc:aborted.set();results.put(exc);return
            for _ in range(2):threading.Thread(target=download,name='base-map',daemon=True).start()
            while done<total:
                if self.stopped.is_set():return
                try:result=results.get(timeout=.2)
                except queue.Empty:continue
                if result is not None:raise result
                done+=1
                if done%8==0 or done==total:
                    self.notify(dict(state='building',message=f'Базовая карта мира: {done} / {total}'))
            for font in ('Noto Sans Regular','Noto Sans Medium','Noto Sans Italic'):
                for start in (0,256,512,768,1024,1280):self.resource('fonts',f'{font}/{start}-{start+255}.pbf')
            for theme in ('light','dark'):
                for suffix in ('.json','.png','@2x.json','@2x.png'):self.resource('sprites',f'v4/{theme}{suffix}')
            with self.db() as db:db.execute("INSERT OR REPLACE INTO config VALUES('base_ready','1')")
            self.notify(dict(state='ready',message='Базовая карта сохранена'))
        except (OSError,ValueError,requests.RequestException):
            if not self.stopped.is_set():
                self.notify(dict(state='error',message='Сохранённая карта доступна. Для новых участков нужно подключение к интернету.'))

    def close(self):
        self.stopped.set()
