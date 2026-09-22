from PySide6.QtCore import Qt,QPoint
from PySide6.QtWidgets import QApplication
from PIL import Image
from fotoarchive.config import Settings
from fotoarchive.ui import MainWindow,Viewer
from fotoarchive.video_player import MomentSlider
from test_ui import FakeBackend


def test_video_track_click_seeks_immediately_and_still_drags(qtbot):
    slider=MomentSlider([]);qtbot.addWidget(slider)
    slider.resize(640,32);slider.setRange(0,120000);slider.show()
    seeks=[];slider.sliderMoved.connect(seeks.append)
    qtbot.mouseClick(slider,Qt.LeftButton,pos=QPoint(480,16))
    assert 88000<slider.value()<93000 and seeks[-1]==slider.value()
    assert not slider.isSliderDown()
    qtbot.mousePress(slider,Qt.LeftButton,pos=QPoint(320,16))
    qtbot.mouseMove(slider,QPoint(160,16))
    qtbot.mouseRelease(slider,Qt.LeftButton,pos=QPoint(160,16))
    assert 27000<slider.value()<32000
    qtbot.mouseClick(slider,Qt.LeftButton,pos=QPoint(0,16));assert slider.value()==0
    qtbot.mouseClick(slider,Qt.LeftButton,pos=QPoint(639,16));assert slider.value()==120000


def test_details_button_restores_panel_after_hidden_layout_is_saved(qtbot,tmp_path):
    cfg=Settings(data_dir=tmp_path)
    window=MainWindow(cfg,FakeBackend());qtbot.addWidget(window);window.show()
    window.details_toggle.setChecked(False);QApplication.processEvents()
    window.persist_workspace();window.close()
    window=MainWindow(cfg,FakeBackend());qtbot.addWidget(window);window.show()
    qtbot.mouseClick(window.details_toggle,Qt.LeftButton)
    QApplication.processEvents()
    assert window.details_panel.isVisible()
    assert window.splitter.sizes()[2]>=260
    for _ in range(3):
        qtbot.mouseClick(window.details_toggle,Qt.LeftButton)
        qtbot.mouseClick(window.details_toggle,Qt.LeftButton)
        QApplication.processEvents()
        assert window.splitter.sizes()[2]>=260
    window.close()


def test_viewer_date_and_icon_controls_follow_the_current_photo(qtbot,tmp_path):
    path=tmp_path/'photo.jpg';Image.new('RGB',(80,60),'blue').save(path)
    items=[dict(id=i,version=1,filename=f'photo-{i}.jpg',path=str(path),
                captured_at=date) for i,date in enumerate(('2002-08-03T12:34:56',None,'broken'))]
    viewer=Viewer(items,0,Settings(data_dir=tmp_path/'data'));qtbot.addWidget(viewer);viewer.show()
    qtbot.waitUntil(lambda:bool(viewer.scene.items()))
    assert 'photo-0.jpg · 03.08.2002 12:34:56' in viewer.label.text()
    for button in viewer.controls.values():
        assert button.text()=='' and not button.icon().isNull()
        assert button.toolTip() and button.accessibleName()
    qtbot.mouseClick(viewer.controls['next'],Qt.LeftButton)
    assert 'photo-1.jpg · Дата неизвестна' in viewer.label.text()
    qtbot.mouseClick(viewer.controls['next'],Qt.LeftButton)
    assert 'photo-2.jpg · Дата неизвестна' in viewer.label.text()
    qtbot.mouseClick(viewer.controls['previous'],Qt.LeftButton)
    assert viewer.position==1
    qtbot.mouseClick(viewer.controls['actual'],Qt.LeftButton)
    qtbot.waitUntil(lambda:viewer.original_loaded)
    assert viewer.view.transform().m11()==1
    qtbot.mouseClick(viewer.controls['fit'],Qt.LeftButton)
    assert viewer.fit_mode
    viewer.close()
