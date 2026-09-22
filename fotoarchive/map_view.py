"""Detailed native embedded map with persistent on-demand local storage."""
import json
import math
from pathlib import Path
from PySide6.QtCore import QObject,Signal,Slot,QUrl,Qt
from PySide6.QtWidgets import QWidget,QVBoxLayout


def valid_bounds(value):
    return isinstance(value,(tuple,list)) and len(value)==4 and all(
        isinstance(v,(float,int)) and math.isfinite(v) for v in value) and (
        -180<=value[0]<value[2]<=180 and -90<=value[1]<value[3]<=90)


class MapBridge(QObject):
    status=Signal(dict)
    view=Signal(object)
    area=Signal(str)
    ready=Signal()
    @Slot(str)
    def viewport(self,value):
        try:
            bounds=json.loads(value)
            if isinstance(bounds,list) and len(bounds)==4 and all(isinstance(v,(float,int)) and math.isfinite(v) for v in bounds):
                bounds=[max(-180,min(180,float(bounds[0]))),max(-90,min(90,float(bounds[1]))),
                        max(-180,min(180,float(bounds[2]))),max(-90,min(90,float(bounds[3])))]
            if valid_bounds(bounds):self.view.emit(tuple(map(float,bounds)))
        except (ValueError,TypeError):pass
    @Slot(str)
    def selected(self,value):
        try:
            bounds=json.loads(value)
            if valid_bounds(bounds):self.area.emit(','.join(str(float(v)) for v in bounds))
        except (ValueError,TypeError):pass
    @Slot()
    def loaded(self):self.ready.emit()


class MapView(QWidget):
    boundsSelected=Signal(str);viewportChanged=Signal();mapStatus=Signal(dict)
    DEFAULT_BOUNDS=(34.8,54.0,40.6,57.3)
    def __init__(self,parent=None,data_dir=None):
        super().__init__(parent)
        self.setMinimumHeight(200)
        self.data_dir=Path(data_dir) if data_dir else Path.home()/'FotoArchiveData';self.bounds=self.DEFAULT_BOUNDS;self.points=[]
        self.web=None;self.server=None;self.ready=False
        self.bridge=MapBridge(self);self.bridge.view.connect(self.change_view)
        self.bridge.area.connect(self.boundsSelected);self.bridge.status.connect(self.mapStatus)
        self.bridge.ready.connect(self.loaded)
        self.layout=QVBoxLayout(self);self.layout.setContentsMargins(0,0,0,0)
        self.package=self  # Shared retry action in the catalogue toolbar.

    def showEvent(self,event):
        super().showEvent(event)
        if not self.web:self.start()

    def start(self):
        from PySide6.QtWebChannel import QWebChannel
        from PySide6.QtWebEngineCore import QWebEnginePage,QWebEngineUrlRequestInterceptor,QWebEngineProfile
        from PySide6.QtWebEngineWidgets import QWebEngineView
        self.ensure_server()
        origin=self.server.origin
        class LocalOnly(QWebEngineUrlRequestInterceptor):
            def interceptRequest(self,info):
                url=info.requestUrl()
                if url.scheme() not in ('qrc','data','blob') and not url.toString().startswith(origin+'/'):info.block(True)
        self.profile=QWebEngineProfile(self)
        self.interceptor=LocalOnly(self);self.profile.setUrlRequestInterceptor(self.interceptor)
        self.web=QWebEngineView(self);self.page=QWebEnginePage(self.profile,self.web)
        self.web.setPage(self.page);self.channel=QWebChannel(self.page)
        self.channel.registerObject('bridge',self.bridge);self.page.setWebChannel(self.channel)
        self.layout.addWidget(self.web)
        self.theme='dark' if self.palette().window().color().lightness()<128 else 'light'
        self.web.setUrl(QUrl(self.server.url+'index.html?theme='+self.theme));self.ensure()

    def ensure_server(self):
        if not self.server:
            from .map_server import MapServer
            self.server=MapServer(self.data_dir/'maps'/'detailed',self.bridge.status.emit)

    def ensure(self):
        self.ensure_server();self.server.ensure()

    def retry(self):
        self.ensure()
        if self.ready:
            self.ready=False;self.page.runJavaScript('fotoMap.theme('+json.dumps(self.theme)+')')

    def loaded(self):
        self.ready=True;self.set_points(self.points)

    def change_view(self,bounds):
        self.bounds=bounds;self.viewportChanged.emit()

    def visible_bounds(self):return ','.join(f'{v:.7f}' for v in self.bounds)

    def set_points(self,points):
        self.points=points
        if self.ready:self.page.runJavaScript('fotoMap.points('+json.dumps(points,ensure_ascii=False)+')')

    def set_theme(self,dark):
        self.theme='dark' if dark else 'light'
        if self.ready:
            self.ready=False
            self.page.runJavaScript('fotoMap.theme('+json.dumps(self.theme)+')')

    def reset(self):self.fit((-179,-75,179,80))

    def fit(self,bounds):
        if not valid_bounds(bounds):return
        self.bounds=tuple(bounds)
        if self.ready:self.page.runJavaScript('fotoMap.fit('+json.dumps(bounds)+')')

    def fit_points(self):
        if self.points:
            self.fit((min(p['longitude'] for p in self.points)-.01,min(p['latitude'] for p in self.points)-.01,
                      max(p['longitude'] for p in self.points)+.01,max(p['latitude'] for p in self.points)+.01))

    def close_resources(self):
        self.ready=False
        if self.web:
            self.web.stop()
            # Delete the page before its profile, including during app shutdown
            # when the event loop no longer processes deferred deletions.
            import shiboken6
            shiboken6.delete(self.web);self.web=None
            shiboken6.delete(self.profile)
        if self.server:self.server.close();self.server=None
