import json
from pathlib import Path

import av
import numpy as np
from PIL import Image
import pytest

from fotoarchive.catalog import Catalog, Filters
from fotoarchive.config import Settings, EMBED_VERSION
from fotoarchive.engine import Engine
from fotoarchive.location import extract_location
from fotoarchive.media import read_media, open_rgb, sha256, visual_path
from fotoarchive.search_session import SearchSession
from fotoarchive.video import frame_at, probe


def make_video(path, duration=22):
    path.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(path), 'w') as output:
        stream = output.add_stream('mpeg4', rate=2)
        stream.width, stream.height, stream.pix_fmt = 96, 64, 'yuv420p'
        output.metadata['creation_time'] = '2003-05-09T12:30:45Z'
        output.metadata['location'] = '+55.75+037.61/'
        for i in range(duration * 2):
            frame = av.VideoFrame.from_image(Image.new('RGB', (96, 64), 'red' if i < 20 else 'blue'))
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    return path


@pytest.fixture
def cfg(tmp_path):
    return Settings(data_dir=tmp_path/'data', root=tmp_path/'originals')


def test_video_metadata_sampling_gps_and_original_integrity(cfg):
    path = make_video(cfg.root/'2003/clip.mp4')
    before = sha256(path)
    metadata, image = probe(path)
    assert metadata['duration_ms'] == 22000 and image.size == (96, 64)
    assert metadata['captured_at'].startswith('2003-05-09T12:30:45')
    image, actual = frame_at(path, 15000)
    assert 15000 <= actual <= 15500 and image.getpixel((40, 30))[2] > 220
    location = extract_location(path)
    assert location['latitude'] == 55.75 and location['longitude'] == 37.61
    assert sha256(path) == before


def prepare_video(engine):
    path = make_video(engine.cfg.root/'2003/clip.mp4')
    asset_id, _ = engine.catalog.register(path)
    engine.process_one(('metadata',), asset_id)
    assert engine.catalog.get(asset_id)['metadata_ready'] == 1
    return asset_id, path


def test_frame_progress_resumes_filters_and_search_return_matched_time(cfg):
    engine = Engine(cfg)
    try:
        asset_id, path = prepare_video(engine)
        cat = engine.catalog
        assert cat.stats()['total'] == 1
        assert cat.db.execute('SELECT count(*) FROM units').fetchone()[0] == 3
        vectors = np.eye(3, 768, dtype=np.float32)
        first = cat.next_job(('embedding',))
        cat.complete_embedding(first, vectors[0]); cat.finish_job(first, .1)
        assert cat.get(asset_id)['embed_version'] is None
        interrupted = cat.next_job(('embedding',))
        assert interrupted['timestamp_ms'] == 10000
        cat.recover()
        resumed = cat.next_job(('embedding',))
        assert resumed['unit_id'] == interrupted['unit_id']
        cat.complete_embedding(resumed, vectors[1]); cat.finish_job(resumed, .1)
        last = cat.next_job(('embedding',))
        cat.complete_embedding(last, vectors[2]); cat.finish_job(last, .1)
        assert cat.get(asset_id)['embed_version'] == EMBED_VERSION
        image_path = visual_path(cat.job_asset(resumed))
        assert Image.open(image_path).getpixel((40, 30))[2] > 220
        engine.index.flush_all()
        assert engine.index.table.count_rows() == 3
        assert cat.browse(Filters(media_kind='photo'))[1] == 0
        assert cat.browse(Filters(media_kind='video', date_from='2003-01-01'))[1] == 1
        assert engine.index.candidates(vectors[1], '', Filters(media_kind='photo')) == []
        session = SearchSession(cat, engine.index, 1, Filters(media_kind='video'), '', vectors[1], [])
        result = session.page(expand=True)
        assert len(result['items']) == 1
        assert result['items'][0]['timestamp_ms'] == 10000
        assert result['items'][0]['unit_id'] == resumed['unit_id']
        assert session.exhausted
    finally:
        engine.close()


