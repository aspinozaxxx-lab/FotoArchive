import json
import math
import numpy as np
import pytest
from PIL import Image
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QPixmap
from fotoarchive.catalog import Catalog, Filters
from fotoarchive.config import Settings
from fotoarchive.face_search import InvalidFaceExamples, resolve_examples
from fotoarchive.face_widgets import FaceBox, FacePreview
from fotoarchive.preferences import Preferences
from fotoarchive.search import SearchIndex
from fotoarchive.search_session import SearchSession
from fotoarchive.ui import MainWindow, Viewer
from test_ui import FakeBackend


def test_multi_example_union_best_score_pagination_and_prefilters(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    catalog = Catalog(Settings(data_dir=tmp_path / "data", root=root))
    values = {}
    portrait = Image.new("RGB", (112,112), "gray")
    for i in range(321):
        path = root / f"{i}.jpg"
        portrait.save(path)
        asset_id, _ = catalog.register(path)
        with catalog.db:
            catalog.db.execute("UPDATE assets SET metadata_ready=1,folder=?,captured_at=? WHERE id=?",
                               ("excluded" if i % 7 == 0 else "chosen", "2003-01-01" if i % 2 else "2004-01-01", asset_id))
        angle = i / 320 * math.pi / 2
        vector = np.zeros(128, np.float32)
        vector[:2] = [math.cos(angle), math.sin(angle)]
        values[asset_id] = vector
        face = {"box":[.1,.1,.7,.7],"landmarks":[[.2,.2]]*5,"confidence":.9,"vector":vector,"portrait":portrait}
        catalog.complete_faces({"asset_id":asset_id,"file_version":1}, [face, face] if i % 10 == 0 else [face])
    index = SearchIndex(catalog)
    index.flush_all()
    ids = [catalog.faces_for(i)[0]["id"] for i in (1,321)]
    info, vectors = resolve_examples(catalog, ids + ids)
    assert len(info) == len(vectors) == 2

    def collect(vectors, filters=Filters()):
        context = SearchSession(catalog,index,1,filters,"",vectors,[],mode="face")
        result = []
        while True:
            page = context.page(offset=len(result), expand=True)
            result += page["items"]
            if not page["has_more"] and len(result) >= page["page_total"]:
                return result

    first, second = collect([vectors[0]]), collect([vectors[1]])
    both = collect(vectors)
    found = {a["id"] for a in both}
    assert found == {a["id"] for a in first} | {a["id"] for a in second}
    assert len(both) == len(found) == 321
    assert len(both) > len(first) and len(both) > len(second)
    assert all(both[i]["face_score"] >= both[i+1]["face_score"] for i in range(len(both)-1))
    for row in both:
        assert row["face_score"] == pytest.approx(max(values[row["id"]] @ v for v in vectors), abs=1e-5)
    filtered = collect(vectors, Filters(folder="chosen", date_to="2003-12-31"))
    assert {a["id"] for a in filtered} == {i+1 for i in range(321) if i % 7 and i % 2}
    catalog.db.execute("UPDATE assets SET version=2 WHERE id=1"); catalog.db.commit()
    with pytest.raises(InvalidFaceExamples) as missing:
        resolve_examples(catalog, ids)
    assert missing.value.invalid_face_ids == [ids[0]]
    catalog.close()


def test_hidden_face_boxes_still_click_in_preview_and_viewer(qtbot, tmp_path):
    face = {"id":"face-a","box":[.2,.2,.8,.8]}
    preview = FacePreview()
    qtbot.addWidget(preview)
    preview.resize(200,200)
    pixmap = QPixmap(200,200); pixmap.fill(Qt.white)
    preview.setPhoto(pixmap); preview.setFaces([face]); preview.show()
    shown = preview.grab().toImage().pixelColor(40,100)
    preview.setShowBoxes(False)
    hidden = preview.grab().toImage().pixelColor(40,100)
    assert shown != hidden and hidden == Qt.white
    clicked = []
    preview.personSelected.connect(clicked.append)
    qtbot.mouseClick(preview, Qt.LeftButton, pos=QPoint(100,100))
    assert clicked == ["face-a"]
    source = tmp_path / "image.jpg"
    Image.new("RGB", (200,200), "white").save(source)
    backend = FakeBackend()
    viewer = Viewer([{"id":1,"version":1,"filename":source.name,"path":str(source)}],0,
                    Settings(data_dir=tmp_path / "data"),backend=backend)
    qtbot.addWidget(viewer); viewer.show()
    qtbot.waitUntil(lambda: bool(viewer.scene.items()))
    backend.event.emit({"type":"face_list","asset_id":1,"version":1,"faces":[face]})
    box = next(i for i in viewer.scene.items() if isinstance(i, FaceBox))
    viewer.face_boxes_check.setChecked(False)
    assert not box.show_boxes and box.isVisible() and box.shape().contains(box.rect().center())
    viewer.personSelected.connect(clicked.append)
    qtbot.mouseClick(viewer.view.viewport(), Qt.LeftButton, pos=viewer.view.mapFromScene(box.rect().center()))
    assert clicked == ["face-a","face-a"]


def test_face_filter_add_browse_remove_reset_restore_and_late_replies(qtbot, tmp_path):
    cfg = Settings(data_dir=tmp_path)
    backend = FakeBackend()
    window = MainWindow(cfg, backend)
    qtbot.addWidget(window)
    window.unknown.setChecked(True)
    window.find_person("face-a")
    first_request = window.request_id
    assert backend.sent[-1]["face_ids"] == ["face-a"]
    assert not window.face_panel.isHidden()
    window.find_person("face-b")
    assert backend.sent[-1]["face_ids"] == ["face-a","face-b"]
    window.find_person("face-b")
    assert len(window.face_examples) == 2
    window.face_panel.browse_button.click()
    assert backend.sent[-1]["action"] == "browse" and window.face_picking
    assert backend.sent[-1]["filters"]["unknown_date"] and len(window.face_examples) == 2
    window.find_person("face-c")
    assert backend.sent[-1]["face_ids"] == ["face-a","face-b","face-c"]
    assert not window.face_picking
    window.face_panel.remove_buttons["face-b"].click()
    assert backend.sent[-1]["face_ids"] == ["face-a","face-c"]
    window.set_face_boxes(False)
    assert not window.detail_image.show_boxes
    window.close()
    restored_backend = FakeBackend()
    restored = MainWindow(cfg, restored_backend)
    qtbot.addWidget(restored)
    assert restored_backend.sent[-1]["face_ids"] == ["face-a","face-c"]
    assert not restored.show_face_boxes
    restored.face_panel.clear_button.click()
    assert restored_backend.sent[-1]["action"] == "browse"
    assert not restored.face_examples and restored.face_panel.isHidden()
    restored.on_event({"type":"results","id":first_request-1,"mode":"face","items":[],"total":100,
                       "examples":[{"id":"face-a"}]})
    assert not restored.face_examples and restored.face_panel.isHidden()
    assert Preferences(tmp_path).face_ids == []
    restored.close()


def test_box_toggle_syncs_with_viewer_and_preserves_source_settings(qtbot, tmp_path):
    cfg = Settings(data_dir=tmp_path)
    cfg.save()
    original = (tmp_path / "settings.json").read_bytes()
    backend = FakeBackend()
    window = MainWindow(cfg,backend)
    qtbot.addWidget(window)
    path = tmp_path / "image.jpg"
    Image.new("RGB", (64,64)).save(path)
    viewer = Viewer([{"id":1,"version":1,"filename":path.name,"path":str(path)}],0,cfg,window,backend)
    qtbot.addWidget(viewer)
    viewer.face_boxes_check.setChecked(False)
    assert not window.face_boxes_check.isChecked() and not window.detail_image.show_boxes
    window.face_boxes_check.setChecked(True)
    assert viewer.face_boxes_check.isChecked()
    assert (tmp_path / "settings.json").read_bytes() == original
    viewer.close(); window.close()
