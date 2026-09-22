import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fotoarchive.map_smoke import run
sys.exit(run(Path(r'D:\FotoArchiveData'),offline='--offline' in sys.argv))