def test_changed_video_removes_stale_frames_and_outbox_replays(cfg):
    engine = Engine(cfg)
    try:
        asset_id, path = prepare_video(engine)
        cat = engine.catalog
        while job := cat.next_job(('embedding',)):
            cat.complete_embedding(job, np.eye(1, 768, dtype=np.float32)[0]); cat.finish_job(job, .1)
        engine.index.flush_all()
        assert engine.index.table.count_rows() == 3
        make_video(path, duration=4)
        assert cat.register(path) == (asset_id, True)
        assert cat.db.execute('SELECT count(*) FROM unit_jobs').fetchone()[0] == 0
        engine.index.flush_all()
        assert engine.index.table.count_rows() == 0
        engine.process_one(('metadata',), asset_id)
        assert cat.db.execute('SELECT count(*) FROM units').fetchone()[0] == 1
    finally:
        engine.close()


def test_frame_error_does_not_block_other_frames_and_retry_is_local(cfg):
    engine = Engine(cfg)
    try:
        asset_id, _ = prepare_video(engine)
        cat = engine.catalog
        job = cat.next_job(('caption',)); cat.finish_job(job, .1, 'bad frame')
        for _ in range(2):
            next_job = cat.next_job(('caption',))
            assert next_job['unit_id'] != job['unit_id']
            cat.complete_caption(next_job, {'description': 'scene'}); cat.finish_job(next_job, .1)
        assert cat.next_job(('caption',)) is None
        assert cat.db.execute("SELECT status FROM jobs WHERE stage='caption'").fetchone()[0] == 'error'
        cat.retry_errors()
        assert cat.next_job(('caption',))['unit_id'] == job['unit_id']
    finally:
        engine.close()


@pytest.mark.parametrize('suffix,format', [('.png', 'PNG'), ('.tif', 'TIFF'), ('.heic', 'HEIF'), ('.webp', 'WEBP'), ('.gif', 'GIF')])
def test_image_formats_read_exif_and_preserve_source(tmp_path, suffix, format):
    path = tmp_path / ('sample' + suffix)
    image = Image.new('RGB', (80, 60), 'green')
    exif = Image.Exif(); exif[34665] = {36867: '2002:12:31 23:59:58'}
    image.save(path, format=format, exif=exif.tobytes())
    before = sha256(path)
    metadata, decoded = read_media(path)
    assert decoded.size == (80, 60)
    assert metadata['captured_at'] == ('2002-12-31T23:59:58' if suffix != '.gif' else None)
    assert sha256(path) == before


def test_media_switch_combines_with_other_filters(qtbot, tmp_path):
    from test_ui import FakeBackend
    from fotoarchive.ui import MainWindow
    backend = FakeBackend()
    window = MainWindow(Settings(data_dir=tmp_path), backend)
    qtbot.addWidget(window)
    window.search_box.setText('people by the river')
    window.unknown.setChecked(True)
    window.media_combo.setCurrentIndex(2)
    assert backend.sent[-1]['filters']['media_kind'] == 'video'
    assert backend.sent[-1]['filters']['unknown_date'] is True
    assert backend.sent[-1]['query'] == 'people by the river'
    window.reset_filters()
    assert backend.sent[-1]['filters']['media_kind'] == ''
    window.close()


def test_face_matches_keep_their_video_frame(cfg):
    engine = Engine(cfg)
    try:
        asset_id, _ = prepare_video(engine)
        cat = engine.catalog
        records = []
        for i in range(2):
            job = cat.next_job(('faces',))
            vector = np.zeros(128, np.float32); vector[i] = 1
            face = {'box': [.1, .1, .8, .8], 'landmarks': [[.2,.2]]*5, 'confidence': .99,
                    'vector': vector, 'portrait': Image.new('RGB', (32,32), 'green')}
            cat.complete_faces(job, [face]); cat.finish_job(job, .1)
            records.append((job, vector))
        assert len(cat.faces_for(asset_id)) == 2
        first, second = records
        assert len(engine.load_faces(asset_id, first[0]['unit_id'])) == 1
        engine.index.flush_all()
        session = SearchSession(cat, engine.index, 3, Filters(media_kind='video'), '', second[1], [], mode='face')
        results = session.page(expand=True)['items']
        assert len(results) == 1 and results[0]['timestamp_ms'] == 10000
        assert cat.face_info(results[0]['face_match_id'])['unit_id'] == second[0]['unit_id']
    finally:
        engine.close()


