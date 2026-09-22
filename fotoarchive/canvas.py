"""Photo-first cells: no permanent filenames or dates; bounded hover previews."""
from PySide6.QtCore import QEvent, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QLabel, QStyle, QStyledItemDelegate
from .gallery import PhotoModel
from .smooth_scroll import PhotoGallery
from .video import timestamp_text


class ElideLabel(QLabel):
    def setText(self, text):
        super().setText(text)
        self.setToolTip(text)

    def minimumSizeHint(self):
        return QSize(20, 22)

    def sizeHint(self):
        return QSize(220, 22)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setPen(self.palette().windowText().color())
        painter.drawText(self.rect(), Qt.AlignVCenter | Qt.AlignLeft,
                         self.fontMetrics().elidedText(self.text(), Qt.ElideRight, self.width()))


class PhotoDelegate(QStyledItemDelegate):
    stackToggled = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.edge = 220
        self.proportions = False

    def sizeHint(self, option, index):
        width = self.edge
        if self.proportions:
            w,h = index.model().geometry.get(index.row(), (4,3))
            width = round(self.edge * max(.45, min(2.5,w/max(1,h))))
        return QSize(width, self.edge)

    def badge(self, rect):
        return QRect(rect.left()+8, rect.top()+8, 64, 26)

    def paint(self, p, option, index):
        asset = index.data(PhotoModel.AssetRole) or {}
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        rect = option.rect.adjusted(2,2,-2,-2)
        selected = bool(option.state & QStyle.State_Selected)
        dark = option.palette.window().color().lightness() < 128
        p.fillRect(rect, QColor('#25292c' if dark else '#e0e3e4'))
        pixmap = index.data(PhotoModel.PixmapRole)
        if pixmap:
            p.save()
            p.setClipRect(rect)
            scaled = pixmap.scaled(rect.size(), Qt.KeepAspectRatio if self.proportions else Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            p.drawPixmap(rect.x()+(rect.width()-scaled.width())//2,rect.y()+(rect.height()-scaled.height())//2,scaled)
            p.restore()
        if selected:
            p.setPen(QPen(QColor('#159e8f'),3))
            p.setBrush(Qt.NoBrush)
            p.drawRect(rect.adjusted(1,1,-1,-1))
            p.fillRect(QRect(rect.right()-27,rect.top()+5,22,22),QColor('#147f72'))
            p.setPen(Qt.white)
            p.drawText(QRect(rect.right()-27,rect.top()+5,22,22),Qt.AlignCenter,'✓')
        if asset.get('stack_count',0) > 1:
            badge = self.badge(rect)
            p.setBrush(QColor(20,30,34,210))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(badge,4,4)
            p.setPen(Qt.white)
            p.drawText(badge,Qt.AlignCenter,('▾ ' if asset.get('stack_expanded') else '▤ ')+str(asset['stack_count']))
        if asset.get('media_kind') == 'video':
            badge = QRect(rect.left()+7,rect.bottom()-28,100,22)
            p.fillRect(badge,QColor(20,30,34,210))
            p.setPen(Qt.white)
            p.drawText(badge,Qt.AlignCenter,'▶ '+timestamp_text(asset.get('duration_ms')))
        p.restore()

    def editorEvent(self, event, model, option, index):
        if event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
            asset = index.data(PhotoModel.AssetRole) or {}
            if asset.get('stack_count',0) > 1 and self.badge(option.rect.adjusted(2,2,-2,-2)).contains(event.position().toPoint()):
                self.stackToggled.emit(asset['stack_key'])
                return True
        return super().editorEvent(event,model,option,index)


class CanvasGallery(PhotoGallery):
    zoomRequested = Signal(int)
    hoverVideo = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.hover_asset = None
        self.frames = []
        self.frame_number = 0
        self.hover_delay = QTimer(self)
        self.hover_delay.setSingleShot(True)
        self.hover_delay.timeout.connect(lambda: self.hoverVideo.emit(self.hover_asset) if self.hover_asset else None)
        self.hover_clock = QTimer(self)
        self.hover_clock.setInterval(650)
        self.hover_clock.timeout.connect(self.advance_hover)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            self.zoomRequested.emit(16 if event.angleDelta().y()>0 else -16)
            event.accept()
            return
        self.stop_hover()
        super().wheelEvent(event)

    def stop_hover(self):
        self.hover_delay.stop()
        self.hover_clock.stop()
        self.hover_asset = None
        if self.model():
            self.model().hover_thumbnail = None
        self.viewport().update()

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        index = self.indexAt(event.position().toPoint())
        asset = index.data(PhotoModel.AssetRole) if index.isValid() else None
        if (asset or {}).get('id') != (self.hover_asset or {}).get('id'):
            self.stop_hover()
            if asset and asset.get('media_kind') == 'video':
                self.hover_asset = asset
                self.hover_delay.start(450)

    def leaveEvent(self, event):
        self.stop_hover()
        super().leaveEvent(event)

    def keyPressEvent(self, event):
        self.stop_hover()
        super().keyPressEvent(event)

    def set_storyboard(self, event):
        if self.hover_asset and event['asset_id'] == self.hover_asset['id'] and event['version'] == self.hover_asset['version']:
            self.frames = event['frames']
            self.frame_number = 0
            if self.frames:
                self.hover_clock.start()
                self.advance_hover()

    def advance_hover(self):
        if self.hover_asset and self.frames:
            frame = self.frames[self.frame_number % len(self.frames)]
            self.frame_number += 1
            self.model().hover_thumbnail = (self.hover_asset['id'],frame['thumbnail'])
            self.viewport().update()
