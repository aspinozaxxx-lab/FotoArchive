from PySide6.QtCore import QObject, Signal, Qt
from fotoarchive.config import Settings
from fotoarchive.ui import MainWindow

class FakeBackend(QObject):
    event = Signal(dict)
    def __init__(self):
        super().__init__()
        self.sent = []
    def send(self, **message):
        self.sent.append(message)
    def close(self):
        pass

def test_query_filters_and_stale_results(qtbot, tmp_path):
    backend = FakeBackend()
    window = MainWindow(Settings(data_dir=tmp_path), backend)
    qtbot.addWidget(window)
    window.search_box.setText("три человека без автомобиля")
    window.complex_check.setChecked(True)
    window.unknown.setChecked(True)
    qtbot.keyClick(window.search_box, Qt.Key_Return)
    assert backend.sent[-1]["action"] == "search"
    assert backend.sent[-1]["complex"] is True
    assert backend.sent[-1]["filters"]["unknown_date"] is True
    window.on_event({"type": "results", "id": -1, "items": [], "total": 999})
    assert window.total == 0
    window.date_enabled.setChecked(True)
    assert not window.unknown.isChecked()
    window.close()


def test_infinite_gallery_and_stale_groups(qtbot, tmp_path):
    backend = FakeBackend()
    window = MainWindow(Settings(data_dir=tmp_path), backend)
    qtbot.addWidget(window)
    items = [{"id": i, "filename": f"{i}.jpg", "relative_path": f"2003/{i}.jpg", "version": 1, "width": 64, "height": 48, "size": 1000, "extension": ".jpg"} for i in range(200)]
    window.on_event({"type": "results", "id": 0, "items": items, "total": 200000,
                     "semantic": True, "conditions": ["Нет автомобиля"], "offset": 0, "page_total": 400, "has_more": True})
    window.model.fetchMore()
    assert backend.sent[-1] == {"action": "search_page", "id": 0, "view": 0, "offset": 200, "verdict": ""}
    window.on_event({"type": "search_page", "id": 0, "view": 0, "offset": 200, "verdict": "", "items": items, "page_total": 400, "has_more": True})
    assert window.model.rowCount() == 400
    window.verdict_combo.setCurrentIndex(1)
    assert backend.sent[-1]["verdict"] == "yes"
    assert backend.sent[-1]["offset"] == 0
    confirmed = [dict(items[0], verification={"verdict": "yes"})]
    window.on_event({"type": "search_page", "id": 0, "view": 1, "items": confirmed, "offset": 0, "page_total": 1, "verdict": "yes"})
    assert window.model.rowCount() == 1
    for request_id, view, verdict in ((-1, 1, "yes"), (0, 0, "yes"), (0, 1, "")):
        window.on_event({"type": "search_page", "id": request_id, "view": view, "items": items, "offset": 0, "page_total": 400, "verdict": verdict})
    assert window.model.rowCount() == 1
    assert not hasattr(window, "orientation_combo") and not hasattr(window, "next")
    window.close()


def test_viewer_loads_original_orientation_and_switches_with_arrows(qtbot, tmp_path):
    from PIL import Image
    from fotoarchive.ui import Viewer
    from fotoarchive.media import sha256
    first, second = tmp_path / "rotated.jpg", tmp_path / "second.bmp"
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (80, 40), "red").save(first, exif=exif)
    Image.new("RGB", (96, 64), "blue").save(second)
    before = [sha256(first), sha256(second)]
    items = [{"id": i, "version": 1, "path": str(path), "filename": path.name}
             for i, path in enumerate((first, second), 1)]
    viewer = Viewer(items, 0, Settings(data_dir=tmp_path / "data"))
    qtbot.addWidget(viewer)
    viewer.show()
    qtbot.waitUntil(lambda: bool(viewer.scene.items()), timeout=5000)
    assert viewer.scene.sceneRect().width() == 40
    assert viewer.scene.sceneRect().height() == 80
    viewer.actual()
    qtbot.waitUntil(lambda: viewer.original_loaded, timeout=5000)
    assert viewer.view.transform().m11() == 1.0
    qtbot.keyClick(viewer, Qt.Key_Right)
    qtbot.waitUntil(lambda: viewer.position == 1 and bool(viewer.scene.items()), timeout=5000)
    assert viewer.scene.sceneRect().width() == 96
    assert [sha256(first), sha256(second)] == before
    viewer.close()


