"""Manual desktop acceptance surface: synthetic file, never sends a message."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image,ImageDraw
from PySide6.QtWidgets import QApplication
from fotoarchive.config import Settings
from fotoarchive.telegram_share import Contacts
from fotoarchive.ui import Viewer
folder=Path(r'D:\FotoArchiveData\reports\v082-telegram-test');folder.mkdir(parents=True,exist_ok=True)
path=folder/'FotoArchive-test.png'
picture=Image.new('RGB',(640,360),'#1d6962');draw=ImageDraw.Draw(picture)
draw.text((35,35),'FotoArchive - attachment preparation test',fill='white');picture.save(path)
cfg=Settings(data_dir=folder)
if len(sys.argv)!=2:raise SystemExit('Usage: probe_share.py @test_contact')
Contacts(folder).add(sys.argv[1])
app=QApplication([])
w=Viewer([dict(id=1,version=1,path=str(path),filename=path.name,width=640,height=360)],0,cfg)
w.setWindowTitle('FotoArchive — проверка Telegram');w.show()
sys.exit(app.exec())
