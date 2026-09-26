import argparse
import multiprocessing
import sys
import time
from pathlib import Path

STARTED = time.perf_counter()


def main():
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description="FotoArchive")
    parser.add_argument("--data-dir")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--media-smoke-test", action="store_true")
    parser.add_argument('--browse-benchmark', action='store_true', help='Measure catalogue reads without opening or changing the library')
    parser.add_argument('--startup-benchmark', action='store_true', help='Measure read-only first-window and thumbnail readiness without starting inference')
    parser.add_argument('--ui-smoke-test', action='store_true', help='Check the packaged Qt gallery and reader with generated records, without a real catalogue or GPU')
    parser.add_argument("--analyze-orientation", action="store_true", help="Start/resume recommendations for the already indexed photos")
    parser.add_argument('--map-smoke-test',action='store_true')
    parser.add_argument('--map-offline',action='store_true')
    parser.add_argument('--playback-smoke-test', action='store_true', help='Check photo/video sharing menu and closing with isolated generated media')
    args = parser.parse_args()
    if args.startup_benchmark:
        from .startup_benchmark import run
        return run(args.data_dir, STARTED)
    if args.playback_smoke_test:
        if not args.data_dir:parser.error('--playback-smoke-test requires an isolated --data-dir')
        from .playback_smoke import run
        return run(Path(args.data_dir))
    if args.map_smoke_test:
        if not args.data_dir:parser.error('--map-smoke-test requires --data-dir')
        from .map_smoke import run
        return run(Path(args.data_dir),offline=args.map_offline)
    if args.ui_smoke_test:
        if not args.data_dir:
            parser.error('--ui-smoke-test requires an isolated --data-dir for its report')
        from .ui_smoke import run
        return run(Path(args.data_dir))
    if args.browse_benchmark:
        from .config import Settings
        from .browse_benchmark import run
        return run(Settings.load(args.data_dir))
    if args.media_smoke_test:
        from .config import Settings
        from .media_smoke import run
        return run(Settings.load(args.data_dir))
    if args.smoke_test:
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        # The offscreen Qt plugin does not discover Windows system fonts itself.
        os.environ.setdefault("QT_QPA_FONTDIR", str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"))
    from .config import Settings
    from PySide6.QtCore import QLockFile
    from PySide6.QtCore import QCoreApplication,Qt
    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QMessageBox
    from .ui import MainWindow, STYLE
    cfg = Settings.load(args.data_dir)
    cfg.initialize()
    if sys.platform == "win32":
        import ctypes
        identity = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        identity.argtypes = [ctypes.c_wchar_p]
        identity.restype = ctypes.c_long
        identity("FotoArchive.Desktop")
    application = QApplication(sys.argv[:1])
    application.setApplicationName("FotoArchive")
    application.setWindowIcon(QIcon(str(Path(__file__).with_name("assets") / "FotoArchive.ico")))
    application.setStyle("Fusion")
    application.setStyleSheet(STYLE)
    lock = QLockFile(str(cfg.data_dir / "application.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.information(None, "FotoArchive", "Приложение уже открыто для этого каталога.")
        return 0
    window = MainWindow(cfg)
    from .startup import record_startup
    record_startup(window, STARTED)
    if args.smoke_test:
        from .smoke import install_smoke_check
        install_smoke_check(application, window, cfg)
    window.show()
    if not args.smoke_test:
        from PySide6.QtCore import QTimer
        QTimer.singleShot(10000,window.map_widget.ensure)
    if args.analyze_orientation:
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0,lambda:window.backend.send(action='orientation_start',filters={}))
    result = application.exec()
    lock.unlock()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
