import time
from PySide6.QtGui import QImage
from PIL import Image
from fotoarchive.config import Settings
from fotoarchive.ui import Viewer
from fotoarchive.preview_buffer import PreviewBuffer,preview_key


def test_five_both_sides_ready_and_navigation_does_not_decode(qtbot,tmp_path,monkeypatch):
    items=[]
    for i in range(15):
        path=tmp_path/f'{i}.png'
        Image.new('RGB',(640+i,480),'red').save(path)
        items.append(dict(id=i,version=1,path=str(path),filename=path.name))
    viewer=Viewer(items,7,Settings(data_dir=tmp_path/'data'))
    qtbot.addWidget(viewer);viewer.show()
    qtbot.waitUntil(lambda:len(viewer.preview_buffer.cache)==11,timeout=10000)
    assert {key[0] for key in viewer.preview_buffer.cache}==set(range(2,13))
    assert sum(image.sizeInBytes() for image in viewer.preview_buffer.cache.values())<=PreviewBuffer.BUDGET
    times=[]
    for step in (1,1,-1,-1,-1,-1):
        start=time.perf_counter();viewer.navigate(step);times.append(time.perf_counter()-start)
        assert viewer.scene.sceneRect().width()==640+viewer.position
    assert max(times)<.08
    viewer.close()


def test_foreground_does_not_wait_for_background_and_close_is_immediate(qtbot,tmp_path,monkeypatch):
    import threading
    blocker=threading.Event();calls=[]
    from fotoarchive.preview_buffer import Read
    def slow(self):
        calls.append(self.asset['id'])
        if self.asset['id']==1:
            blocker.wait(3)
        image=QImage(60,40,QImage.Format_RGB888);image.fill(0)
        self.signals.ready.emit(preview_key(self.asset),image,'')
    monkeypatch.setattr(Read,'run',slow)
    buffer=PreviewBuffer(Settings(data_dir=tmp_path))
    assets=[dict(id=i,version=1,path=str(i)) for i in range(3)]
    try:
        buffer.prepare(assets[0],[assets[1]])
        qtbot.waitUntil(lambda:1 in calls)
        buffer.prepare(assets[2],[])
        qtbot.waitUntil(lambda:buffer.get(assets[2]) is not None,timeout=500)
        start=time.perf_counter();buffer.close()
        assert time.perf_counter()-start<.05
    finally:
        blocker.set()


def test_direction_change_keeps_all_overlapping_decoded_images(qtbot,tmp_path,monkeypatch):
    from fotoarchive.preview_buffer import Read
    calls=[]
    def read(self):
        calls.append(self.asset['id'])
        image=QImage(20,20,QImage.Format_RGB888);image.fill(0)
        self.signals.ready.emit(preview_key(self.asset),image,'')
    monkeypatch.setattr(Read,'run',read)
    assets=[dict(id=i,version=1,path=str(i)) for i in range(13)]
    buffer=PreviewBuffer(Settings(data_dir=tmp_path))
    buffer.prepare(assets[5],assets[:5]+assets[6:11])
    qtbot.waitUntil(lambda:len(buffer.cache)==11)
    buffer.prepare(assets[6],assets[1:6]+assets[7:12])
    qtbot.waitUntil(lambda:preview_key(assets[11]) in buffer.cache)
    count=len(calls)
    buffer.prepare(assets[5],assets[:5]+assets[6:11])
    qtbot.waitUntil(lambda:len(buffer.cache)==11)
    assert calls[count:]==[0]
    buffer.close()
