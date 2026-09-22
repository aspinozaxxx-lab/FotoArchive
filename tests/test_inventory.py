import time

from PIL import Image

from fotoarchive.catalog import Catalog
from fotoarchive.config import Settings
from fotoarchive.engine import Engine
from fotoarchive.inventory import SourceInventory
from fotoarchive.source_details import SourceDetailsDialog, source_summary
from fotoarchive.ui import MainWindow
from test_ui import FakeBackend


def config(tmp_path):
    root = tmp_path / 'photos'
    (root / 'first' / 'nested').mkdir(parents=True)
    return Settings(data_dir=tmp_path / 'data', root=root, includes=['first'])


def collect(inventory):
    deadline = time.monotonic() + 5
    while not inventory.collect():
        assert time.monotonic() < deadline
        time.sleep(.01)
    return inventory.status()


def test_total_precedes_registration_is_stable_and_includes_failed_photos(tmp_path):
    cfg = config(tmp_path)
    paths = [cfg.root / 'first' / f'{i}.JPG' for i in range(3)]
    for path in paths:
        Image.new('RGB', (64, 48)).save(path)
    broken = cfg.root / 'first' / 'nested' / 'broken.bmp'
    broken.write_bytes(b'broken')
    for name in ('photo.xmp', 'photo.aae', 'photo.cr2', 'video.mp4'):
        (cfg.root / 'first' / name).write_bytes(b'sidecar or unsupported')
    engine = Engine(cfg)
    inventory = engine.pipeline.inventory
    try:
        inventory.ensure()
        assert inventory.status()['total'] is None
        status = collect(inventory)
        assert status['total'] == 6 and status['counts']['registered'] == 0
        assert inventory.saved['skipped'] == 2
        assert status['format_counts'] == {'.jpg': 3, '.bmp': 1, '.xmp': 1, '.aae': 1, '.cr2': 1, '.mp4': 1}
        assert 'изображений: 5' in source_summary(status)
        for scanned, path in enumerate(paths + [broken, cfg.root/'first/photo.cr2', cfg.root/'first/video.mp4'], 1):
            asset_id, _ = engine.catalog.register(path)
            engine.process_one(('metadata',), asset_id)
            status = inventory.status()
            assert status['total'] == 6 and status['counts']['scanned'] == scanned
        assert status['counts']['metadata'] == 3
        assert status['counts']['unreadable'] == 3
    finally:
        engine.close()


def test_nested_folders_restart_and_expanding_selection_do_not_double_count(tmp_path):
    cfg = config(tmp_path)
    (cfg.root / 'first' / 'one.jpg').write_bytes(b'photo')
    (cfg.root / 'first' / 'nested' / 'two.bmp').write_bytes(b'photo')
    cfg.includes += ['first/nested', 'first']
    cat = Catalog(cfg)
    inv = SourceInventory(cat)
    try:
        inv.ensure()
        assert collect(inv)['total'] == 2
        inv.close()
        inv = SourceInventory(cat)
        inv.ensure()
        assert inv.status()['total'] == 2 and inv.counter is None
        assert inv.status()['format_counts'] == {'.jpg': 1, '.bmp': 1}
        (cfg.root / 'second').mkdir()
        (cfg.root / 'second' / 'three.jpeg').write_bytes(b'photo')
        cfg.includes.append('second')
        inv.start()
        assert inv.status()['total'] is None
        assert collect(inv)['total'] == 3
    finally:
        inv.close()
        cat.close()


def test_unavailable_folder_preserves_last_complete_manifest_and_retries(tmp_path):
    cfg = config(tmp_path)
    photo = cfg.root / 'first' / 'one.jpg'
    photo.write_bytes(b'photo')
    cat = Catalog(cfg)
    inv = SourceInventory(cat)
    try:
        inv.ensure()
        assert collect(inv)['total'] == 1
        saved = cat.state('source_inventory')
        (cfg.root / 'first').rename(cfg.root / 'offline')
        inv.start()
        state = collect(inv)
        assert state['phase'] == 'error' and state['total'] is None and state['last_total'] == 1
        assert state['format_counts'] is None
        assert cat.state('source_inventory') == saved
        assert cat.db.execute('SELECT count(*) FROM source_inventory').fetchone()[0] == 1
        (cfg.root / 'offline').rename(cfg.root / 'first')
        inv.start()
        assert collect(inv)['total'] == 1
    finally:
        inv.close()
        cat.close()


def test_changing_selection_during_count_uses_only_the_latest_manifest(tmp_path):
    cfg = config(tmp_path)
    for i in range(700):
        (cfg.root / 'first' / f'{i}.jpg').write_bytes(b'photo')
    cat = Catalog(cfg)
    inv = SourceInventory(cat)
    try:
        inv.ensure()
        (cfg.root / 'second').mkdir()
        (cfg.root / 'second' / 'extra.bmp').write_bytes(b'photo')
        cfg.includes.append('second')
        inv.start()
        assert collect(inv)['total'] == 701
        assert not list(cfg.data_dir.glob('inventory-*.sqlite3'))
    finally:
        inv.close()
        cat.close()


