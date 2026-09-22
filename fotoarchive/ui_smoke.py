"""Frozen-application acceptance check without opening any real catalogue."""
import json
import os
import time
from pathlib import Path


def run(folder):
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    os.environ.setdefault('QT_QPA_FONTDIR',str(Path(os.environ.get('WINDIR',r'C:\Windows'))/'Fonts'))
    from PySide6.QtCore import QObject,Signal
    from PySide6.QtGui import QColor,QPixmap
    from PySide6.QtWidgets import QApplication,QListView
    from .config import Settings
    from .ui import MainWindow
    from . import __version__

    class Backend(QObject):
        event=Signal(dict)
        def __init__(self, reader_cfg=None):
            super().__init__()
            self.commands=[]
            self.reader=None
            if reader_cfg:
                from .browse_reader import BrowseReader
                self.reader=BrowseReader(reader_cfg,self.event.emit)
                self.reader.enable()
        def send(self,**command):
            self.commands.append(command)
            if self.reader:
                if command['action']=='browse': self.reader.replace(command)
                elif command['action']=='search_page': self.reader.page(command)
        def close(self):
            if self.reader:
                self.reader.close()
                self.reader.thread.join(5)

    folder.mkdir(parents=True,exist_ok=True)
    app=QApplication([])
    app.setStyle('Fusion')
    w=MainWindow(Settings(data_dir=folder),Backend())
    w.resize(1300,900)
    w.show()
    report=dict(version=__version__,passed=False,checks=[],scope='generated rows and isolated synthetic SQLite; no real catalogue, models or source files')
    def pump(predicate):
        deadline=time.monotonic()+5
        while not predicate():
            app.processEvents()
            if time.monotonic()>deadline: raise TimeoutError('Qt layout or animation stalled')
            time.sleep(.002)
        app.processEvents()
    try:
        items=[dict(id=i,version=1,filename=f'{i}.jpg',relative_path=f'synthetic/{i}.jpg',
                    width=400+i%3*300,height=600,extension='.jpg',size=100,thumbnail='synthetic',media_kind='photo') for i in range(400)]
        items[190].update(stack_key='b:test',stack_count=4)
        pix=QPixmap(400,600);pix.fill(QColor('#168f82'));w.model.cache['synthetic']=pix
        for mode in (0,1,2):
            w.layout_combo.setCurrentIndex(mode)
            w.on_event(dict(type='results',id=w.request_id,items=items[:200],total=403,page_total=400))
            w.model.accept_page(200,items[200:],400,False)
            pump(lambda:w.gallery.visualRect(w.model.index(399)).isValid())
            w.gallery.scrollTo(w.model.index(190),QListView.PositionAtCenter)
            point=w.gallery.visualRect(w.model.index(190)).topLeft()
            for expanded in (True,False):
                w.toggle_stack('b:test',190)
                command=next(c for c in reversed(w.backend.commands) if c['action']=='browse')
                anchor=command['refresh_anchor']
                assert anchor['asset_id']==190
                rows=items[:191]+([items[190]|dict(id=500+i,stack_expanded=True) for i in range(3)] if expanded else [])+items[191:]
                rows[190]=rows[190]|dict(stack_expanded=expanded)
                w.on_event(dict(type='results',id=w.request_id,items=rows[:200],total=403,page_total=len(rows),
                    restore=dict(row=190,selected_row=190,offset_y=point.y(),loaded_count=len(rows))))
                pump(lambda:w._stack_change is None and not w.gallery.stack_motion.isVisible())
                after=w.gallery.visualRect(w.model.index(190)).topLeft()
                error=max(abs(after.x()-point.x()),abs(after.y()-point.y()))
                assert error<=1, error
                report['checks'].append(dict(mode=mode,expanded=expanded,anchor_error_px=error))
        w.grab().save(str(folder/'ui-smoke.png'))
        w.close()
        # Exercise the actual reader, a restored virtual range and cache eviction.
        # The older 400-row check alone missed guessed widths in distant pages.
        from .catalog import Catalog
        from datetime import datetime,timedelta
        cfg=Settings(data_dir=folder/'synthetic-catalog',root=folder/'synthetic-source')
        cat=Catalog(cfg)
        try:
            count=cat.db.execute('SELECT count(*) FROM assets').fetchone()[0]
            if not count:
                with cat.db:
                    cat.db.executemany('''INSERT INTO assets(id,source_id,relative_path,path_key,path,folder,filename,
                        extension,size,mtime_ns,metadata_ready,width,height,captured_at,camera,media_kind,thumbnail,updated_at)
                        VALUES(?,?,?,?,?,?,?,'.jpg',100,0,1,?,?,?,'camera','photo','synthetic',0)''',
                        [(i+1,cat.source_id,f'{i}.jpg',str(i),str(i),'2020',f'{i}.jpg',
                          *[(1200,800),(600,1200),(1800,600),(800,800),(900,1200)][i%5],
                          (datetime(2020,1,1)+timedelta(seconds=i//3*20+i%3)).isoformat()) for i in range(9000)])
            elif count!=9000:
                raise ValueError('Unexpected contents in synthetic test catalogue')
        finally:
            cat.close()
        w=MainWindow(cfg,Backend(cfg))
        w.resize(1480,940)
        w.thumbnail_size.setValue(152)
        w.model.cache['synthetic']=pix
        w.show()
        w.search()
        pump(lambda:not w.loading and w.model.rowCount()>=200)
        while w.model.rowCount()<2800:
            count=w.model.rowCount()
            w.model.fetchMore()
            pump(lambda:w.model.rowCount()>count)
        pump(lambda:w.gallery.visualRect(w.model.index(w.model.rowCount()-1)).isValid())
        w.gallery.scrollTo(w.model.index(2197),QListView.PositionAtCenter)
        pump(lambda:w.model.asset(2197) is not None)
        before=w.gallery.visualRect(w.model.index(2197)).topLeft()
        count=w.model.rowCount()
        w.model.fetchMore()
        pump(lambda:w.model.rowCount()>count)
        pump(lambda:w.gallery.visualRect(w.model.index(w.model.rowCount()-1)).isValid())
        after=w.gallery.visualRect(w.model.index(2197)).topLeft()
        error=max(abs(after.x()-before.x()),abs(after.y()-before.y()))
        assert error==0, ('append',error)
        report['append_check']=dict(row=2197,anchor_error_px=error)
        report['async_checks']=[]
        for mode in (0,1,2):
            w.layout_combo.setCurrentIndex(mode)
            pump(lambda:w.gallery.visualRect(w.model.index(w.model.rowCount()-1)).isValid())
            for row in (275,2197):
                w.search(preserve_position=True)
                pump(lambda:not w.loading)
                pump(lambda:w.gallery.visualRect(w.model.index(w.model.rowCount()-1)).isValid())
                w.gallery.scrollTo(w.model.index(row),QListView.PositionAtCenter)
                pump(lambda:w.model.asset(row) is not None)
                asset=w.model.asset(row)
                before=w.gallery.visualRect(w.model.index(row)).topLeft()
                for expanded in (True,False):
                    w.toggle_stack(asset['stack_key'],row)
                    pump(lambda:not w.loading and w._stack_change is None and not w.gallery.stack_motion.isVisible())
                    after=w.gallery.visualRect(w.model.index(row)).topLeft()
                    error=max(abs(after.x()-before.x()),abs(after.y()-before.y()))
                    assert error==0, (mode,row,expanded,error,(before.x(),before.y()),(after.x(),after.y()))
                    report['async_checks'].append(dict(mode=mode,row=row,expanded=expanded,anchor_error_px=error))
        report['passed']=True
    except Exception as exc:
        report['error']=repr(exc)
    finally:
        w.close()
    (folder/'ui-smoke.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    return 0 if report['passed'] else 1
