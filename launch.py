"""Executable entry point. Imports are deferred for Windows multiprocessing freezing."""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        from fotoarchive.__main__ import main
        raise SystemExit(main())
    except Exception:
        import os
        import sys
        import traceback
        from pathlib import Path
        folder = Path(os.environ.get("FOTOARCHIVE_DATA", r"D:\FotoArchiveData")) / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        error_log = folder / "startup_error.log"
        error_log.write_text(traceback.format_exc(), encoding="utf-8")
        if "--smoke-test" not in sys.argv:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, f"Не удалось открыть приложение. Подробности: {error_log}", "FotoArchive", 0x10)
        raise SystemExit(1)
