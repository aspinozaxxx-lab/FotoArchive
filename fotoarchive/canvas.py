"""Photo-first cells: no permanent filenames or dates; bounded hover previews."""
from PySide6.QtCore import QEvent, QRect, QRectF, QPoint, QSize, Qt, QTimer, Signal, QVariantAnimation, QEasingCurve
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
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

    def badge(self, rect, asset, metrics):
        width = 6 + 12 + 4 + metrics.horizontalAdvance(str(asset['stack_count'])) + 6
        return QRect(rect.left()+6, rect.top()+6, width, max(22, metrics.height()+4))

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
        outline = QPainterPath()
        outline.addRoundedRect(QRectF(rect), 4, 4)
        p.fillPath(outline, QColor('#25292c' if dark else '#e0e3e4'))
        pixmap = index.data(PhotoModel.PixmapRole)
        if pixmap:
            p.save()
            p.setClipPath(outline)
            scaled = pixmap.scaled(rect.size(), Qt.KeepAspectRatio if self.proportions or self.full_frame else Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            p.drawPixmap(rect.x()+(rect.width()-scaled.width())//2,rect.y()+(rect.height()-scaled.height())//2,scaled)
            p.restore()
        if selected:
            p.setPen(QPen(QColor('#159e8f'),3))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(rect.adjusted(1,1,-1,-1), 4, 4)
            p.fillRect(QRect(rect.right()-27,rect.top()+5,22,22),QColor('#147f72'))
            p.setPen(Qt.white)
            p.drawText(QRect(rect.right()-27,rect.top()+5,22,22),Qt.AlignCenter,'✓')
        if asset.get('stack_count',0) > 1:
            badge = self.badge(rect,asset,option.fontMetrics)
            p.setBrush(QColor(74,88,96,205))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(badge,4,4)
            from .icons import draw_symbol
            draw_symbol(p,'collapse' if asset.get('stack_expanded') else 'stack',
                        QRectF(badge.left()+6,badge.center().y()-6,12,12),QColor('white'))
            p.setPen(Qt.white)
            p.drawText(badge.adjusted(22,0,-6,0),Qt.AlignVCenter|Qt.AlignLeft,str(asset['stack_count']))
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
            if asset.get('stack_count',0) > 1 and self.badge(option.rect.adjusted(2,2,-2,-2),asset,option.fontMetrics).contains(event.position().toPoint()):
                self.stackToggled.emit(asset['stack_key'], index.row())
                return True
        return super().editorEvent(event,model,option,index)


class StackMotion(QWidget):
    """One viewport of transient pixels; no photo decoding or archive-sized list."""
    def __init__(self, gallery, key, origin):
        # A viewport child is moved by QWidget.scroll during model reset and
        # anchor restoration, briefly exposing the empty underlying layout.
        # A sibling stays above the viewport throughout that entire operation.
        super().__init__(gallery)
        self.gallery, self.key, self.origin = gallery, key, QRectF(origin)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setGeometry(gallery.viewport().geometry())
        # Preserve the actual styled viewport, including gutters. The palette's
        # Base colour is not the transparent list's background (in either theme).
        self.snapshot = gallery.viewport().grab()
        self.background = gallery.palette().window().color()
        self.background = self.snapshot.toImage().pixelColor(0, 0)
        self.old = gallery.visible_cells(snapshot=self.snapshot)
        self.new = None
        self.progress = 0.0
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(190)
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.setEasingCurve(QEasingCurve.OutCubic)
        self.animation.valueChanged.connect(self.advance)
        self.animation.finished.connect(self.hide)
        self.ready_timer = QTimer(self)
        self.ready_timer.setInterval(16)
        self.ready_timer.timeout.connect(self.finish)
        self.ready_attempts = 0
        self.show()
        self.raise_()

    def advance(self, value):
        self.progress = value
        self.update()

    def finish(self):
        if self.new is not None:
            return
        cells, ready = self.gallery.visible_cells(require_ready=True)
        self.ready_attempts += 1
        # A reset can finish its layout before the page or thumbnails arrive.
        # Keep the exact old pixels until the replacement is actually drawable.
        if not ready:
            self.ready_timer.start()
            return
        self.ready_timer.stop()
        self.new = cells
        self.animation.start()

    def paintEvent(self, event):
        p = QPainter(self)
        if self.new is None:
            p.drawPixmap(0, 0, self.snapshot)
            return
        p.fillRect(self.rect(), self.background)
        t = self.progress
        # The cover stays above the emerging members of its stack.
        keys = sorted(self.old.keys() | self.new.keys(),
                      key=lambda key: self.old.get(key, (None, None, None))[0] == self.origin)
        for key in keys:
            old, new = self.old.get(key), self.new.get(key)
            if new:
                end, pixmap, stack = new
                start = old[0] if old else self.origin if stack == self.key else end.translated(0, self.height())
                if old and t < 1:
                    pixmap = old[1]
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
        self._view_anchor = None
        self._reflow_anchor = None
        self._restoring_layout = False
        self._reflow_timer = QTimer(self)
        self._reflow_timer.setSingleShot(True)
        self._reflow_timer.timeout.connect(self.restore_view_position)
        self._resize_settle = QTimer(self)
        self._resize_settle.setSingleShot(True)
        self._resize_settle.setInterval(150)
        self._resize_settle.timeout.connect(self.finish_reflow)
        self.verticalScrollBar().sliderPressed.connect(self.cancel_reflow)
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
        self.cancel_reflow()
        self.stop_hover()
        super().wheelEvent(event)

    def visible_cells(self, require_ready=False, snapshot=None):
        indexes = set()
        edge = self.itemDelegate().edge
        for y in [*range(0, self.viewport().height(), max(1, edge//2)), self.viewport().height()-1]:
            for x in [*range(0, self.viewport().width(), max(1, edge//5)), self.viewport().width()-1]:
                index = self.indexAt(QPoint(x, y))
                if index.isValid():
                    indexes.add(index.row())
        cells = {}
        ready = True
        # Capture actual styled/DPI-scaled pixels, rather than rerendering each
        # card with a different font, palette or device pixel ratio.
        snapshot = snapshot if snapshot is not None else self.viewport().grab()
        dpr = snapshot.devicePixelRatio()
        for row in sorted(indexes):
            index = self.model().index(row)
            asset = index.data(PhotoModel.AssetRole)
            rect = self.visualRect(index)
            if not asset or not rect.isValid():
                ready = False
                continue
            if asset.get('thumbnail') and index.data(PhotoModel.PixmapRole) is None and asset['thumbnail'] not in self.model().failed:
                ready = False
            pixmap = QPixmap(round(rect.width()*dpr),round(rect.height()*dpr))
            pixmap.setDevicePixelRatio(dpr)
            pixmap.fill(Qt.transparent)
            painter = QPainter(pixmap)
            painter.drawPixmap(-rect.topLeft(),snapshot)
            painter.end()
            cells[asset['id']] = (QRectF(rect), pixmap, asset.get('stack_key'))
        return (cells, ready and bool(cells)) if require_ready else cells

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
        self.preserve_view_position()
        self.stop_stack_transition()
        super().resizeEvent(event)
        self.schedule_reflow()

    def event(self,event):
        changing = event.type() in (QEvent.ScreenChangeInternal,QEvent.DevicePixelRatioChange,
                                   QEvent.FontChange,QEvent.StyleChange)
        if changing and hasattr(self,'_reflow_timer'):
            self.preserve_view_position()
        result = super().event(event)
        if changing and hasattr(self,'_reflow_timer'):
            self.schedule_reflow()
        return result

    def setModel(self,model):
        previous = self.model()
        if previous is not None:
            previous.modelAboutToBeReset.disconnect(self.cancel_reflow)
        super().setModel(model)
        if model is not None:
            model.modelAboutToBeReset.connect(self.cancel_reflow)

    def remember_view_position(self):
        if self._reflow_anchor or not self.model() or not self.model().rowCount():
            return
        edge = self.itemDelegate().edge
        for y in (2,8,min(edge//2,self.viewport().height()-1)):
            for x in range(4,self.viewport().width(),max(4,edge//4)):
                index = self.indexAt(QPoint(x,y))
                if index.isValid():
                    rect = self.visualRect(index)
                    self._view_anchor = dict(row=index.row(),fraction=rect.y()/max(1,rect.height()))
                    return

    def preserve_view_position(self):
        if not self._reflow_anchor and self._view_anchor:
            self._reflow_anchor = dict(self._view_anchor)
            self.stop_scroll()
            self._append_range_floor = self.verticalScrollBar().maximum()

    def schedule_reflow(self):
        if self._reflow_anchor:
            self._reflow_timer.start(0)
            self._resize_settle.start()

    def restore_view_position(self):
        anchor = self._reflow_anchor
        if not anchor or not self.model() or not self.model().rowCount():
            return
        rect = self.visualRect(self.model().index(min(anchor['row'],self.model().rowCount()-1)))
        last = self.visualRect(self.model().index(self.model().rowCount()-1))
        if not rect.isValid() or not last.isValid():
            self._reflow_timer.start(16)
            return
        self._restoring_layout = True
        try:
            bar = self.verticalScrollBar()
            # The old range only protects the intermediate batches. Once all
            # rows have geometry it must be allowed to shrink as well as grow.
            self._append_range_floor = None
            super().updateGeometries()
            bar.setValue(bar.value()+rect.y()-round(anchor['fraction']*rect.height()))
        finally:
            self._restoring_layout = False

    def finish_reflow(self):
        self.restore_view_position()
        if self._reflow_timer.isActive():
            self._resize_settle.start()
            return
        self._reflow_anchor = None
        self.remember_view_position()

    def cancel_reflow(self):
        self._reflow_timer.stop()
        self._resize_settle.stop()
        self._reflow_anchor = self._view_anchor = None

    def scrollContentsBy(self,dx,dy):
        super().scrollContentsBy(dx,dy)
        if hasattr(self,'_reflow_anchor') and not self._restoring_layout:
            self.remember_view_position()

    def scrollTo(self,index,hint=PhotoGallery.EnsureVisible):
        # Explicit navigation takes precedence over a resize still settling.
        if not hasattr(self,'_reflow_timer'):
            return super().scrollTo(index,hint)
        self.cancel_reflow()
        super().scrollTo(index,hint)
        self.remember_view_position()

    def paintEvent(self,event):
        super().paintEvent(event)
        self.remember_view_position()

    def mousePressEvent(self, event):
        self.cancel_reflow()
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
        self.cancel_reflow()
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
