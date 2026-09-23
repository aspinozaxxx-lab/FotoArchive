"""A native FFmpeg/GIL deadlock needs an external process deadline."""
import json
from pathlib import Path
import subprocess
import sys


def test_video_end_and_close_with_share_menu_do_not_deadlock(tmp_path):
    result = subprocess.run(
        [sys.executable, '-m', 'fotoarchive', '--playback-smoke-test', '--data-dir', str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        encoding='utf-8', errors='replace', timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / 'playback-smoke.json').read_text(encoding='utf-8'))
    assert report['passed'] and len(report['checks']) == 20
    assert report['attempted_shares'] == 0 and report['originals_unchanged']
    assert report['max_close_ms'] < 1000
