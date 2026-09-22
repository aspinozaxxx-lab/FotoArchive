"""Real Qt/worker acceptance with restored user preferences and untouched originals."""
import json
import multiprocessing as mp
import os
from pathlib import Path
import sqlite3
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from fotoarchive.config import Settings
from fotoarchive.face_review import FaceReviewDialog
from fotoarchive.face_widgets import FaceBox
from fotoarchive.preferences import Preferences
from fotoarchive.ui import MainWindow, STYLE, Viewer


def main():
    cfg = Settings.load()
    report_dir = cfg.data_dir / "reports"
    flow = json.loads((report_dir / "v03_flow.json").read_text(encoding="utf-8"))
    examples = flow["examples"]
    prefs_path = cfg.data_dir / "preferences.json"
    before = prefs_path.read_bytes() if prefs_path.exists() else None
    Preferences(cfg.data_dir).save(face_examples=[], rejected_faces=[])
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window = MainWindow(cfg)
    window.show()
    errors = []
    report = {"passed": False}
    window.backend.event.connect(lambda event: errors.append(event) if event["type"] in {"fatal", "error"} else None)

    def wait_for(predicate, timeout=40):
        deadline = time.monotonic()+timeout
        while not predicate():
            if errors:
                raise RuntimeError(errors)
            if time.monotonic() > deadline:
                raise TimeoutError("UI condition")
            QTest.qWait(30)

    def asset_for(face_id):
        with sqlite3.connect(f"file:{(cfg.data_dir / 'catalog.sqlite3').as_posix()}?mode=ro", uri=True) as database:
            database.row_factory = sqlite3.Row
            return dict(database.execute("SELECT * FROM assets WHERE id=?", (int(face_id.split(':')[0]),)).fetchone())

    def click_face(face_id):
        viewer = Viewer([asset_for(face_id)], 0, cfg, window, window.backend)
        viewer.personSelected.connect(window.find_person)
        viewer.show()
        wait_for(lambda: any(isinstance(item, FaceBox) and item.face_id == face_id for item in viewer.scene.items()))
        viewer.face_boxes_check.setChecked(False)
        assert not window.show_face_boxes and not window.detail_image.show_boxes
        box = next(item for item in viewer.scene.items() if isinstance(item, FaceBox) and item.face_id == face_id)
        assert not box.show_boxes and box.isVisible()
        point = viewer.view.mapFromScene(box.rect().center())
        QTest.mouseClick(viewer.view.viewport(), Qt.LeftButton, pos=point)
        wait_for(lambda: not window.loading and window.mode == "face" and face_id in window.face_examples)
        viewer.close()
        viewer.deleteLater()

    try:
        wait_for(lambda: window.model.rowCount() > 0)
        click_face(examples[0])
        window.face_panel.browse_button.click()
        wait_for(lambda: not window.loading and window.mode == "browse")
        assert list(window.face_examples) == examples[:1]
        click_face(examples[1])
        window.model.fetchMore()
        wait_for(lambda: window.model.rowCount() > 200)
        assert window.model.rowCount() == flow["combined_matches"]
        QTest.qWait(300)
        window.grab().save(str(report_dir / "v03_person_filter.png"))
        initial = list(window.face_examples)
        dialog = FaceReviewDialog(window)
        dialog.show()
        wait_for(lambda: not dialog.loading)
        assert len(dialog.items) == 6
        assert {face["inside_filter"] for face in dialog.items} == {True, False}
        QTest.qWait(200)
        dialog.grab().save(str(report_dir / "v03_refine_dialog.png"))
        choices = [face["id"] for face in dialog.items[:3]]
        for face_id, decision in zip(choices, ("yes", "no", "skip")):
            dialog.decision_buttons[face_id][0][decision].click()
        assert list(window.face_examples) == initial
        dialog.undo_button.click()
        assert choices[2] not in dialog.skipped
        dialog.decision_buttons[choices[2]][0]["skip"].click()
        dialog.request_suggestions()
        wait_for(lambda: not dialog.loading)
        assert not set(choices) & {face["id"] for face in dialog.items}
        dialog.accept()
        window.apply_face_refinement(dialog.examples, dialog.rejected, dialog.skipped)
        wait_for(lambda: not window.loading and window.mode == "face")
        assert choices[0] in window.face_examples and choices[1] in window.face_rejected
        assert Preferences(cfg.data_dir).rejected_faces == {choices[1]}
        dialog.deleteLater()
        window.face_panel.clear_button.click()
        wait_for(lambda: not window.loading and window.mode == "browse")
        assert window.total == flow["reset_catalog_total"] and not window.face_examples and not window.face_rejected
        assert not Preferences(cfg.data_dir).show_face_boxes
        report.update(passed=True, hidden_boxes_clicked=True, browse_to_add_example=True,
                      merged_gallery_rows=flow["combined_matches"], boundary_cards=6,
                      staged_feedback=True, undo=True, apply_and_reset=True, errors=errors)
    except Exception as exc:
        report["error"] = repr(exc)
        raise
    finally:
        for child in window.findChildren(Viewer) + window.findChildren(FaceReviewDialog):
            child.close()
        window.close()
        window.backend.process.join(15)
        report["worker_exitcode"] = window.backend.process.exitcode
        if before is None:
            prefs_path.unlink(missing_ok=True)
        else:
            prefs_path.write_bytes(before)
        (report_dir / "v03_ui.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    mp.freeze_support()
    main()
