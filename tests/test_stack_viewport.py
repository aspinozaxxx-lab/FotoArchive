"""Stack interaction through the real reader and asynchronously arriving pages."""
from datetime import datetime, timedelta

import pytest
from PySide6.QtCore import QObject, Signal, QPoint, Qt
from PySide6.QtWidgets import QListView

from fotoarchive.browse_reader import BrowseReader
from fotoarchive.catalog import Catalog
from fotoarchive.config import Settings
from fotoarchive.ui import MainWindow
from fotoarchive.gallery import PhotoModel


class ReaderBackend(QObject):
    event = Signal(dict)

    def __init__(self, cfg):
        super().__init__()
        self.reader = BrowseReader(cfg, self.event.emit)
        self.reader.enable()
        self.sent = []

    def send(self, **command):
        self.sent.append(command)
        if command['action'] == 'browse':
            self.reader.replace(command)
        elif command['action'] == 'search_page':
            self.reader.page(command)

    def close(self):
        self.reader.close()
        self.reader.thread.join(5)


@pytest.fixture
def stack_window(qtbot, tmp_path):
    cfg = Settings(data_dir=tmp_path/'data', root=tmp_path/'synthetic')
    cat = Catalog(cfg)
    records = []
    for i in range(9000):
        captured = (datetime(2020,1,1)+timedelta(seconds=i//3*20+i%3)).isoformat()
        width,height = [(1200,800),(600,1200),(1800,600),(800,800),(900,1200)][i%5]
        records.append((i+1,cat.source_id,f'{i}.jpg',str(i),str(i),'2020',f'{i}.jpg',
                        width,height,captured))
    with cat.db:
        cat.db.executemany('''INSERT INTO assets(id,source_id,relative_path,path_key,path,folder,filename,
            extension,size,mtime_ns,metadata_ready,width,height,captured_at,camera,media_kind,updated_at)
            VALUES(?,?,?,?,?,?,?,'.jpg',100,0,1,?,?,?,'camera','photo',0)''',records)
    cat.close()
    backend = ReaderBackend(cfg)
    w = MainWindow(cfg,backend)
    qtbot.addWidget(w)
    w.resize(1480,940)
    w.thumbnail_size.setValue(152)
    w.layout_combo.setCurrentIndex(1)
    w.show()
    w.search()
    qtbot.waitUntil(lambda:not w.loading and w.model.rowCount()>=200,timeout=10000)
    yield w
    w.close()


@pytest.mark.parametrize('row',[275,1775,2197])
def test_deep_stack_with_evicted_pages_keeps_viewport(qtbot,stack_window,row):
    w = stack_window
    # Traverse real pages, rather than pre-filling a model or seeking one sparse
    # page. This evicts old records and preserves the geometry of visited rows.
    while w.model.rowCount()<min(3000,row+600):
        count=w.model.rowCount()
        w.model.fetchMore()
        qtbot.waitUntil(lambda:w.model.rowCount()>count,timeout=5000)
    # A reopened/history-restored view knows how many rows were exposed, but
    # only one page of actual records arrives initially. The other pages are
    # decoded on demand when the user drags the scrollbar into this range.
    w.search(preserve_position=True)
    qtbot.waitUntil(lambda:not w.loading,timeout=10000)
    qtbot.wait(100)
    w.gallery.scrollTo(w.model.index(row),QListView.PositionAtCenter)
    qtbot.waitUntil(lambda:w.model.asset(row) is not None)
    qtbot.wait(250)
    asset=w.model.asset(row)
    key=asset['stack_key']
    original_rect=w.gallery.visualRect(w.model.index(row))
    before_id=asset['id']
    for expanded in (True,False,True,False):
        rect=w.gallery.visualRect(w.model.index(row))
        assert rect.intersects(w.gallery.viewport().rect())
        qtbot.mouseClick(w.gallery.viewport(),Qt.LeftButton,pos=rect.topLeft()+QPoint(25,22))
        qtbot.waitUntil(lambda:not w.loading and w._stack_change is None,timeout=10000)
        qtbot.wait(1000)
        assert w.model.asset(row)['id']==before_id
        actual=w.gallery.visualRect(w.model.index(row))
        assert (actual.x(),actual.y())==(original_rect.x(),original_rect.y()), (
            row, expanded, tuple(w.model.blocks), len(w.model.geometry),
            (actual.x(),actual.y()),(original_rect.x(),original_rect.y()))


def test_layout_roles_do_not_fetch_offscreen_records(qtbot):
    model=PhotoModel()
    model.reset_result([],200000,False,loaded_count=100000)
    requests=[]
    model.pageRequested.connect(requests.append)
    for role in (Qt.SizeHintRole,Qt.FontRole,Qt.DecorationRole,Qt.TextAlignmentRole,Qt.BackgroundRole):
        assert model.data(model.index(40000),role) is None
    assert requests==[]
    model.data(model.index(40000),PhotoModel.AssetRole)
    assert requests==[40000]


def test_page_append_while_scrolled_keeps_position(qtbot,stack_window):
    w=stack_window
    while w.model.rowCount()<1000:
        count=w.model.rowCount()
        w.model.fetchMore()
        qtbot.waitUntil(lambda:w.model.rowCount()>count,timeout=5000)
    qtbot.wait(200)
    w.gallery.scrollTo(w.model.index(77),QListView.PositionAtCenter)
    qtbot.wait(100)
    for row in (747,1090,1490):
        qtbot.waitUntil(lambda:w.gallery.visualRect(w.model.index(w.model.rowCount()-1)).isValid())
        w.gallery.scrollTo(w.model.index(row),QListView.PositionAtCenter)
        qtbot.waitUntil(lambda:w.model.asset(row) is not None)
        before=w.gallery.visualRect(w.model.index(row)).topLeft()
        count=w.model.rowCount()
        w.model.fetchMore()
        qtbot.waitUntil(lambda:w.model.rowCount()>count,timeout=5000)
        qtbot.wait(1000)
        assert w.gallery.visualRect(w.model.index(row)).topLeft()==before
        while w.model.rowCount()<row+500:
            count=w.model.rowCount()
            w.model.fetchMore()
            qtbot.waitUntil(lambda:w.model.rowCount()>count,timeout=5000)