def test_video_verification_cache_is_per_frame_and_does_not_claim_the_whole_clip(cfg):
    class Vision:
        def __init__(self): self.paths = []
        def verify(self, path, conditions):
            self.paths.append(path)
            return {'verdict': 'yes', 'checks': []}
        def close(self): pass
    engine = Engine(cfg)
    engine.vlm = Vision()
    try:
        asset_id, _ = prepare_video(engine)
        first = engine.catalog.media_units.asset_at(engine.catalog.get(asset_id), f'{asset_id}:frame:0')
        second = engine.catalog.media_units.asset_at(engine.catalog.get(asset_id), f'{asset_id}:frame:10000')
        for asset in (first, second):
            result = engine.verify(asset, ['a scene'])
            assert result['scope'] == 'frame' and result['timestamp_ms'] == asset['timestamp_ms']
        assert engine.verify(first, ['a scene'])['cached']
        assert len(set(engine.vlm.paths)) == 2
    finally:
        engine.close()


def test_deep_video_frame_stream_does_not_hide_later_files(cfg):
    engine = Engine(cfg)
    try:
        asset_id, path = prepare_video(engine)
        cat = engine.catalog
        metadata, image = probe(path)
        metadata['duration_ms'] = 2050000  # synthetic scale fixture, no decoding of extra frames
        cat.complete_metadata({'asset_id': asset_id, 'file_version': 1}, metadata, path, 'test')
        vector = np.zeros(768, np.float32); vector[0] = 1
        with cat.db:
            for unit in cat.db.execute('SELECT id FROM units').fetchall():
                cat.db.execute('INSERT INTO embeddings VALUES(?,?,?,?,?)', (unit['id'],1,EMBED_VERSION,768,vector.tobytes()))
            cat._enqueue(asset_id, 1)
        photo = path.with_name('later.jpg'); Image.new('RGB', (64,48)).save(photo)
        photo_id, _ = cat.register(photo); engine.process_one(('metadata',), photo_id)
        other = vector.copy(); other[1] = .2; other /= np.linalg.norm(other)
        job = cat.next_job(('embedding',), photo_id)
        cat.complete_embedding(job, other); cat.finish_job(job, .1)
        engine.index.flush_all()
        session = SearchSession(cat, engine.index, 1, Filters(), '', vector, [])
        results = session.page(expand=True)['items']
        assert [row['id'] for row in results] == [asset_id, photo_id]
        assert session.exhausted
    finally:
        engine.close()


def test_video_player_opens_at_found_moment_and_stops_on_close(qtbot, tmp_path):
    from PySide6.QtMultimedia import QMediaPlayer
    from fotoarchive.video_player import VideoPlayerDialog
    path = make_video(tmp_path/'clip.mp4')
    player = VideoPlayerDialog({'path': str(path), 'filename': path.name, 'timestamp_ms': 10000}, Settings(data_dir=tmp_path/'data'))
    qtbot.addWidget(player)
    player.show()
    qtbot.waitUntil(lambda: player.player.position() >= 10000 or player.player.error() != QMediaPlayer.NoError, timeout=15000)
    assert player.player.error() == QMediaPlayer.NoError
    assert player.slider.maximum() == 22000
    player.close()
    assert player.player.playbackState() == QMediaPlayer.StoppedState
    assert player.player.source().isEmpty()
