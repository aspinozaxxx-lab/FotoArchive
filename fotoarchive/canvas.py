"""Photo-first cells: no permanent filenames or dates; bounded hover previews."""
from PySide6.QtCore import QEvent, QRect, QRectF, QPoint, QSize, Qt, QTimer, Signal, QVariantAnimation, QEasingCurve
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QLabel, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QWidget
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
    stackToggled = Signal(str, int)
    momentOpened = Signal(dict, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.edge = 220
        self.proportions = False
        self.full_frame = False

    def sizeHint(self, option, index):
        width = self.edge
        if self.proportions:
            w,h = index.model().geometry.get(index.row(), (4,3))
            width = round(self.edge * max(.45, min(2.5,w/max(1,h))))
        return QSize(width, self.edge)

    def badge(self, rect):
        return QRect(rect.left()+8, rect.top()+8, 64, 26)

    def moment_targets(self, rect, asset, metrics):
        moments = asset.get('matched_moments', [])
        if not moments:
            return []
        total = asset.get('moment_count', len(moments))
        shown = []
        x, right = rect.left()+7, rect.right()-7
        for moment in moments[:2]:
            title = timestamp_text(moment['timestamp_ms'])
            width = metrics.horizontalAdvance(title)+14
            reserve = metrics.horizontalAdvance('+'+str(total-len(shown)-1))+17 if total>len(shown)+1 else 0
            if x+width+reserve>right:
                break
            shown.append((QRect(x,rect.bottom()-54,width,22),title,moment))
            x += width+3
        remaining = total-len(shown)
        if remaining:
            shown.append((QRect(x,rect.bottom()-54,max(20,right-x),22),'+'+str(remaining),None))
        return shown

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
            scaled = pixmap.scaled(rect.size(), Qt.KeepAspectRatio if self.proportions or self.full_frame else Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
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
            for target,title,_ in self.moment_targets(rect,asset,option.fontMetrics):
                p.fillRect(target,QColor(14,81,75,235))
                p.drawText(target,Qt.AlignCenter,title)
        p.restore()

    def editorEvent(self, event, model, option, index):
        if event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
            asset = index.data(PhotoModel.AssetRole) or {}
            for rect,_,moment in self.moment_targets(option.rect.adjusted(2,2,-2,-2),asset,option.fontMetrics):
                if rect.contains(event.position().toPoint()):
                    self.momentOpened.emit(asset,moment)
                    return True
            if asset.get('stack_count',0) > 1 and self.badge(option.rect.adjusted(2,2,-2,-2)).contains(event.position().toPoint()):
                self.stackToggled.emit(asset['stack_key'], index.row())
                return True
        return super().editorEvent(event,model,option,index)


class StackMotion(QWidget):
    """One viewport of transient pixels; no photo decoding or archive-sized list."""
    def __init__(self, gallery, key, origin):
        super().__init__(gallery.viewport())
        self.gallery, self.key, self.origin = gallery, key, QRectF(origin)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setGeometry(gallery.viewport().rect())
        self.old = gallery.visible_cells()
        self.new = None
        self.progress = 0.0
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(190)
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.setEasingCurve(QEasingCurve.OutCubic)
        self.animation.valueChanged.connect(self.advance)
        self.animation.finished.connect(self.hide)
        self.expiry = QTimer(self)
        self.expiry.setSingleShot(True)
        self.expiry.timeout.connect(self.hide)
        self.expiry.start(4000)
        self.show()

    def advance(self, value):
        self.progress = value
        self.update()

    def finish(self):
        self.new = self.gallery.visible_cells()
        self.expiry.stop()
        self.animation.start()

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), self.gallery.palette().base())
        if self.new is None:
            for rect, pixmap, _ in self.old.values():
                p.drawPixmap(rect.toRect(), pixmap)
            return
        t = self.progress
        # The cover stays above the emerging members of its stack.
        keys = sorted(self.old.keys() | self.new.keys(),
                      key=lambda key: self.old.get(key, (None, None, None))[0] == self.origin)
        for key in keys:
            old, new = self.old.get(key), self.new.get(key)
            if new:
                end, pixmap, stack = new
                start = old[0] if old else self.origin if stack == self.key else end.translated(0, self.height())
                opacity = 1.0 if old else t
            else:
                start, pixmap, stack = old
                end = self.origin if stack == self.key else start.translated(0, self.height())
                opacity = 1.0-t
            rect = QRectF(start.x()+(end.x()-start.x())*t, start.y()+(end.y()-start.y())*t,
                          start.width()+(end.width()-start.width())*t, start.height()+(end.height()-start.height())*t)
            p.setOpacity(opacity)
            p.drawPixmap(rect.toRect(), pixmap)


class CanvasGallery(PhotoGallery):
    zoomRequested = Signal(int)
    hoverVideo = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.hover_asset = None
        self.frames = []
        self.frame_number = 0
        self.stack_motion = None
        self.hover_delay = QTimer(self)
        self.hover_delay.setSingleShot(True)
        self.hover_delay.timeout.connect(lambda: self.hoverVideo.emit(self.hover_asset) if self.hover_asset else None)
        self.hover_clock = QTimer(self)
        self.hover_clock.setInterval(650)
        self.hover_clock.timeout.connect(self.advance_hover)

    def wheelEvent(self, event):
        self.stop_stack_transition()
        if event.modifiers() & Qt.ControlModifier:
            self.zoomRequested.emit(16 if event.angleDelta().y()>0 else -16)
            event.accept()
            return
        self.stop_hover()
        super().wheelEvent(event)

    def visible_cells(self):
        indexes = set()
        edge = self.itemDelegate().edge
        for y in [*range(0, self.viewport().height(), max(1, edge//2)), self.viewport().height()-1]:
            for x in [*range(0, self.viewport().width(), max(1, edge//5)), self.viewport().width()-1]:
                index = self.indexAt(QPoint(x, y))
                if index.isValid():
                    indexes.add(index.row())
        cells = {}
        for row in sorted(indexes)[:120]:
            index = self.model().index(row)
            asset = index.data(PhotoModel.AssetRole)
            rect = self.visualRect(index)
            if not asset or not rect.isValid():
                continue
            option = QStyleOptionViewItem()
            option.initFrom(self)
            option.rect = QRect(QPoint(), rect.size())
            if self.selectionModel().isSelected(index):
                option.state |= QStyle.State_Selected
            pixmap = QPixmap(rect.size())
            pixmap.fill(self.palette().base().color())
            painter = QPainter(pixmap)
            self.itemDelegate().paint(painter, option, index)
            painter.end()
            cells[asset['id']] = (QRectF(rect), pixmap, asset.get('stack_key'))
        return cells

    def begin_stack_transition(self, key, rect):
        self.stop_stack_transition()
        self.stop_hover()
        self.stop_scroll()
        self.stack_motion = StackMotion(self, key, rect)

    def finish_stack_transition(self):
        if self.stack_motion and self.stack_motion.isVisible():
            self.stack_motion.finish()

    def stop_stack_transition(self):
        if self.stack_motion:
            self.stack_motion.hide()
            self.stack_motion.deleteLater()
            self.stack_motion = None

    def resizeEvent(self, event):
        self.stop_stack_transition()
        super().resizeEvent(event)

    def mousePressEvent(self, event):
        self.stop_stack_transition()
        super().mousePressEvent(event)

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
        self.stop_stack_transition()
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
