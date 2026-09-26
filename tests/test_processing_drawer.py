import pytest
from PySide6.QtCore import QPoint

from fotoarchive.config import Settings
from fotoarchive.ui import MainWindow
from test_ui import FakeBackend


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_drawer_fits_report_and_adapts_to_live_content_and_window_size(qtbot, tmp_path, theme):
    window = MainWindow(Settings(data_dir=tmp_path), FakeBackend())
    qtbot.addWidget(window)
    window.apply_theme(theme, save=False)
    window.resize(1480, 1200)
    window.show()
    window.pipeline_label.setText('Подготовка CPU: до 6 процессов · GPU: лица + описания')
    window.source_label.setText('Источник: 126 546 фото и видео во всех добавленных папках')
    window.remote_panel.update_status(dict(state='working', cache_bytes=18*1024**3,
        cache_limit=20*1024**3, cache_pending_bytes=2*1024**3, queued=1200))
    qtbot.wait(30)
    size = window.size()
    window.processing_toggle.setChecked(True)
    drawer = window.processing_panel
    qtbot.waitUntil(lambda: drawer.verticalScrollBar().maximum() == 0)
    qtbot.wait(30)
    assert window.size() == size
    assert drawer.viewport().height() >= drawer.widget().height()
    base = drawer.height()

    # New warnings and wrapped text must expand the drawer while space permits.
    window.updates_label.setText('Проверка завершена · новых: 100 · изменённых: 20')
    window.updates_label.show()
    window.remote_panel.warning.setText('\n'.join(['Повторная обработка файлов с ошибками.'] * 4))
    window.remote_panel.warning.show()
    qtbot.waitUntil(lambda: drawer.height() > base)
    qtbot.wait(30)
    assert drawer.verticalScrollBar().maximum() == 0
    expanded = drawer.height()

    window.resize(1060, 680)
    qtbot.waitUntil(lambda: drawer.verticalScrollBar().maximum() > 0)
    assert drawer.height() < expanded
    drawer.ensureWidgetVisible(window.pause_button)
    qtbot.wait(30)
    assert drawer.viewport().rect().contains(window.pause_button.mapTo(drawer.viewport(), window.pause_button.rect().center()))
    assert drawer.mapTo(window.centralWidget(), QPoint(0, drawer.height())).y() <= window.compact_status.y()

    window.resize(1480, 1200)
    qtbot.waitUntil(lambda: drawer.verticalScrollBar().maximum() == 0)
    window.remote_panel.warning.hide()
    window.updates_label.hide()
    qtbot.waitUntil(lambda: drawer.height() == base)
    for _ in range(5):
        window.processing_toggle.setChecked(False)
        window.processing_toggle.setChecked(True)
        qtbot.wait(10)
        assert drawer.verticalScrollBar().maximum() == 0
    window.close()