def test_indexing_keeps_scroll_selection_and_open_viewer(qtbot, tmp_path):
    from PIL import Image
    from fotoarchive.ui import Viewer

    path = tmp_path / "photo.jpg"
    Image.new("RGB", (80, 60), "blue").save(path)
    backend = FakeBackend()
    window = MainWindow(Settings(data_dir=tmp_path / "data"), backend)
    qtbot.addWidget(window)
    window.show()
    items = [{"id": i, "filename": f"{i}.jpg", "relative_path": f"2003/{i}.jpg",
              "path": str(path), "version": 1, "width": 80, "height": 60,
              "size": 1000, "extension": ".jpg"} for i in range(400)]
    window.on_event({"type": "results", "id": 0, "items": items[:200], "total": 200000,
                     "metadata_count": 200000, "mode": "browse"})
    window.model.accept_page(200, items[200:], 200000, False)
    qtbot.waitUntil(lambda: window.gallery.visualRect(window.model.index(250)).isValid())
    window.gallery.setCurrentIndex(window.model.index(250))
    window.gallery.scrollTo(window.model.index(250))
    qtbot.waitUntil(lambda: window.gallery.verticalScrollBar().value() > 0)
    qtbot.wait(20)
    scroll = window.gallery.verticalScrollBar().value()
    request_id = window.request_id
    viewer = Viewer(window.model, 250, window.cfg, window, backend)
    qtbot.addWidget(viewer)
    viewer.show()
    qtbot.waitUntil(lambda: bool(viewer.scene.items()))
    backend.sent.clear()
    for count in (200001, 200040, 200400):
        window.on_event({"type": "status", "paused": False, "stats": {
            "metadata": count, "total": 201000, "embeddings": 10, "captions": 2,
            "faces": 5, "locations": 1, "errors": 0, "pending": 1000}})
    window.on_event({"type": "index_done", "facets": {}})
    qtbot.wait(20)
    assert window.gallery.verticalScrollBar().value() == scroll
    assert window.selected_asset["id"] == 250
    assert window.gallery.currentIndex().row() == 250
    assert window.request_id == request_id
    assert window.model.rowCount() == 400
    assert viewer.asset["id"] == 250
    viewer.navigate(1)
    qtbot.waitUntil(lambda: viewer.asset["id"] == 251)
    assert not any(c["action"] == "browse" for c in backend.sent)
    assert not window.refresh_results_button.isHidden()
    viewer.close()
    window.folder_combo.addItem("2003", "2003")
    window.folder_combo.setCurrentIndex(1)
    qtbot.mouseClick(window.refresh_results_button, Qt.LeftButton)
    assert backend.sent[-1]["action"] == "browse"
    assert backend.sent[-1]["filters"]["folder"] == "2003"
    assert window.refresh_results_button.isHidden()
    window.close()


def test_empty_catalogue_populates_during_indexing(qtbot, tmp_path):
    backend = FakeBackend()
    window = MainWindow(Settings(data_dir=tmp_path), backend)
    qtbot.addWidget(window)
    window.on_event({"type": "results", "id": 0, "items": [], "total": 0,
                     "metadata_count": 0, "mode": "browse"})
    backend.sent.clear()
    window.on_event({"type": "status", "paused": False, "stats": {
        "metadata": 1, "total": 50, "embeddings": 0, "captions": 0,
        "faces": 0, "locations": 0, "errors": 0, "pending": 49}})
    assert backend.sent[-1]["action"] == "browse"
    window.close()


def test_viewer_maximizes_refits_and_keeps_manual_zoom(qtbot, tmp_path):
    from PIL import Image
    from fotoarchive.ui import Viewer

    path = tmp_path / "photo.jpg"
    Image.new("RGB", (1600, 1200), "blue").save(path)
    viewer = Viewer([{"id": 1, "version": 1, "path": str(path), "filename": path.name}],
                    0, Settings(data_dir=tmp_path / "data"))
    qtbot.addWidget(viewer)
    assert viewer.windowFlags() & Qt.WindowMaximizeButtonHint
    viewer.resize(1100, 600)
    viewer.show()
    qtbot.waitUntil(lambda: bool(viewer.scene.items()))
    old_scale = viewer.view.transform().m11()
    viewer.resize(1450, 950)
    qtbot.waitUntil(lambda: viewer.view.transform().m11() > old_scale)
    viewer.showMaximized()
    qtbot.waitUntil(viewer.isMaximized)
    assert not viewer.isFullScreen()
    viewer.showNormal()
    qtbot.waitUntil(lambda: not viewer.isMaximized())
    viewer.actual()
    qtbot.waitUntil(lambda: viewer.original_loaded)
    viewer.resize(1200, 800)
    qtbot.wait(20)
    assert viewer.view.transform().m11() == 1.0
    viewer.fit()
    qtbot.waitUntil(lambda: viewer.view.transform().m11() != 1.0)
    viewer.close()
