"""Offline map: public-domain coastline, local GPS clusters, no network requests."""
import json
from pathlib import Path
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QTransform
from PySide6.QtWidgets import QWidget


class MapView(QWidget):
    boundsSelected = Signal(str)
    viewportChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(200)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setToolTip('Колесо — масштаб. Перетаскивание — перемещение. Shift + перетаскивание — выбрать область.')
        self.points = []
        self.bounds = (-180., -90., 180., 90.)
        self.start = self.end = None
        self.selecting = False
        self.auto_fit = True
        self.land = QPainterPath()
        data = json.loads((Path(__file__).with_name('assets') / 'land.geojson').read_text(encoding='utf-8'))
        for feature in data['features']:
            geometry = feature['geometry']
            polygons = geometry['coordinates'] if geometry['type'] == 'MultiPolygon' else [geometry['coordinates']]
            for polygon in polygons:
                for ring in polygon:
                    for i, (lon, lat, *_) in enumerate(ring):
                        (self.land.moveTo if i == 0 else self.land.lineTo)(lon, -lat)
                    self.land.closeSubpath()

    def reset(self):
        self.auto_fit = False
        self.bounds = (-180., -90., 180., 90.)
        self.update()
        self.viewportChanged.emit()

    def set_points(self, points):
        self.points = points
        if self.auto_fit and points:
            self.auto_fit = False
            self.fit_points()
        self.update()

    def fit_points(self):
        if not self.points:
            return
        west,east = min(p['longitude'] for p in self.points),max(p['longitude'] for p in self.points)
        south,north = min(p['latitude'] for p in self.points),max(p['latitude'] for p in self.points)
        dx,dy = max(.03,(east-west)*.12),max(.03,(north-south)*.12)
        self.bounds = max(-180,west-dx),max(-90,south-dy),min(180,east+dx),min(90,north+dy)
        self.update()
        self.viewportChanged.emit()

    def map_rect(self):
        w,s,e,n = self.bounds
        scale = min(self.width()/(e-w),self.height()/(n-s))
        width,height = (e-w)*scale,(n-s)*scale
        return QRectF((self.width()-width)/2,(self.height()-height)/2,width,height)

    def coordinate(self, point):
        w, s, e, n = self.bounds
        rect = self.map_rect()
        return (w+max(0,min(1,(point.x()-rect.left())/max(1,rect.width())))*(e-w),
                n-max(0,min(1,(point.y()-rect.top())/max(1,rect.height())))*(n-s))

    def pixel(self, lon, lat):
        w, s, e, n = self.bounds
        rect = self.map_rect()
        return QPointF(rect.left()+(lon-w)/(e-w)*rect.width(), rect.top()+(n-lat)/(n-s)*rect.height())

    def visible_bounds(self):
        return ','.join(f'{v:.7f}' for v in self.bounds)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        dark = self.palette().window().color().lightness() < 128
        p.fillRect(self.rect(), QColor('#202c35' if dark else '#dfeaf0'))
        w, s, e, n = self.bounds
        rect = self.map_rect()
        scale = rect.width()/(e-w)
        t = QTransform(scale, 0, 0, scale, rect.left()-w*scale, rect.top()+n*scale)
        p.setPen(QPen(QColor('#59686d' if dark else '#a9bab8'), .6))
        p.setBrush(QColor('#384849' if dark else '#f0f2ed'))
        p.drawPath(t.map(self.land))
        p.setPen(QColor('#adbbc0' if dark else '#6e8188'))
        p.drawText(self.rect().adjusted(8, 5, -8, -5), Qt.AlignLeft | Qt.AlignBottom, 'Natural Earth · карта работает без интернета')
        for point in self.points:
            at = self.pixel(point['longitude'], point['latitude'])
            if not self.rect().adjusted(-15,-15,15,15).contains(at.toPoint()):
                continue
            radius = 14 if point['count'] > 1 else 5
            p.setBrush(QColor('#168f82'))
            p.setPen(QPen(QColor('#ffffff'), 1))
            p.drawEllipse(at, radius, radius)
            if radius > 5:
                count = str(point['count']) if point['count'] < 1000 else f"{point['count']/1000:.1f}к"
                p.drawText(QRectF(at.x()-17,at.y()-12,34,24), Qt.AlignCenter, count)
        if self.selecting and self.start and self.end:
            p.setBrush(QColor(22,143,130,45))
            p.setPen(QPen(QColor('#168f82'),2))
            p.drawRect(QRectF(self.start,self.end).normalized())

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.start = self.end = event.position()
            self.start_bounds = self.bounds
            self.selecting = bool(event.modifiers() & Qt.ShiftModifier)

    def mouseMoveEvent(self, event):
        if self.start:
            self.end = event.position()
            if not self.selecting:
                w,s,e,n = self.start_bounds
                scale = min(self.width()/(e-w),self.height()/(n-s))
                dx = (self.end.x()-self.start.x())/scale
                dy = (self.end.y()-self.start.y())/scale
                dx = max(e-180, min(w+180, dx))
                dy = max(-90-s, min(90-n, dy))
                self.bounds = (w-dx,s+dy,e-dx,n+dy)
            self.update()

    def mouseReleaseEvent(self, event):
        if self.start:
            if self.selecting and (event.position()-self.start).manhattanLength() > 6:
                lon1, lat1 = self.coordinate(self.start)
                lon2, lat2 = self.coordinate(event.position())
                self.boundsSelected.emit(','.join(str(v) for v in (min(lon1,lon2),min(lat1,lat2),max(lon1,lon2),max(lat1,lat2))))
            elif (event.position()-self.start).manhattanLength() < 6:
                nearest = min(self.points, key=lambda point: (self.pixel(point['longitude'],point['latitude'])-event.position()).manhattanLength(), default=None)
                if nearest and (self.pixel(nearest['longitude'],nearest['latitude'])-event.position()).manhattanLength() < 20:
                    # A cluster represents a five-degree source grid cell.
                    lon, lat = nearest['longitude'], nearest['latitude']
                    import math
                    w,s = math.floor((lon+180)/5)*5-180, math.floor((lat+90)/5)*5-90
                    self.boundsSelected.emit(','.join(str(nearest.get(key,value)) for key,value in
                        [('west',w),('south',s),('east',min(180,w+5)),('north',min(90,s+5))]))
            self.start = self.end = None
            self.selecting = False
            self.update()
            self.viewportChanged.emit()

    def wheelEvent(self, event):
        w,s,e,n = self.bounds
        lon,lat = self.coordinate(event.position())
        factor = .8 if event.angleDelta().y() > 0 else 1.25
        width,height = min(360,max(.01,(e-w)*factor)),min(180,max(.005,(n-s)*factor))
        west = max(-180,min(180-width,lon-(lon-w)/(e-w)*width))
        south = max(-90,min(90-height,lat-(lat-s)/(n-s)*height))
        self.bounds = west,south,west+width,south+height
        event.accept()
        self.update()
        self.viewportChanged.emit()
