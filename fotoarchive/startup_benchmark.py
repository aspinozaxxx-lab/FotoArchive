"""Read-only startup probe, also usable from the packaged Windows executable."""
import json
import os
from pathlib import Path
import sys
import time


def run(data_dir, started):
    # The external probe includes bootloader/OS time before Python imports.
    started = float(os.environ.get('FOTOARCHIVE_BENCHMARK_STARTED', started))
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    os.environ.setdefault('QT_QPA_FONTDIR', str(Path(os.environ.get('WINDIR', r'C:\Windows'))/'Fonts'))
    from PySide6.QtCore import QObject, Signal, QTimer, QCoreApplication, Qt
    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    from PySide6.QtWidgets import QApplication
    from .config import Settings
    from .browse_reader import BrowseReader
    from .preferences import Preferences
    from . import ui, __version__

    class ReadOnlyPreferences(Preferences):
        def save(self, **values):
            self.values.update(values)

    class Backend(QObject):
        event = Signal(dict)
        def __init__(self, cfg):
            super().__init__()
            self.reader = BrowseReader(cfg, self.event.emit)
            self.reader.enable()
        def send(self, **message):
            if message['action'] == 'browse':
                self.reader.replace(message)
            elif message['action'] == 'search_page':
                self.reader.page(message)
        def close(self):
            self.reader.close()
            self.reader.thread.join(3)

    cfg = Settings.load(data_dir)
    app = QApplication([])
    app.setStyle('Fusion')
    app.setStyleSheet(ui.STYLE)
    original_preferences = ui.Preferences
    ui.Preferences = ReadOnlyPreferences
    window = ui.MainWindow(cfg, Backend(cfg))
    ui.Preferences = original_preferences
    report = dict(version=__version__, frozen=bool(getattr(sys, 'frozen', False)),
        scope='read-only catalogue, actual Qt layout and SSD thumbnails; no inference, originals or preference writes',
        window_seconds=time.perf_counter()-started)
    samples = []
    previous = time.perf_counter()
    finished = False
    def result(event):
        if event.get('type') == 'results':
            report.setdefault('first_results_seconds', time.perf_counter()-started)
            report.update(total=event['total'], cached=event.get('cached', False))
        elif event.get('type') == 'error':
            report['error'] = event.get('message')
    def image(key, value):
        if not value.isNull():
            report.setdefault('first_thumbnail_seconds', time.perf_counter()-started)
    def tick():
        nonlocal previous, finished
        now = time.perf_counter()
        samples.append((now-previous)*1000)
        previous = now
        if not finished and ('first_thumbnail_seconds' in report and now-started > report['first_thumbnail_seconds']+1
                or now-started > 25):
            finished = True
            report['ui_tick_p95_ms'] = sorted(samples)[round((len(samples)-1)*.95)]
            report['max_ui_tick_ms'] = max(samples)
            report['passed'] = 'first_thumbnail_seconds' in report and 'error' not in report
            window.close()
            app.quit()
    timer = QTimer(window)
    timer.setInterval(16)
    timer.timeout.connect(tick)
    window.backend.event.connect(result)
    window.model.signals.ready.connect(image)
    window.show()
    timer.start()
    app.exec()
    (cfg.data_dir/'reports').mkdir(exist_ok=True)
    (cfg.data_dir/'reports/startup-benchmark.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return 0 if report.get('passed') else 1
