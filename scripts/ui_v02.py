"""Exercise the native Qt surface with its real worker and capture the rendered result."""
import json
import multiprocessing as mp
import os
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from fotoarchive.config import Settings
    from fotoarchive.ui import MainWindow, Viewer, STYLE
    cfg = Settings.load()
    output = cfg.data_dir / "reports"
    app = QApplication([])
    app.setStyle("Fusion"); app.setStyleSheet(STYLE)
    window = MainWindow(cfg)
    window.show()
    state = {"phase":"browse", "passed":False}
    database = sqlite3.connect(f"file:{(cfg.data_dir / 'catalog.sqlite3').as_posix()}?mode=ro", uri=True)
    person_assets = {row[0] for row in database.execute("SELECT DISTINCT asset_id FROM faces")}
    database.close()
    viewer = None

    def finish(error=None):
        nonlocal viewer
        if state["phase"] == "done":
            return
        state["phase"] = "done"
        state["passed"] = error is None
        state["error"] = error
        timer.stop()
        if viewer:
            viewer.close()
        window.close()
        (output / "v02_ui.json").write_text(json.dumps(state, ensure_ascii=False, indent=2),encoding="utf-8")
        app.exit(1 if error else 0)

    def event(message):
        if message["type"] in {"error", "fatal"}:
            finish(message["message"])

    def step():
        nonlocal viewer
        try:
            phase = state["phase"]
            if phase == "browse" and window.total and not window.loading:
                if window.model.rowCount() < window.total:
                    window.gallery.scrollToBottom()
                    window.scroll_more()
                else:
                    state["scroll_count"] = window.model.rowCount()
                    state["resident_records"] = sum(map(len, window.model.blocks.values()))
                    assert state["scroll_count"] > 200
                    assert state["resident_records"] <= 1600
                    state["phase"] = "return_top"
                    window.gallery.scrollToTop()
                    window.model.asset(0)
            elif phase == "return_top":
                if not window.model.asset(0):
                    return
                state["reloaded_first"] = window.model.asset(0)["id"]
                for row in range(200):
                    asset = window.model.asset(row)
                    if asset and asset["id"] in person_assets:
                        state["row"] = row
                        window.gallery.setCurrentIndex(window.model.index(row))
                        window.gallery.scrollTo(window.model.index(row))
                        state["phase"] = "faces"
                        break
            elif phase == "faces" and window.detail_image.faces:
                window.grab().save(str(output / "v02_application.png"))
                viewer = Viewer(window.model, state["row"], cfg, window, window.backend)
                viewer.personSelected.connect(window.find_person)
                viewer.show()
                state["phase"] = "viewer"
            elif phase == "viewer" and viewer.faces and viewer.scene.items():
                from fotoarchive.face_widgets import FaceBox
                boxes = [i for i in viewer.scene.items() if isinstance(i, FaceBox)]
                if not boxes:
                    return
                viewer.grab().save(str(output / "v02_viewer.png"))
                point = viewer.view.mapFromScene(boxes[0].rect().center())
                state["clicked_face"] = boxes[0].face_id
                state["phase"] = "face_search"
                QTest.mouseClick(viewer.view.viewport(), Qt.LeftButton, pos=point)
            elif phase == "face_search" and window.mode == "face" and not window.loading:
                assert window.model.rowCount() > 0
                state["face_search_results"] = window.model.known_total
                window.grab().save(str(output / "v02_person_search.png"))
                # A real interval change submits the filter while retaining the chosen person.
                slider = window.date_slider
                width = slider.maximum - slider.minimum
                slider.setRange(slider.minimum + width//4, slider.maximum - width//4)
                window.commit_dates()
                assert window.reference[0] == "face_search"
                state["phase"] = "date_filter"
            elif phase == "date_filter" and not window.loading:
                state["date_filtered_results"] = window.model.known_total
                state["date_filter"] = window.filters()
                window.grab().save(str(output / "v02_date_filter.png"))
                finish()
        except Exception as exc:
            finish(f"{type(exc).__name__}: {exc}")

    window.backend.event.connect(event)
    timer = QTimer(window)
    timer.timeout.connect(step)
    timer.start(180)
    QTimer.singleShot(60000, lambda: finish("UI scenario timed out"))
    code = app.exec()
    window.backend.process.join(15)
    print(json.dumps(state, ensure_ascii=True,indent=2))
    return code


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
