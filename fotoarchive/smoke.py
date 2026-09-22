"""Packaged-app acceptance check through the real UI and spawned worker."""
import json
import sys
import time
from PySide6.QtCore import QTimer


def install_smoke_check(app, window, cfg):
    report = {"executable": sys.executable, "frozen": bool(getattr(sys, "frozen", False)), "started": time.time(), "passed": False}
    report["application_icon"] = not app.windowIcon().pixmap(32, 32).isNull()
    report['check_updates_button'] = window.check_updates_button.text() == 'Проверить обновления'
    stage = {"value": "browse"}
    output = cfg.data_dir / "reports/packaged_smoke.json"
    preferences_path = cfg.data_dir / "preferences.json"
    previous_preferences = preferences_path.read_bytes() if preferences_path.exists() else None
    timeout = QTimer(window)
    timeout.setSingleShot(True)

    def finish(error=None):
        if stage["value"] == "finished":
            return
        stage["value"] = "finished"
        timeout.stop()
        if stage.get("dialog"):
            stage["dialog"].close()
        report["passed"] = error is None
        report["error"] = error
        report["elapsed_seconds"] = time.time() - report["started"]
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        window.grab().save(str(cfg.data_dir / "reports/packaged_application.png"))
        if previous_preferences is None:
            preferences_path.unlink(missing_ok=True)
        else:
            preferences_path.write_bytes(previous_preferences)
        window.close()
        app.exit(0 if error is None else 1)

    def review_ready():
        try:
            dialog = stage["dialog"]
            if not dialog.items:
                raise AssertionError("No boundary suggestions in the indexed sample")
            report["face_refinement_cards"] = len(dialog.items)
            dialog.grab().save(str(cfg.data_dir / "reports/packaged_face_refinement.png"))
            dialog.decide(dialog.items[0]["id"], "yes")
            if len(dialog.items) > 1:
                dialog.decide(dialog.items[1]["id"], "no")
            stage["value"] = "review_applied"
            dialog.accept()
            window.apply_face_refinement(dialog.examples, dialog.rejected, dialog.skipped)
        except Exception as exc:
            finish(str(exc))

    def event(message):
        if message['type']=='status' and 'pipeline' in message:
            report['pipeline']=message['pipeline']
            report['catalog_progress']=message['stats']
            report['source_inventory']=message.get('inventory')
        if message["type"] == "results" and message.get("id") != window.request_id:
            return
        if message["type"] in {"fatal", "error"}:
            finish(message["message"])
        elif message["type"] == "results":
            if stage["value"] == "browse":
                if not report["application_icon"]:
                    finish("Application icon was not bundled or could not be loaded")
                    return
                report["catalog_total"] = message["total"]
                if not message["items"]:
                    finish("Smoke test needs an indexed sample")
                    return
                stage["asset_id"] = message["items"][0]["id"]
                stage["value"] = "search"
                stage["search_started"] = time.perf_counter()
                window.search_box.setText("салют в ночном небе")
                window.search()
            elif stage["value"] == "search":
                report["search_seconds"] = time.perf_counter() - stage["search_started"]
                report["search_top5"] = [a["id"] for a in message["items"][:5]]
                if not message["items"]:
                    finish("Search returned no candidates")
                    return
                window.gallery.setCurrentIndex(window.model.index(0))
                stage["value"] = "diagnostics"
                window.backend.send(action="diagnostics", asset_id=stage["asset_id"])
            elif stage["value"] == "first_face":
                next_face = next((asset.get("face_match_id") for asset in message["items"]
                                  if asset.get("face_match_id") not in window.face_examples), None)
                if not next_face:
                    finish("Need a second face example in the indexed sample")
                    return
                stage["value"] = "multiple_faces"
                window.find_person(next_face)
            elif stage["value"] == "multiple_faces":
                from .face_review import FaceReviewDialog
                report["face_examples"] = len(message["examples"])
                window.face_boxes_check.setChecked(False)
                report["boxes_hidden"] = not window.detail_image.show_boxes
                if report["face_examples"] != 2:
                    finish("Multiple face filter did not retain both examples")
                    return
                stage["value"] = "refining"
                stage["dialog"] = FaceReviewDialog(window)
                stage["dialog"].show()
            elif stage["value"] == "review_applied":
                report["refinement_applied"] = len(message["examples"]) == 3 and bool(window.face_rejected)
                if not report["refinement_applied"]:
                    finish("Face refinement was not applied")
                    return
                stage["value"] = "reset"
                window.face_panel.clear_button.click()
            elif stage["value"] == "reset":
                report["face_reset"] = message["mode"] == "browse" and not window.face_examples and not window.face_rejected
                if not report["face_reset"]:
                    finish("Face filter did not reset")
                else:
                    from .orientation_ui import OrientationDialog
                    stage['dialog'].close()
                    stage['value'] = 'orientation'
                    stage['dialog'] = OrientationDialog(window)
                    stage['dialog'].show()
        elif message['type'] == 'orientation_page' and stage['value'] == 'orientation':
            if message.get('id') == stage['dialog'].request_id:
                QTimer.singleShot(100, orientation_ready)
        elif message['type'] == 'folders_added' and stage['value'] == 'folders':
            report['existing_folders_skipped'] = len(message['skipped'])
            if message['added'] or message['errors'] or len(message['skipped']) != 2:
                finish('Existing folders were not skipped')
            else:
                stage['value'] = 'finishing'
                QTimer.singleShot(200, source_ready)
        elif message["type"] == "face_suggestions" and stage["value"] == "refining":
            if message.get("id") == stage["dialog"].request_id:
                QTimer.singleShot(0, review_ready)
        elif message["type"] == "diagnostics":
            report["models"] = message
            if (message["dimensions"] != 768 or message["providers"][0] != "DmlExecutionProvider"
                    or message["face_dimensions"] != 128
                    or any(p[0] != "DmlExecutionProvider" for p in message["face_providers"].values())):
                finish("GPU inference contract failed")
            else:
                stage["value"] = "first_face"
                window.find_person(message["sample_face_id"])

    def orientation_ready():
        dialog = stage['dialog']
        report['orientation_review'] = dialog.apply.isVisible() and dialog.rect().contains(dialog.apply.geometry())
        if not report['orientation_review']:
            finish('Rotation review controls are not visible')
            return
        dialog.grab().save(str(cfg.data_dir / 'reports/packaged_orientation_review.png'))
        dialog.close()
        stage['value'] = 'folders'
        folder = str(cfg.root / cfg.includes[0])
        window.backend.send(action='add_folders', paths=[folder, folder])

    def source_ready():
        try:
            from .inventory import format_totals
            from .source_details import SourceDetailsDialog
            inventory = window.source_inventory
            if inventory.get('phase') == 'counting':
                QTimer.singleShot(200, source_ready)
                return
            if inventory.get('phase') != 'ready' or inventory.get('format_counts') is None:
                raise AssertionError('Source format inventory was not available')
            totals = format_totals(inventory['format_counts'])
            assert totals['supported'] == inventory['total']
            report['source_format_totals'] = dict(totals)
            report['source_label'] = window.source_label.text()
            stage['dialog'] = SourceDetailsDialog(window)
            stage['dialog'].show()
            QTimer.singleShot(200, source_captured)
        except Exception as exc:
            finish(str(exc))

    def source_captured():
        stage['dialog'].grab().save(str(cfg.data_dir / 'reports/packaged_source_details.png'))
        report['source_details_visible'] = stage['dialog'].details.isVisible()
        finish()

    window.backend.event.connect(event)
    timeout.timeout.connect(lambda: finish("Packaged smoke check timed out"))
    timeout.start(120000)
    QTimer.singleShot(0, window.clear_query)