def test_footer_uses_source_total_instead_of_growing_registered_count(qtbot, tmp_path):
    window = MainWindow(Settings(data_dir=tmp_path), FakeBackend())
    qtbot.addWidget(window)
    window.on_event({'type': 'source_inventory', 'inventory': {'phase': 'counting', 'total': None}})
    label, bar, _ = window.progress_bars['metadata']
    assert 'считаю снимки' in label.text() and bar.maximum() == 0
    for registered, processed in ((4, 2), (21, 15), (40, 40)):
        window.on_event({'type': 'status', 'paused': False,
            'stats': {'total': registered, 'metadata': processed - 1, 'embeddings': 0, 'captions': 0,
                      'faces': 0, 'locations': 0, 'errors': 1, 'pending': 100},
            'inventory': {'phase': 'ready', 'total': 1000, 'counts': {
                'scanned': processed, 'metadata': processed - 1, 'unreadable': 1}}})
        assert label.text() == f'Каталог  {processed} / 1 000'
        assert bar.maximum() == 1000 and bar.value() == processed
        assert 'Не удалось прочитать: 1' in label.toolTip()
    window.on_event({'type': 'source_inventory', 'inventory': {
        'phase': 'error', 'total': None, 'last_total': 1000, 'error': 'Disk unavailable'}})
    assert 'подсчёт недоступен' in label.text()
    assert '1000' in label.toolTip()
    window.close()


def test_old_inventory_recounts_formats_without_reprocessing_photos(tmp_path):
    cfg = config(tmp_path)
    path = cfg.root / 'first' / 'one.jpg'
    Image.new('RGB', (64, 48)).save(path)
    engine = Engine(cfg)
    inv = engine.pipeline.inventory
    try:
        engine.catalog.register(path)
        engine.process_one(('metadata',))
        inv.ensure()
        collect(inv)
        legacy = engine.catalog.state('source_inventory')
        legacy.pop('format_counts')
        engine.catalog.set_state('source_inventory', legacy)
        jobs = [tuple(row) for row in engine.catalog.db.execute('SELECT * FROM jobs ORDER BY asset_id,stage')]
        replacement = SourceInventory(engine.catalog)
        try:
            replacement.ensure()
            assert replacement.counter is not None
            assert collect(replacement)['format_counts'] == {'.jpg': 1}
            assert [tuple(row) for row in engine.catalog.db.execute('SELECT * FROM jobs ORDER BY asset_id,stage')] == jobs
        finally:
            replacement.close()
    finally:
        engine.close()


def test_inventory_explains_supported_and_unsupported_files_only_in_selected_folders(tmp_path):
    cfg = config(tmp_path)
    for name in ('one.JPG', 'raw.CR2', 'raw.XMP', 'phone.HEIC', 'phone.AAE', 'clip.MOV', 'notes.txt'):
        (cfg.root / 'first' / name).write_bytes(b'test')
    (cfg.root / 'not-selected').mkdir()
    (cfg.root / 'not-selected' / 'extra.jpg').write_bytes(b'test')
    cfg.includes += ['first/nested', 'first']
    cat = Catalog(cfg)
    inv = SourceInventory(cat)
    try:
        inv.ensure()
        state = collect(inv)
        assert state['total'] == 4
        assert sum(state['format_counts'].values()) == 7
        assert 'изображений: 3' in source_summary(state)
        assert 'всего для каталога: 4' in source_summary(state)
    finally:
        inv.close()
        cat.close()


def test_source_scope_is_visible_without_counting_sidecars_as_photos(qtbot, tmp_path):
    window = MainWindow(Settings(data_dir=tmp_path), FakeBackend())
    qtbot.addWidget(window)
    inventory = {'phase': 'ready', 'total': 73966, 'counts': {'scanned': 73966, 'metadata': 73943},
                 'root': r'F:\MyFoto', 'includes': [str(year) for year in range(2003, 2015)],
                 'format_counts': {'.jpg': 47079, '.bmp': 20, '.jpeg': 3, '.cr2': 25446,
                                   '.arw': 1040, '.crw': 22, '.png': 76, '.mov': 280, '.xmp': 26507}}
    window.on_event({'type': 'source_inventory', 'inventory': inventory})
    assert '73 686' in window.source_label.text() and '73 966' in window.source_label.text()
    dialog = SourceDetailsDialog(window)
    qtbot.addWidget(dialog)
    text = dialog.details.toPlainText()
    assert '2003' in text and '2014' in text and '2015' not in text
    assert 'RAW: 26 508' in text and 'видео: 280' in text
    assert 'Каталогизация файлов завершена' in text
    dialog.close()
    window.close()
