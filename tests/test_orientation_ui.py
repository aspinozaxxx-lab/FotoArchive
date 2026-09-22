from PySide6.QtCore import Qt, QPoint
from PySide6.QtWidgets import QPushButton
from PIL import Image

from fotoarchive.config import Settings
from fotoarchive.orientation_ui import OrientationDialog
from fotoarchive.ui import MainWindow
from test_ui import FakeBackend


def test_orientation_review_exclusion_paging_and_explicit_apply(qtbot, tmp_path):
    backend = FakeBackend()
    window = MainWindow(Settings(data_dir=tmp_path), backend)
    qtbot.addWidget(window)
    dialog = OrientationDialog(window)
    qtbot.addWidget(dialog)
    dialog.show()
    thumbnail = tmp_path / 'thumb.webp'
    Image.new('RGB', (80, 50), 'green').save(thumbnail)
    asset = dict(id=7, version=2, relative_path='2003/photo.jpg', thumbnail=str(thumbnail),
                 proposed_rotation=90, rotation_reason='Люди стоят боком', certain=1, selected=1, excluded=0)
    stats = dict(running=True, pending=80, total=100, checked=20, recommendations=15,
                 selected=15, excluded=1, errors=0, edit_errors=0, edits_pending=0,
                 applied=3, last_batch='previous', average_seconds=5)
    backend.event.emit(dict(type='orientation_progress', stats=stats))
    window.clear_details()
    assert window.orientation_stats == stats
    backend.event.emit(dict(type='orientation_page', id=dialog.request_id, items=[asset], total=45))
    assert dialog.checkboxes[7].isChecked() and dialog.apply.isEnabled()
    qtbot.wait(30)
    qtbot.mouseClick(dialog.checkboxes[7], Qt.LeftButton, pos=QPoint(10, dialog.checkboxes[7].height()//2))
    assert backend.sent[-1]['action'] == 'orientation_decide' and backend.sent[-1]['selected'] is False
    assert dialog.apply.isVisible() and dialog.rect().contains(dialog.apply.geometry())
    qtbot.mouseClick(dialog.apply, Qt.LeftButton)
    assert backend.sent[-1]['action'] == 'orientation_apply'
    assert sum(item['action'] == 'orientation_apply' for item in backend.sent) == 1
    exclude = next(button for button in dialog.findChildren(QPushButton) if button.text() == 'Исключить эту фотографию')
    exclude.click()
    assert backend.sent[-2]['excluded'] is True
    old_request = dialog.request_id
    dialog.next.click()
    assert backend.sent[-1]['offset'] == 40
    backend.event.emit(dict(type='orientation_page', id=old_request, items=[], total=0))
    assert dialog.items == [asset]  # A delayed older page cannot replace the current request.
    dialog.pause_button.click()
    assert backend.sent[-1]['action'] == 'orientation_pause'
    assert dialog.undo.isVisible()
    qtbot.mouseClick(dialog.undo, Qt.LeftButton)
    assert backend.sent[-1]['batch_id'] == 'previous'
    dialog.close()
    backend.event.emit(dict(type='orientation_notice', message='late'))
    assert dialog.cleaned_up
    window.close()


def test_uncertain_rotation_is_never_selected_implicitly(qtbot, tmp_path):
    backend = FakeBackend()
    window = MainWindow(Settings(data_dir=tmp_path), backend)
    qtbot.addWidget(window)
    dialog = OrientationDialog(window)
    qtbot.addWidget(dialog)
    dialog.group.setCurrentIndex(dialog.group.findData('uncertain'))
    asset = dict(id=1, version=1, relative_path='unknown.jpg', proposed_rotation=None,
                 rotation_reason='Проверьте вручную', certain=0, selected=0, excluded=0)
    backend.event.emit(dict(type='orientation_page', id=dialog.request_id, items=[asset], total=1))
    assert not dialog.checkboxes[1].isChecked() and not dialog.checkboxes[1].isEnabled()
    assert not any(item['action'] == 'orientation_apply' for item in backend.sent)
    dialog.close()
    window.close()
