from PySide6.QtCore import QPoint, QPointF, QSize, Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel, QWheelEvent
from PySide6.QtWidgets import QApplication, QListView

from fotoarchive.smooth_scroll import PhotoGallery


def gallery(qtbot):
    view = PhotoGallery()
    qtbot.addWidget(view)
    view.setViewMode(QListView.IconMode)
    view.setMovement(QListView.Static)
    view.setGridSize(QSize(140, 120))
    view.setUniformItemSizes(True)
    model = QStandardItemModel(view)
    for index in range(500):
        model.appendRow(QStandardItem(str(index)))
    view.setModel(model)
    view.resize(640, 480)
    view.show()
    qtbot.waitUntil(lambda: view.verticalScrollBar().maximum() > 1000)
    view.setCurrentIndex(model.index(0, 0))
    return view


def wheel(view, angle=-120, pixels=0):
    point = view.viewport().rect().center()
    event = QWheelEvent(QPointF(point), QPointF(view.viewport().mapToGlobal(point)),
                        QPoint(0, pixels), QPoint(0, angle), Qt.NoButton,
                        Qt.NoModifier, Qt.NoScrollPhase, False)
    QApplication.sendEvent(view.viewport(), event)


def settle(qtbot, view):
    qtbot.waitUntil(lambda: not view._animation.isActive(), timeout=1500)
    return view.verticalScrollBar().value()


def test_single_notch_moves_through_intermediate_pixels_and_rapid_wheel_accelerates(qtbot):
    view = gallery(qtbot)
    bar = view.verticalScrollBar()
    wheel(view)
    assert bar.value() == 0
    qtbot.waitUntil(lambda: bar.value() > 0)
    intermediate = bar.value()
    one_notch = settle(qtbot, view)
    assert 0 < intermediate < one_notch < 120  # less than one photo row
    assert view.currentIndex().row() == 0

    bar.setValue(0)
    for _ in range(3):
        wheel(view)
        qtbot.wait(25)
    rapid = settle(qtbot, view)
    assert rapid > 3 * one_notch * 1.15
    assert view.currentIndex().row() == 0


def test_reversal_and_manual_scroll_cancel_queued_motion(qtbot):
    view = gallery(qtbot)
    bar = view.verticalScrollBar()
    bar.setValue(1000)
    for _ in range(3):
        wheel(view)
        qtbot.wait(25)
    before_reverse = bar.value()
    wheel(view, angle=120)
    qtbot.waitUntil(lambda: bar.value() < before_reverse)
    assert settle(qtbot, view) < before_reverse

    wheel(view)
    qtbot.waitUntil(view._animation.isActive)
    bar.setValue(700)
    qtbot.wait(150)
    assert bar.value() == 700
    assert not view._animation.isActive()


def test_touchpad_boundaries_and_result_reset(qtbot):
    view = gallery(qtbot)
    bar = view.verticalScrollBar()
    wheel(view, angle=0, pixels=-23)
    assert bar.value() == 23
    assert not view._animation.isActive()

    bar.setValue(bar.maximum() - 5)
    wheel(view)
    assert settle(qtbot, view) == bar.maximum()
    wheel(view)
    assert not view._animation.isActive()
    wheel(view, angle=120)
    assert settle(qtbot, view) < bar.maximum()

    wheel(view)
    view.model().clear()
    qtbot.wait(150)
    assert bar.value() == 0
    assert not view._animation.isActive()
