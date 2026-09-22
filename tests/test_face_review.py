import math
from collections import OrderedDict

import numpy as np
from PIL import Image
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from fotoarchive.catalog import Catalog, Filters
from fotoarchive.config import Settings
from fotoarchive.face_review import FaceReviewDialog
from fotoarchive.face_search import boundary_suggestions
from fotoarchive.preferences import Preferences
from fotoarchive.search import SearchIndex
from fotoarchive.search_session import SearchSession
from fotoarchive.ui import MainWindow
from test_ui import FakeBackend


def exhaust(generator):
    batches = 0
    while True:
        try:
            next(generator)
            batches += 1
        except StopIteration as result:
            return result.value, batches


def test_boundary_both_sides_max_score_diversity_prefilters_and_rejection(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    catalog = Catalog(Settings(data_dir=tmp_path / "data", root=root))
    portrait = Image.new("RGB", (112, 112), "gray")
    ids = []
    originals = {}
    for i in range(42):
        path = root / f"{i}.jpg"
        portrait.save(path)
        asset_id, _ = catalog.register(path)
        score = .364 + i * .0001 if i % 2 else .362 - i * .0001
        vector = np.zeros(128, np.float32)
        vector[0] = score
        # These would appear close to the boundary for reference 1 but are
        # confident matches to reference 2: they must not be suggested.
        vector[1] = .8 if i < 4 else 0
        vector[i+2] = math.sqrt(1 - score**2 - vector[1]**2)
        originals[asset_id] = vector
        with catalog.db:
            catalog.db.execute("UPDATE assets SET metadata_ready=1,folder=?,captured_at=? WHERE id=?",
                               ("other" if i in (4,5) else "chosen", "2002-01-01" if i == 6 else "2003-05-01", asset_id))
        face = {"box":[.1,.1,.7,.7],"landmarks":[[.2,.2]]*5,"confidence":.9,"vector":vector,"portrait":portrait}
        catalog.complete_faces({"asset_id":asset_id,"file_version":1}, [face, face] if i == 8 else [face])
        ids.append(catalog.faces_for(asset_id)[0]["id"])
    index = SearchIndex(catalog)
    index.flush_all()
    with catalog.db:
        catalog.db.execute("UPDATE assets SET version=2 WHERE id=8")
    references = np.eye(128, dtype=np.float32)[:2]
    filters = Filters(folder="chosen", date_from="2003-01-01")
    items, batches = exhaust(boundary_suggestions(index, references, filters, excluded=[ids[9]]))
    assert batches >= 1 and len(items) == 6
    assert len({face["asset_id"] for face in items}) == 6
    assert {face["inside_filter"] for face in items} == {True, False}
    assert sum(face["inside_filter"] for face in items) == 3
    assert not {face["asset_id"] for face in items} & set(range(1,9))
    assert ids[9] not in {face["id"] for face in items}
    for face in items:
        assert abs(face["face_score"] - max(originals[face["asset_id"]] @ ref for ref in references)) < 1e-6
    following, _ = exhaust(boundary_suggestions(index, references, filters, excluded=[ids[9]]+[face["id"] for face in items]))
    assert not {face["id"] for face in items} & {face["id"] for face in following}

    faces_in_photo = [face["id"] for face in catalog.faces_for(9)]
    assert len(faces_in_photo) == 2
    # Use that face as the query: rejecting one face must not hide the photograph
    # when another face in it still matches the same person.
    def matched(excluded):
        context = SearchSession(catalog, index, 1, Filters(), "", [originals[9]], [], mode="face", excluded_faces=excluded)
        return {a["id"] for a in context.page(expand=True)["items"]}
    assert 9 in matched(faces_in_photo[:1])
    assert 9 not in matched(faces_in_photo + ["untrusted' OR 1=1 --"])
    catalog.close()


def example(key, tmp_path, inside=True):
    path = tmp_path / f"{key}.png"
    Image.new("RGB", (112, 112), "gray").save(path)
    return {"id":key,"asset_id":1,"file_version":1,"thumbnail":str(path),"filename":path.name,
            "relative_path":path.name,"inside_filter":inside,"face_score":.37 if inside else .35,
            "box":[.1,.1,.7,.7],"asset":{"id":1,"version":1,"filename":path.name,"path":str(path)}}


def test_refinement_transaction_apply_cancel_undo_and_persistence(qtbot, tmp_path):
    backend = FakeBackend()
    window = MainWindow(Settings(data_dir=tmp_path), backend)
    qtbot.addWidget(window)
    window.face_cache["a"] = example("a", tmp_path)
    window.find_person("a")
    candidates = [example(key, tmp_path, key != "b") for key in ("b","c","d")]
    before = (tmp_path / "preferences.json").read_bytes()

    def edit_dialog():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, FaceReviewDialog)
        backend.event.emit({"type":"face_suggestions","id":dialog.request_id,"items":candidates})
        dialog.decision_buttons["b"][0]["yes"].click()
        dialog.decision_buttons["c"][0]["no"].click()
        dialog.decision_buttons["d"][0]["skip"].click()
        assert list(window.face_examples) == ["a"]
        assert (tmp_path / "preferences.json").read_bytes() == before
        assert list(dialog.examples) == ["a","b"] and dialog.rejected == {"c"}
        dialog.undo_button.click()
        assert "d" not in dialog.skipped and dialog.rejected == {"c"}
        dialog.decision_buttons["d"][0]["skip"].click()
        dialog.apply_button.click()

    QTimer.singleShot(0, edit_dialog)
    window.refine_person()
    assert list(window.face_examples) == ["a","b"]
    assert window.face_rejected == {"c"} and window.face_skipped == {"d"}
    assert backend.sent[-1]["face_ids"] == ["a","b"] and backend.sent[-1]["excluded_faces"] == ["c"]
    assert Preferences(tmp_path).rejected_faces == {"c"}
    saved = (tmp_path / "preferences.json").read_bytes()

    def cancel_dialog():
        dialog = QApplication.activeModalWidget()
        pending = dialog.request_id
        assert "c" in backend.sent[-1]["excluded"] and "d" in backend.sent[-1]["excluded"]
        dialog.remove_example("a")
        backend.event.emit({"type":"face_suggestions","id":pending,"items":candidates})
        assert not dialog.items  # stale reply after the examples changed
        dialog.restore_button.click()
        assert not dialog.rejected
        dialog.reject()

    QTimer.singleShot(0, cancel_dialog)
    window.refine_person()
    assert list(window.face_examples) == ["a","b"] and window.face_rejected == {"c"}
    assert (tmp_path / "preferences.json").read_bytes() == saved
    window.clear_person()
    assert not Preferences(tmp_path).rejected_faces and not window.face_skipped
    assert backend.sent[-1]["action"] == "browse"
    window.close()


def test_refinement_remove_last_example_and_stale_response(qtbot, tmp_path):
    backend = FakeBackend()
    window = MainWindow(Settings(data_dir=tmp_path), backend)
    qtbot.addWidget(window)
    window.find_person("a")
    dialog = FaceReviewDialog(window)
    qtbot.addWidget(dialog)
    request = dialog.request_id
    dialog.remove_example("a")
    assert not dialog.next_button.isEnabled()
    assert dialog.apply_button.text() == "Сбросить фильтр"
    backend.event.emit({"type":"face_suggestions","id":request,"items":[example("b", tmp_path)]})
    assert not dialog.items
    dialog.undo()
    assert list(dialog.examples) == ["a"] and dialog.next_button.isEnabled()
    dialog.request_suggestions()
    assert dialog.request_id != request
    dialog.reject()
    backend.event.emit({"type":"face_suggestions","id":dialog.request_id,"items":[]})
    assert list(window.face_examples) == ["a"]
    window.apply_face_refinement(OrderedDict(), {"x"}, {"y"})
    assert not window.face_examples and not window.face_rejected and not window.face_skipped
    window.close()
