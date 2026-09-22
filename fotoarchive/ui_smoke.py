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
        def __init__(self):
            super().__init__()
            self.commands=[]
        def send(self,**command): self.commands.append(command)
        def close(self): pass

    folder.mkdir(parents=True,exist_ok=True)
    app=QApplication([])
    app.setStyle('Fusion')
    w=MainWindow(Settings(data_dir=folder),Backend())
    w.resize(1300,900)
    w.show()
    report=dict(version=__version__,passed=False,checks=[],scope='generated rows only; no catalogue, models or source files')
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
            y=w.gallery.visualRect(w.model.index(190)).y()
            for expanded in (True,False):
                w.toggle_stack('b:test',190)
                command=next(c for c in reversed(w.backend.commands) if c['action']=='browse')
                anchor=command['refresh_anchor']
                assert anchor['asset_id']==190
                rows=items[:191]+([items[190]|dict(id=500+i,stack_expanded=True) for i in range(3)] if expanded else [])+items[191:]
                rows[190]=rows[190]|dict(stack_expanded=expanded)
                w.on_event(dict(type='results',id=w.request_id,items=rows[:200],total=403,page_total=len(rows),
                    restore=dict(row=190,selected_row=190,offset_y=y,loaded_count=len(rows))))
                pump(lambda:w._stack_change is None and not w.gallery.stack_motion.isVisible())
                error=abs(w.gallery.visualRect(w.model.index(190)).y()-y)
                assert error<=1, error
                report['checks'].append(dict(mode=mode,expanded=expanded,anchor_error_px=error))
        w.grab().save(str(folder/'ui-smoke.png'))
        report['passed']=True
    except Exception as exc:
        report['error']=repr(exc)
    finally:
        w.close()
    (folder/'ui-smoke.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    return 0 if report['passed'] else 1
