"""Pinned, resumable download of the AMD-compatible video model modules."""
from pathlib import Path
import argparse
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

def download(directory, progress=print):
    from fotoarchive.config import Settings
    from fotoarchive.video_setup import download_models
    cfg=Settings(data_dir=Path(directory));cfg.initialize()
    return download_models(cfg,progress)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', default=r'D:\FotoArchiveData')
    args = parser.parse_args()
    download(args.data)
