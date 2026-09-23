"""A share menu must never hold a nested event loop or outlive its viewer."""
from time import perf_counter

import pytest
from PIL import Image
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QDialog, QVBoxLayout

from fotoarchive.config import Settings
from fotoarchive.telegram_share import Contacts, ShareButton


@pytest.mark.parametrize('trigger', ['hover', 'click'])
def test_menu_returns_immediately_and_never_starts_sharing(qtbot, tmp_path, monkeypatch, trigger):
    cfg = Settings(data_dir=tmp_path)
    Contacts(tmp_path).add('test_contact')
    viewer = QDialog()
    qtbot.addWidget(viewer)
    def no_asset_request():
        pytest.fail('No contact was selected')
    button = ShareButton(cfg, no_asset_request, viewer)
    QVBoxLayout(viewer).addWidget(button)
    viewer.show()
    monkeypatch.setattr(button, 'underMouse', lambda: True)
    # This guard unwinds the old blocking showMenu path so failure is bounded.
    guard = QTimer(viewer)
    guard.setSingleShot(True)
    guard.timeout.connect(button.menu.close)
    guard.start(400)
    start = perf_counter()
    if trigger == 'hover':
        button.hover.timeout.emit()
    else:
        qtbot.mouseClick(button, Qt.LeftButton)
    elapsed = perf_counter() - start
    guard.stop()
    assert elapsed < .2
    assert button.menu.isVisible()
    viewer.close()
    assert not button.menu.isVisible()
    assert button.stop.is_set()
    assert not button.hover.isActive()
    # A stale timeout or queued contact click cannot reopen a closed viewer.
    button.hover.timeout.emit()
    button.choose('test_contact')
    assert not button.menu.isVisible()


def test_late_share_completion_after_close_is_ignored(qtbot, tmp_path, monkeypatch):
    from fotoarchive.telegram_share import QMessageBox
    viewer = QDialog()
    qtbot.addWidget(viewer)
    button = ShareButton(Settings(data_dir=tmp_path), lambda: None, viewer)
    QVBoxLayout(viewer).addWidget(button)
    viewer.show()
    button.setEnabled(False)
    monkeypatch.setattr(QMessageBox, 'information', lambda *_: pytest.fail('Closed viewer displayed an error'))
    viewer.close()
    button.finished('Cancelled during closing')
    assert not button.isEnabled()


def test_photo_closes_menu_and_pending_hover_before_preview_cleanup(qtbot, tmp_path, monkeypatch):
    from fotoarchive.ui import Viewer
    path = tmp_path / 'photo.jpg'
    Image.new('RGB', (32, 24), 'green').save(path)
    viewer = Viewer([dict(id=1, version=1, path=str(path), filename=path.name)], 0,
                    Settings(data_dir=tmp_path / 'data'))
    qtbot.addWidget(viewer)
    viewer.show()
    button = viewer.share_button
    qtbot.mouseClick(button, Qt.LeftButton)
    button.hover.start()
    original_close = viewer.preview_buffer.close
    def checked_cleanup():
        assert button.stop.is_set() and not button.menu.isVisible() and not button.hover.isActive()
        original_close()
    monkeypatch.setattr(viewer.preview_buffer, 'close', checked_cleanup)
    viewer.close()
    assert viewer.finished_cleanup
