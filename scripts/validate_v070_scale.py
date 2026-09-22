"""200k synthetic records, real Qt rendering, concurrent writer, latency and RAM."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
os.environ.setdefault('QT_QPA_FONTDIR',r'C:\Windows\Fonts')
import json
from pathlib import Path
import sys
import sqlite3
from threading import Event,Thread
import time
from datetime import datetime,timedelta
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
import psutil
from PIL import Image,ImageDraw
from fotoarchive.config import Settings
from fotoarchive.catalog import Catalog,Filters
from fotoarchive.browse_reader import BrowseViews,BrowseReader
from fotoarchive.library import Library,LibraryReader
from PySide6.QtCore import QObject,Signal,QTimer,QEvent,QCoreApplication,Qt
QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
from PySide6.QtWidgets import QApplication
from fotoarchive.ui import MainWindow

base=Path(r'D:\FotoArchiveData\reports\v070-scale')
report_dir=Path(os.environ.get('FOTOARCHIVE_BENCHMARK_REPORT',str(base)))
report_dir.mkdir(parents=True,exist_ok=True)
cfg=Settings(data_dir=base/'catalog',root=base/'synthetic',includes=['2000'])
cfg.initialize()
cfg.save()
thumbs=[]
for n in range(256):
    thumb=base/f'synthetic-thumb-{n:03}.jpg'
    thumbs.append(str(thumb))
    if not thumb.exists():
        size=[(480,360),(320,480),(480,270),(400,400)][n%4]
        im=Image.new('RGB',size,(40+n%90,65+n%120,80+n%100))
        draw=ImageDraw.Draw(im)
        draw.polygon([(0,size[1]),(size[0]//2,80),(size[0],size[1])],fill='#648678')
        draw.ellipse((size[0]-110,40,size[0]-50,100),fill='#e7bf75')
        draw.text((25,30),f'Test {n:03}',fill='white')
        im.save(thumb)
cat=Catalog(cfg)
if cat.db.execute('SELECT count(*) FROM assets').fetchone()[0]!=200000:
    for start in range(0,200000,2000):
        records=[]
        for i in range(start,start+2000):
            group=i//5
            year=2000+(group//1600)%25
            stamp=datetime(year,1,1)+timedelta(seconds=(group%1600)*120+i%5)
            folder=f'{year}/event_{group//100:04}'
            video=i%50==0
            name=f'IMG_{i:07}'+('.mp4' if video else '.jpg')
            records.append((i+1,cat.source_id,folder+'/'+name,f'synthetic:{i}',f'synthetic:{i}',folder,name,
                '.mp4' if video else '.jpg',4000000,i,1,4000,3000,12000000,
                stamp.isoformat() if i%47 else None,'Synthetic camera','video' if video else 'photo',30000 if video else 0,
                str(thumb),'Описание тестовой сцены. '*20,55.0+i%100/100 if i%10==0 else None,
                37.0+i%70/100 if i%10==0 else None,'Москва' if i%10==0 else '',0))
        with cat.db:
            cat.db.executemany('''INSERT OR REPLACE INTO assets(id,source_id,relative_path,path_key,path,folder,filename,extension,
                size,mtime_ns,metadata_ready,width,height,pixels,captured_at,camera,media_kind,duration_ms,thumbnail,description,
                latitude,longitude,geo_text,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',records)
    cat.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    print('200000 synthetic records prepared',flush=True)
if cat.db.execute('SELECT thumbnail FROM assets WHERE id=1').fetchone()[0]!=thumbs[1]:
    with cat.db:
        cat.db.executemany('UPDATE assets SET thumbnail=?,width=?,height=? WHERE id%256=?',
            [(path,*[(4000,3000),(3000,4000),(4000,2250),(3000,3000)][n%4],n) for n,path in enumerate(thumbs)])
facets=cat.facets()
report={'rows':200000,'source':'synthetic only; original archive is not scanned', 'read':{}}
views=BrowseViews(cfg)
options=dict(stacks=True,seconds=2,versions=True,sort='newest')
commands=[dict(action='browse',id=i,filters=filters,presentation=options) for i,filters in enumerate(
    [{},{'media_kind':'photo'},{'media_kind':'video'},{'date_from':'2010-01-01','date_to':'2015-12-31'}],1)]
cold=[]
for command in commands:
    started=time.perf_counter()
    _,session,_=views.select(command,lambda:False)
    page=session.page()
    cold.append(time.perf_counter()-started)
    print('cold',command['filters'],round(cold[-1],3),'cards',page['page_total'],flush=True)
warm=[]
for i in range(40):
    started=time.perf_counter()
    _,session,_=views.select(commands[i%4]|{'id':i+10},lambda:False)
    session.page(offset=0)
    warm.append(time.perf_counter()-started)
report['read']['cold_seconds']=cold
report['read']['warm_p95_seconds']=float(np.percentile(warm,95))
_,session,_=views.select(commands[0],lambda:False)
group=next(a for a in session.page()['items'] if a['stack_count']>1)
expand=[]
for i in range(6):
    started=time.perf_counter()
    _,session,_=views.select(commands[0]|{'presentation':options|{'expanded':[group['stack_key']] if i%2==0 else []}},lambda:False)
    session.page()
    expand.append(time.perf_counter()-started)
report['read']['stack_toggle_p95_seconds']=float(np.percentile(expand,95))
started=time.perf_counter()
nav=Library(cat).navigation(Filters())
report['read']['navigation_seconds']=time.perf_counter()-started
report['folders']=len(nav['folders'])
started=time.perf_counter()
Library(cat).places(Filters())
report['read']['map_seconds']=time.perf_counter()-started
views.close()
cat.close()
print(json.dumps(report['read']),flush=True)

stop=Event()
def writer():
    db=sqlite3.connect(cfg.data_dir/'catalog.sqlite3',timeout=5)
    count=0
    while not stop.wait(.025):
        with db:
            db.execute('UPDATE assets SET description=? WHERE id=?',('Фоновое описание '+str(count),1+count%200000))
        count+=1
    db.close()
thread=Thread(target=writer,daemon=True)
thread.start()

class TestBackend(QObject):
    event=Signal(dict)
    def __init__(self):
        super().__init__()
        self.reader=BrowseReader(cfg,self.event.emit)
        self.library=LibraryReader(cfg,self.event.emit)
        self.reader.enable();self.library.enable()
        self.last_request=0
    def send(self,**command):
        if command['action']=='browse':
            self.last_request=command['id'];self.reader.replace(command)
        elif command['action']=='search_page': self.reader.page(command)
        elif command['action'].startswith('library_'): self.library.send(command)
    def close(self):
        self.reader.close();self.library.close()

slow_events=[]
class MeasuredApplication(QApplication):
    def notify(self,target,event):
        started=time.perf_counter()
        result=super().notify(target,event)
        elapsed=time.perf_counter()-started
        if elapsed>.06 and len(slow_events)<100:
            slow_events.append(dict(seconds=elapsed,target=type(target).__name__,name=target.objectName(),event=int(event.type())))
        return result
app=MeasuredApplication([]) if os.environ.get('FOTOARCHIVE_BENCHMARK_PROFILE') else QApplication([])
app.setStyle('Fusion')
backend=TestBackend()
window=MainWindow(cfg,backend)
window.resize(1480,940)
map_cache=os.environ.get('FOTOARCHIVE_BENCHMARK_MAP_CACHE')
if map_cache:
    window.map_widget.data_dir=Path(map_cache)
    window.setWindowTitle('FotoArchive — нагрузочная проверка 200 000')
window.show()
window.on_event(dict(type='ready',facets=facets,includes=cfg.includes))
ticks=[]
last=[time.perf_counter()]
timer=QTimer()
def tick():
    now=time.perf_counter();ticks.append(now-last[0]);last[0]=now
timer.timeout.connect(tick);timer.start(10)
paint_latencies=[]
started_at=[time.perf_counter()]
awaiting=[0]
class PaintProbe(QObject):
    scheduled=False
    def eventFilter(self,target,event):
        if event.type()==QEvent.Paint and not window.loading and window.model.rowCount() and awaiting[0]==window.request_id:
            from PySide6.QtCore import QPoint
            top=window.gallery.indexAt(QPoint(5,5))
            first=top.row() if top.isValid() else 0
            visible=[]
            for row in range(max(0,first-8),min(window.model.rowCount(),first+100)):
                if window.gallery.visualRect(window.model.index(row)).intersects(target.rect()):
                    asset=window.model.asset(row)
                    if not asset or str(asset.get('thumbnail')) not in window.model.cache:
                        return False
                    visible.append(asset)
            if visible and not self.scheduled:
                self.scheduled=True
                request=window.request_id
                def complete():
                    self.scheduled=False
                    if awaiting[0]==request==window.request_id:
                        paint_latencies.append(time.perf_counter()-started_at[0]);awaiting[0]=-1
                QTimer.singleShot(0,complete)
        return False
probe=PaintProbe()
window.gallery.viewport().installEventFilter(probe)
def pump(predicate,timeout=20):
    deadline=time.monotonic()+timeout
    while not predicate():
        app.processEvents()
        if time.monotonic()>deadline: raise TimeoutError('Qt view did not finish')
        time.sleep(.001)
    for _ in range(12): app.processEvents();time.sleep(.002)
pump(lambda:awaiting[0]==-1)
initial_paint=paint_latencies.pop()
if map_cache:
    window.map_toggle.setChecked(True)
    pump(lambda:window.map_widget.ready,timeout=40)
    ticks.clear();last[0]=time.perf_counter()
    report['detailed_map_visible']=True
for i in range(30):
    started_at[0]=time.perf_counter()
    window.media_combo.setCurrentIndex((window.media_combo.currentIndex()+1)%3)
    awaiting[0]=window.request_id
    pump(lambda:awaiting[0]==-1)
switch_ticks=ticks[:]
# Deep, sparse page loading uses the same path as a restored scroll position.
window.media_combo.setCurrentIndex(0)
pump(lambda:not window.loading)
started=time.perf_counter()
window.model.request_page(40000)
pump(lambda:window.model.rowCount()>=40200)
window.gallery.scrollTo(window.model.index(40050))
pump(lambda:bool(window.model.asset(40050)))
report['deep_page_seconds']=time.perf_counter()-started
anchor=window.capture_anchor()
window.media_combo.setCurrentIndex(2)
pump(lambda:not window.loading)
window.navigate_history(-1)
pump(lambda:not window.loading)
pump(lambda:window.capture_anchor() and window.capture_anchor()['asset_id']==anchor['asset_id'])
report['deep_history_restored']=True
scroll_times=[]
for proportional in (0,1):
    window.layout_combo.setCurrentIndex(proportional)
    for n in range(20):
        started=time.perf_counter()
        window.gallery.verticalScrollBar().setValue(window.gallery.verticalScrollBar().value()+80)
        app.processEvents()
        scroll_times.append(time.perf_counter()-started)
report['scroll_event_p95_seconds']=float(np.percentile(scroll_times,95))
report['bounded_cache']={'pages':len(window.model.blocks),'images':len(window.model.cache),'image_tasks':len(window.model.requested)}
# Real asynchronous stack requests deep in the catalogue, including variable
# widths. A correct row ID alone is not enough: its screen position must stay.
if os.environ.get('FOTOARCHIVE_BENCHMARK_PROFILE'):
    import cProfile,pstats
    profile=cProfile.Profile();profile.enable()
stack_timings=[]
stack_errors=[]
motion_gaps=[]
for layout_mode in (0,1,2):
    window.layout_combo.setCurrentIndex(layout_mode)
    pump(lambda:window.gallery.visualRect(window.model.index(window.model.rowCount()-1)).isValid())
    visible=[]
    for offset,items in list(window.model.blocks.items()):
        for i,asset in enumerate(items):
            rect=window.gallery.visualRect(window.model.index(offset+i))
            if asset.get('stack_count',0)>1 and 30<rect.top()<window.gallery.viewport().height()-80:
                visible.append((offset+i,asset))
    if not visible: raise AssertionError('No visible stack for animation benchmark')
    row,asset=visible[0]
    for _ in range(4):
        before=window.gallery.visualRect(window.model.index(row)).topLeft()
        tick_start=len(ticks)
        started=time.perf_counter()
        window.toggle_stack(asset['stack_key'],row)
        pump(lambda:not window.loading and window._stack_change is None)
        restored=time.perf_counter()-started
        pump(lambda:not window.gallery.stack_motion or not window.gallery.stack_motion.isVisible())
        stack_timings.append(dict(layout=layout_mode,restore_seconds=restored,total_seconds=time.perf_counter()-started))
        after=window.gallery.visualRect(window.model.index(row)).topLeft()
        stack_errors.append(max(abs(after.x()-before.x()),abs(after.y()-before.y())))
        motion_gaps.extend(ticks[tick_start:])
if os.environ.get('FOTOARCHIVE_BENCHMARK_PROFILE'):
    profile.disable();pstats.Stats(profile).sort_stats('cumulative').print_stats(32)
    report['slow_events']=slow_events
report['stack_motion']=dict(samples=stack_timings,max_anchor_error_px=max(stack_errors),
    restore_p95_seconds=float(np.percentile([s['restore_seconds'] for s in stack_timings],95)),
    total_p95_seconds=float(np.percentile([s['total_seconds'] for s in stack_timings],95)),
    event_loop_gap_p95_seconds=float(np.percentile(motion_gaps,95)),event_loop_gap_max_seconds=max(motion_gaps))
window.layout_combo.setCurrentIndex(0)
window.gallery.scrollToTop()
window.search()
pump(lambda:not window.loading)
window.apply_theme('light')
window.grab().save(str(report_dir/'workspace-light.png'))
window.apply_theme('dark')
window.grab().save(str(report_dir/'workspace-dark.png'))
window.map_toggle.setChecked(True)
pump(lambda:bool(window.map_widget.points))
window.grab().save(str(report_dir/'workspace-map.png'))
started=time.perf_counter()
for i in range(100):
    window.media_combo.setCurrentIndex([1,2,0][i%3])
    app.processEvents()
last_request=window.request_id
settle=time.perf_counter()
pump(lambda:not window.loading)
report['rapid_100_switches_seconds']=time.perf_counter()-started
report['rapid_last_switch_settle_seconds']=time.perf_counter()-settle
report['last_request_won']=(window.request_id==last_request and window.filters()['media_kind']=='photo'
    and window.total==196000 and all(a['media_kind']=='photo' for rows in window.model.blocks.values() for a in rows))
report['qt']={'initial_paint_seconds':initial_paint,'filter_paint_p95_seconds':float(np.percentile(paint_latencies[3:],95)),
    'filter_paint_max_seconds':max(paint_latencies[3:]),'event_loop_gap_p95_seconds':float(np.percentile(switch_ticks,95)),
    'event_loop_gap_max_seconds':max(switch_ticks),'rss_mib':psutil.Process().memory_info().rss/1024**2,
    'method':('native with detailed map; ' if map_cache else 'offscreen; ')+'completed Qt paint with all visible thumbnails decoded (256 distinct images), concurrent catalogue writer; event-loop gaps cover startup and media switches only'}
if map_cache:
    process=psutil.Process()
    report['qt']['rss_with_map_children_mib']=sum(p.memory_info().rss for p in [process]+process.children(recursive=True))/1024**2
window.close()
stop.set();thread.join(5)
backend.reader.thread.join(5);backend.library.thread.join(5)
report['passed']=(report['qt']['filter_paint_p95_seconds']<.3 and report['read']['warm_p95_seconds']<.3
    and report['last_request_won'] and report['rapid_last_switch_settle_seconds']<.3
    and report['deep_history_restored'] and report['scroll_event_p95_seconds']<.1
    and report['stack_motion']['max_anchor_error_px']<=1 and report['stack_motion']['restore_p95_seconds']<.6
    and report['bounded_cache']['pages']<=8 and report['bounded_cache']['images']<=256)
(report_dir/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
raise SystemExit(0 if report['passed'] else 1)
