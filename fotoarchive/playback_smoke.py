"""Exercise real Qt/FFmpeg playback and viewer closing, without opening Telegram.

Run in a separate process with a deadline: a native multimedia deadlock cannot
be interrupted by a timer in the frozen GUI thread.
"""
import hashlib
import json
import time
from pathlib import Path


def make_clip(path):
    from fractions import Fraction
    import av
    import numpy as np

    with av.open(str(path), 'w') as output:
        video = output.add_stream('libx264', rate=24)
        video.width, video.height, video.pix_fmt = 160, 96, 'yuv420p'
        audio = output.add_stream('aac', rate=48000)
        audio.layout = 'mono'
        for index in range(18):
            pixels = np.zeros((96, 160, 3), dtype=np.uint8)
            pixels[:, :, index % 3] = 140
            frame = av.VideoFrame.from_ndarray(pixels, format='rgb24')
            frame.pts, frame.time_base = index, Fraction(1, 24)
            for packet in video.encode(frame):
                output.mux(packet)
        for packet in video.encode():
            output.mux(packet)
        for index in range(36):
            frame = av.AudioFrame.from_ndarray(np.zeros((1, 1024), dtype=np.float32),
                                             format='fltp', layout='mono')
            frame.sample_rate, frame.pts, frame.time_base = 48000, index * 1024, Fraction(1, 48000)
            for packet in audio.encode(frame):
                output.mux(packet)
        for packet in audio.encode():
            output.mux(packet)


def run(folder, repeats=5):
    from PIL import Image
    from PySide6.QtCore import QCoreApplication, QTimer, Qt
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from . import __version__
    from .config import Settings
    from .telegram_share import Contacts, TelegramDraft
    from .ui import Viewer

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    clip, photo = folder / 'synthetic.mp4', folder / 'synthetic.jpg'
    make_clip(clip)
    Image.new('RGB', (640, 360), '#1d6962').save(photo)
    original_hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in (clip, photo)]
    cfg = Settings(data_dir=folder / 'isolated-data')
    Contacts(cfg.data_dir).add('test_contact')
    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication([])
    app.setStyle('Fusion')
    app.setQuitOnLastWindowClosed(False)
    report = dict(version=__version__, passed=False, checks=[], attempted_shares=0,
                  scope='synthetic photo and H264/AAC video; no catalogue, GPU inference or Telegram')
    previous_prepare = TelegramDraft.prepare

    def forbidden_share(*_):
        report['attempted_shares'] += 1
        raise AssertionError('Opening/dismissing the menu must never start sharing')

    TelegramDraft.prepare = forbidden_share
    heartbeat_times = []
    heartbeat = QTimer()
    heartbeat.setInterval(10)
    heartbeat.timeout.connect(lambda: heartbeat_times.append(time.perf_counter()))
    heartbeat.start()

    def pump_until(predicate, timeout=5):
        deadline = time.perf_counter() + timeout
        while not predicate():
            app.processEvents()
            if time.perf_counter() > deadline:
                raise TimeoutError('Viewer did not finish its operation')
            time.sleep(.002)

    try:
        for repeat in range(repeats):
            for kind in ('video-close-playing', 'video-close-at-end', 'photo-close', 'photo-escape'):
                path = clip if kind.startswith('video') else photo
                asset = dict(id=1, version=1, path=str(path), filename=path.name,
                             width=160 if kind.startswith('video') else 640, height=96,
                             media_kind='video' if kind.startswith('video') else 'image')
                viewer = Viewer([asset], 0, cfg)
                viewer.setWindowTitle('FotoArchive — проверка закрытия просмотра')
                viewer.show()
                if kind.startswith('video'):
                    player = viewer.video_pane.player
                    viewer.video_pane.audio.setMuted(True)
                    pump_until(lambda: player.duration() > 0)
                else:
                    pump_until(lambda: bool(viewer.scene.items()))
                button = viewer.share_button
                # Exercise the actual click and hover signal paths alternately.
                if repeat % 2:
                    button.underMouse = lambda: True
                    opened_at = time.perf_counter()
                    button.hover.timeout.emit()
                else:
                    opened_at = time.perf_counter()
                    QTest.mouseClick(button, Qt.LeftButton)
                open_ms = (time.perf_counter() - opened_at) * 1000
                assert button.menu.isVisible()
                assert open_ms < 250, f'Menu opening blocked for {open_ms:.0f} ms'
                if kind == 'video-close-at-end':
                    # The reported deadlock also happens at EOF without a close
                    # click: FFmpeg releases its decoder threads inside the menu.
                    pump_until(lambda: player.mediaStatus() == QMediaPlayer.EndOfMedia)
                    assert button.menu.isVisible()
                close_at = time.perf_counter()
                if kind == 'photo-escape':
                    QTest.keyClick(button.menu, Qt.Key_Escape)
                    QTest.keyClick(viewer, Qt.Key_Escape)
                else:
                    viewer.close()
                pump_until(lambda: not viewer.isVisible())
                assert not button.menu.isVisible() and not button.hover.isActive()
                assert button.stop.is_set()
                if kind.startswith('video'):
                    assert player.playbackState() == QMediaPlayer.StoppedState
                    assert player.source().isEmpty()
                report['checks'].append(dict(case=kind, repeat=repeat, open_ms=round(open_ms, 2),
                                             close_ms=round((time.perf_counter()-close_at)*1000, 2)))
                viewer.deleteLater()
                QTest.qWait(30)
        assert report['attempted_shares'] == 0
        assert original_hashes == [hashlib.sha256(path.read_bytes()).hexdigest() for path in (clip, photo)]
        report['originals_unchanged'] = True
        report['max_event_gap_ms'] = round(max((b-a)*1000 for a, b in zip(heartbeat_times, heartbeat_times[1:])), 2)
        report['max_close_ms'] = max(check['close_ms'] for check in report['checks'])
        report['passed'] = True
    except Exception as exc:
        report['error'] = str(exc)
    finally:
        heartbeat.stop()
        TelegramDraft.prepare = previous_prepare
        (folder / 'playback-smoke.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        app.closeAllWindows()
    return 0 if report['passed'] else 1
