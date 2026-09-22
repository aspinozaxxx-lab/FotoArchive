"""Real Qt + spawned GPU worker: analyze, exclude, rotate copies, reindex, undo.

Original archive is opened read-only. All mutations use an isolated copy folder.
Large immutable model/runtime files use same-volume hard links, not duplicates.
"""
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QT_QPA_FONTDIR', 'C:/Windows/Fonts')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton
from fotoarchive.config import Settings, EMBED_VERSION, CAPTION_VERSION, FACE_VERSION
from fotoarchive.media import open_rgb, sha256
from fotoarchive.orientation import rotate_clockwise
from fotoarchive.orientation_ui import OrientationDialog
from fotoarchive.ui import MainWindow, STYLE


def readonly(data):
    db = sqlite3.connect(f'file:{(data / "catalog.sqlite3").as_posix()}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    return db


def main():
    production = Settings.load()
    with readonly(production.data_dir) as db:
        sources = {i: Path(db.execute('SELECT path FROM assets WHERE id=?', (i,)).fetchone()[0]) for i in (4, 171)}
    original_hashes = {str(path): sha256(path) for path in sources.values()}
    isolated = Path(tempfile.mkdtemp(prefix='v04-copies-', dir=production.data_dir/'reports'))
    root, data = isolated/'source', isolated/'data'
    root.mkdir(); data.mkdir()
    for name in ('models', 'runtime', 'geonames'):
        for source in (production.data_dir/name).rglob('*'):
            if source.is_file():
                target = data/name/source.relative_to(production.data_dir/name)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.link(source, target)
    shutil.copyfile(sources[4], root/'sideways.jpg')
    shutil.copyfile(sources[171], root/'upright.jpg')
    rotate_clockwise(open_rgb(sources[171]), 90).save(root/'sideways.bmp')
    copy_hashes = {path.name: sha256(path) for path in root.iterdir()}
    cfg = Settings(data_dir=data, root=root, includes=['.'])
    cfg.save()
    app = QApplication([])
    app.setStyle('Fusion'); app.setStyleSheet(STYLE)
    window = MainWindow(cfg); window.show()
    events, errors = [], []
    window.backend.event.connect(lambda event: events.append(event))
    window.backend.event.connect(lambda event: errors.append(event) if event['type'] in {'fatal','error','job_error'} else None)
    report = {'passed':False, 'isolated':str(isolated), 'started':time.time()}
    dialog = None

    def wait_for(predicate, timeout=150):
        deadline = time.monotonic()+timeout
        while not predicate():
            if errors:
                raise RuntimeError(errors)
            if time.monotonic()>deadline:
                raise TimeoutError(f'Validation timed out, last events: {events[-3:]}')
            QTest.qWait(30)

    def record(label):
        print(label, flush=True)

    def assets():
        with readonly(data) as db:
            return {row['filename']:dict(row) for row in db.execute('SELECT * FROM assets')}

    def review_page(group):
        dialog.group.setCurrentIndex(dialog.group.findData(group))
        dialog.request_page()
        wait_for(lambda:not dialog.loading)

    try:
        wait_for(lambda:any(e['type']=='ready' for e in events))
        window.backend.send(action='scan')
        wait_for(lambda:any(e['type']=='index_done' for e in events))
        before = assets()
        assert len(before)==3 and all(a['caption_version']==CAPTION_VERSION for a in before.values())
        record('Indexed 3 isolated copies with actual GPU models')
        dialog = OrientationDialog(window); dialog.show()
        dialog.start_button.click()
        wait_for(lambda:dialog.stats.get('checked')==3 and not dialog.stats.get('running'))
        with readonly(data) as db:
            report['recommendations'] = [dict(row) for row in db.execute('SELECT a.filename,c.rotation,c.certain,c.elapsed FROM orientation_checks c JOIN assets a ON a.id=c.asset_id')]
        record('Orientation analysis complete: '+json.dumps(report['recommendations']))
        expected = {'sideways.jpg':90, 'upright.jpg':0, 'sideways.bmp':270}
        assert all(row['rotation']==expected[row['filename']] for row in report['recommendations'])
        review_page('suggestions')
        QTest.qWait(150)
        dialog.grab().save(str(isolated/'orientation_review.png'))
        # Exercise exclusion from the actual review UI.
        review_page('upright')
        upright_id = before['upright.jpg']['id']
        if not any(a['id']==upright_id for a in dialog.items):
            review_page('uncertain')
        card = next(button for button in dialog.findChildren(QPushButton)
                    if button.text()=='Исключить эту фотографию' and button.isVisible())
        card.click()
        wait_for(lambda:dialog.stats.get('excluded')==1)
        for name, rotation in expected.items():
            if rotation:
                window.backend.send(action='orientation_decide', asset_id=before[name]['id'], version=1, rotation=rotation, selected=True)
        wait_for(lambda:dialog.stats.get('selected')==2)
        assert dialog.apply.isVisible() and dialog.rect().contains(dialog.apply.geometry())
        QTest.mouseClick(dialog.apply, Qt.LeftButton)
        wait_for(lambda:dialog.stats.get('applied')==2 and not dialog.stats.get('edits_pending'))
        after = assets()
        with readonly(data) as db:
            edits = [dict(row) for row in db.execute('SELECT * FROM rotation_edits')]
            for edit in edits:
                assert sha256(Path(edit['backup_path']))==edit['before_hash']==copy_hashes[Path(edit['path']).name]
                assert edit['status']=='done' and not edit['error']
            for name in ('sideways.jpg','sideways.bmp'):
                a = after[name]
                assert a['version']==2 and (a['width'],a['height'])==(before[name]['height'],before[name]['width'])
                assert a['embed_version']==EMBED_VERSION and a['caption_version']==CAPTION_VERSION and a['face_version']==FACE_VERSION
                assert not db.execute("SELECT 1 FROM jobs WHERE asset_id=? AND status<>'done'", (a['id'],)).fetchone()
                assert db.execute('SELECT file_version FROM embeddings WHERE unit_id=?',(f"{a['id']}:photo",)).fetchone()[0]==2
            assert after['upright.jpg']['version']==1 and sha256(root/'upright.jpg')==copy_hashes['upright.jpg']
            report['faces_after_rotation'] = db.execute('SELECT count(*) FROM faces WHERE file_version=2').fetchone()[0]
        jpeg_edit = next(e for e in edits if e['path'].endswith('.jpg'))
        with Image.open(jpeg_edit['backup_path']) as old, Image.open(jpeg_edit['path']) as new:
            assert old.tobytes()==new.tobytes()  # JPEG pixel stream was not recompressed.
        # Search exercises flushed LanceDB records for the new file versions.
        window.search_box.setText('люди'); window.search()
        search_id=window.request_id
        wait_for(lambda:any(e['type']=='results' and e.get('id')==search_id for e in events))
        search_event=next(e for e in reversed(events) if e['type']=='results' and e.get('id')==search_id)
        assert {a['version'] for a in search_event['items'] if a['id']!=upright_id}=={2}
        report['search_after_rotation']=[{'id':a['id'],'version':a['version']} for a in search_event['items']]
        review_page('applied'); QTest.qWait(150)
        dialog.grab().save(str(isolated/'rotation_applied.png'))
        record('Applied JPEG + BMP; backups, lossless JPEG, new embeddings/faces/captions/search verified')
        assert dialog.undo.isVisible()
        QTest.mouseClick(dialog.undo, Qt.LeftButton)
        wait_for(lambda:not dialog.stats.get('edits_pending') and dialog.stats.get('applied')==0)
        restored=assets()
        assert {path.name:sha256(path) for path in root.iterdir()}==copy_hashes
        assert restored['sideways.jpg']['version']==restored['sideways.bmp']['version']==3
        record('Undo restored exact original bytes and reindexed both copies')
        # Already registered folders never start a fresh scan or model work.
        scan_events=sum(e['type']=='scan_done' for e in events)
        window.backend.send(action='add_folders',paths=[str(root),str(root)])
        wait_for(lambda:any(e['type']=='folders_added' for e in events))
        folders=next(e for e in reversed(events) if e['type']=='folders_added')
        assert not folders['added'] and len(folders['skipped'])==2
        QTest.qWait(500)
        assert sum(e['type']=='scan_done' for e in events)==scan_events
        report['duplicate_folders_skipped']=2
        report['backup_and_exact_undo']=True
        report['originals_unchanged']=all(sha256(Path(p))==h for p,h in original_hashes.items())
        assert report['originals_unchanged']
        report['passed']=True
    except Exception as exc:
        report['error']=str(exc)
        raise
    finally:
        if dialog:
            dialog.close()
        window.close()
        window.backend.process.join(15)
        report['seconds']=time.time()-report['started']
        destination=production.data_dir/'reports/v04_integration.json'
        destination.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=True,indent=2),flush=True)


if __name__=='__main__':
    multiprocessing.freeze_support()
    main()
