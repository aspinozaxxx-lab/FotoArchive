"""The visible photo and actual pixels survive reflow, reset and screen changes."""
import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QListView
from test_stack_viewport import stack_window


def deep_view(qtbot,w):
    while w.model.rowCount()<2800:
        count=w.model.rowCount();w.model.fetchMore()
        qtbot.waitUntil(lambda:w.model.rowCount()>count,timeout=5000)
    qtbot.waitUntil(lambda:w.gallery.visualRect(w.model.index(w.model.rowCount()-1)).isValid())
    w.gallery.scrollTo(w.model.index(2197),QListView.PositionAtCenter)
    qtbot.wait(220)
    w.gallery.remember_view_position()
    assert w.gallery._view_anchor and w.gallery._view_anchor['row']>2000


@pytest.mark.parametrize('mode',[0,1,2])
def test_window_zoom_and_screen_changes_keep_visible_photo(qtbot,stack_window,mode):
    w=stack_window;w.layout_combo.setCurrentIndex(mode)
    deep_view(qtbot,w)
    for width,height,edge in ((2100,1150,152),(1280,800,152),(1780,980,256),(1480,940,128)):
        anchor=dict(w.gallery._view_anchor)
        w.resize(width,height);w.thumbnail_size.setValue(edge)
        qtbot.waitUntil(lambda:w.gallery._reflow_anchor is None,timeout=5000)
        rect=w.gallery.visualRect(w.model.index(anchor['row']))
        assert abs(rect.y()-round(anchor['fraction']*rect.height()))<=1,(mode,anchor,rect)
    anchor=dict(w.gallery._view_anchor)
    for kind in (QEvent.ScreenChangeInternal,QEvent.DevicePixelRatioChange):
        QApplication.sendEvent(w.gallery,QEvent(kind))
    qtbot.waitUntil(lambda:w.gallery._reflow_anchor is None,timeout=5000)
    rect=w.gallery.visualRect(w.model.index(anchor['row']))
    assert abs(rect.y()-round(anchor['fraction']*rect.height()))<=1


@pytest.mark.parametrize('theme',['light','dark'])
def test_stack_overlay_stays_in_place_through_reset_and_first_frame(qtbot,stack_window,theme):
    w=stack_window;w.apply_theme(theme,save=False)
    deep_view(qtbot,w)
    row=w.gallery.indexAt(QPoint(12,12)).row()+5
    rect=w.gallery.visualRect(w.model.index(row))
    before=w.gallery.viewport().grab().toImage()
    w.gallery.begin_stack_transition('test',rect)
    motion=w.gallery.stack_motion
    # Reset moves the scrollbar to zero. The opaque old viewport must stay put.
    saved=list(w.model.blocks.items())
    w.model.reset_result([],3000,False,loaded_count=2800,layout_geometry=[(1200,800)]*2800)
    QApplication.processEvents()
    assert motion.geometry()==w.gallery.viewport().geometry()
    assert w.gallery.grab(w.gallery.viewport().geometry()).toImage()==before
    # Paint the animation at its exact first frame: no global palette, font,
    # thumbnail sharpness or pixel change is allowed at the hand-over.
    motion.new=dict(motion.old);motion.progress=0
    motion.update();QApplication.processEvents()
    assert w.gallery.grab(w.gallery.viewport().geometry()).toImage()==before
    w.gallery.stop_stack_transition()


def test_stack_animation_keeps_every_visible_cell_on_large_screen(qtbot,stack_window):
    w=stack_window;w.layout_combo.setCurrentIndex(0)
    w.thumbnail_size.setValue(112);w.resize(2560,1600)
    qtbot.wait(220)
    w.gallery.begin_stack_transition('test',w.gallery.visualRect(w.model.index(5)))
    motion=w.gallery.stack_motion
    assert len(motion.old)>120
    motion.new=dict(motion.old);motion.progress=0
    assert w.gallery.grab(w.gallery.viewport().geometry()).toImage()==motion.snapshot.toImage()
    w.gallery.stop_stack_transition()
