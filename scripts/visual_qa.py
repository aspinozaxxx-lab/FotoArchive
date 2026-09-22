"""Render the application's own Qt widgets and local contact sheets for review."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageDraw, ImageFont
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication
from fotoarchive.config import Settings
from fotoarchive.catalog import Catalog, Filters
from fotoarchive.ui import MainWindow, STYLE

cfg=Settings.load()
catalog=Catalog(cfg)
output=cfg.data_dir/'reports/visual'
output.mkdir(exist_ok=True)
assets=[dict(r) for r in catalog.db.execute('SELECT * FROM assets ORDER BY id')]
font=ImageFont.truetype(r'C:\Windows\Fonts\segoeui.ttf',14)
for page in range((len(assets)+41)//42):
    sheet=Image.new('RGB',(1260,1056),'#f4f6f8')
    draw=ImageDraw.Draw(sheet)
    for position, asset in enumerate(assets[page*42:(page+1)*42]):
        image=Image.open(asset['thumbnail']).convert('RGB')
        image.thumbnail((174,142))
        x=(position%7)*180; y=(position//7)*176
        sheet.paste(image,(x+(180-image.width)//2,y+(142-image.height)//2))
        draw.text((x+5,y+145),f"#{asset['id']} {asset['filename'][:17]}",font=font,fill='#24323c')
    sheet.save(output/f'contact_{page+1}.jpg',quality=92)

class PreviewBackend(QObject):
    event=Signal(dict)
    def send(self, **message):
        if message['action']=='browse':
            items,total=catalog.browse(Filters(**message.get('filters',{})),limit=200)
            QTimer.singleShot(0,lambda:self.event.emit({'type':'results','id':message['id'],'items':items,'total':total}))
    def close(self): pass

app=QApplication([])
app.setStyle('Fusion');app.setStyleSheet(STYLE)
backend=PreviewBackend()
window=MainWindow(cfg,backend)
window.show()
window.update_facets(catalog.facets())
window.on_event({'type':'status','stats':catalog.stats(),'paused':True})
def capture():
    window.gallery.setCurrentIndex(window.model.index(0))
    window.grab().save(str(output/'application.png'))
    window.close();app.quit()
QTimer.singleShot(1800,capture)
app.exec()
catalog.close()
print(str(output))
