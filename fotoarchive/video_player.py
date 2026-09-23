"""Local video playback starts at the moment found by semantic or face search."""
from PySide6.QtCore import Qt, QUrl, QRectF, QTimer, Signal
from PySide6.QtGui import QPainter, QColor
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QListWidget, QListWidgetItem, QScrollArea, QWidget, QStyle, QStyleOptionSlider, QSizePolicy

from .video import timestamp_text, asset_at_moment, search_coverage_text


class MomentSlider(QSlider):
    def __init__(self,moments,parent=None):
        super().__init__(Qt.Horizontal,parent)
        self.moments = moments

    def seek_at(self,point):
        option=QStyleOptionSlider();self.initStyleOption(option)
        groove=self.style().subControlRect(QStyle.CC_Slider,option,QStyle.SC_SliderGroove,self)
        handle=self.style().subControlRect(QStyle.CC_Slider,option,QStyle.SC_SliderHandle,self)
        value=QStyle.sliderValueFromPosition(self.minimum(),self.maximum(),
            round(point.x()-groove.x()-handle.width()/2),max(1,groove.width()-handle.width()),option.upsideDown)
        self.setSliderPosition(value)

    def mousePressEvent(self,event):
        if event.button()==Qt.LeftButton:
            self.setSliderDown(True)
            self.seek_at(event.position())
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self,event):
        if self.isSliderDown():
            self.seek_at(event.position());event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self,event):
        if event.button()==Qt.LeftButton and self.isSliderDown():
            self.seek_at(event.position());self.setSliderDown(False);event.accept()
        else:
            super().mouseReleaseEvent(event)

    def paintEvent(self,event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setBrush(QColor('#159e8f'))
        painter.setPen(Qt.NoPen)
        for moment in self.moments:
            x = 8+moment['timestamp_ms']/max(1,self.maximum())*(self.width()-16)
            painter.drawEllipse(QRectF(x-3,1,6,6))


class VideoPane(QWidget):
    """One reusable decoder surface inside the mixed-media viewer."""
    frameRequested = Signal()
    assetChanged = Signal(dict)

    def __init__(self, cfg, owner=None, backend=None):
        super().__init__(owner)
        self.asset, self.cfg, self.backend = None, cfg, backend
        self.active = False
        self.pending_seek = None
        self.moments_dialog = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        self.video = QVideoWidget()
        self.video.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Ignored)
        layout.addWidget(self.video, 1)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.audio.setVolume(.7)
        controls = self.controls_layout = QHBoxLayout()
        self.toggle = QPushButton('Пауза')
        self.toggle.clicked.connect(lambda: self.player.pause() if self.player.playbackState() == QMediaPlayer.PlayingState else self.player.play())
        controls.addWidget(self.toggle)
        self.slider = MomentSlider([])
        self.slider.sliderMoved.connect(self.player.setPosition)
        controls.addWidget(self.slider, 1)
        self.clock = QLabel()
        controls.addWidget(self.clock)
        frame = QPushButton('Найденный кадр · лица')
        frame.clicked.connect(self.frameRequested)
        controls.addWidget(frame)
        layout.addLayout(controls)
        self.moments_scroller = QScrollArea()
        self.moments_scroller.setWidgetResizable(True)
        self.moments_scroller.setFixedHeight(66)
        self.moments_scroller.setFrameShape(QScrollArea.NoFrame)
        layout.addWidget(self.moments_scroller)
        self.moments_scroller.hide()
        self.message = QLabel()
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.player.durationChanged.connect(lambda duration: self.slider.setRange(0, duration))
        self.player.positionChanged.connect(self.position_changed)
        self.player.playbackStateChanged.connect(lambda state: self.toggle.setText('Пауза' if state == QMediaPlayer.PlayingState else 'Продолжить'))
        self.player.errorOccurred.connect(self.playback_error)
        self.player.mediaStatusChanged.connect(self.media_ready)

    def load(self, asset):
        self.stop()
        self.asset = dict(asset)
        moments = asset.get('matched_moments',[])
        self.slider.moments = moments
        self.slider.setRange(0,0)
        self.clock.setText('00:00 / 00:00')
        old = self.moments_scroller.takeWidget()
        if old:
            old.deleteLater()
        if moments:
            row_widget = QWidget()
            moments_row = QHBoxLayout(row_widget)
            self.moments_scroller.setWidget(row_widget)
            moments_row.addWidget(QLabel('Моменты:'))
            for moment in moments:
                button = QPushButton(timestamp_text(moment['timestamp_ms']))
                button.clicked.connect(lambda checked=False,moment=moment:self.choose_moment(moment))
                moments_row.addWidget(button)
            remaining = asset.get('moment_count',len(moments))-len(moments)
            if remaining>0:
                more = QPushButton(f'Ещё {remaining}…')
                more.clicked.connect(self.more_moments)
                moments_row.addWidget(more)
            moments_row.addStretch()
        self.moments_scroller.setVisible(bool(moments))
        self.message.setText(search_coverage_text(asset))
        self.pending_seek = int(asset.get('timestamp_ms') or 0)
        self.active = True
        self.player.setSource(QUrl.fromLocalFile(asset['path']))
        self.player.play()

    def media_ready(self, status):
        if self.active and status == QMediaPlayer.LoadedMedia and self.pending_seek is not None:
            self.player.setPosition(self.pending_seek)
            self.pending_seek = None

    def playback_error(self,error,text):
        if self.active:
            self.message.setText('Не удалось воспроизвести видео: ' + text)

    def more_moments(self):
        from uuid import uuid4
        if not self.backend or not self.asset:
            return
        if self.moments_dialog:
            self.moments_dialog.raise_()
            return
        dialog = QDialog(self)
        self.moments_dialog = dialog
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.setWindowTitle('Моменты в найденном видео')
        dialog.resize(330,460)
        layout = QVBoxLayout(dialog)
        items = QListWidget()
        layout.addWidget(items)
        more = QPushButton('Загрузить ещё')
        layout.addWidget(more)
        serial = str(uuid4())
        asset_id = self.asset['id']
        owner = self.parentWidget()
        while owner and not hasattr(owner,'request_id'):
            owner = owner.parentWidget()
        request_id = owner.request_id if owner else 0
        state = {'offset':0}
        timeout = QTimer(dialog)
        timeout.setSingleShot(True)
        timeout.timeout.connect(lambda:(more.setText('Повторить загрузку'),more.setEnabled(True)))
        def request():
            more.setEnabled(False)
            more.setText('Загружаю…')
            timeout.start(10000)
            self.backend.send(action='match_moments',id=request_id,serial=serial,
                              asset_id=asset_id,offset=state['offset'])
        def receive(event):
            if event['type']!='match_moments' or event.get('serial')!=serial:
                return
            timeout.stop()
            for row in event['items']:
                item = QListWidgetItem(timestamp_text(row['timestamp_ms']))
                item.setData(Qt.UserRole,row)
                items.addItem(item)
            state['offset'] += len(event['items'])
            more.setEnabled(len(event['items'])==100)
            more.setText('Загрузить ещё' if len(event['items'])==100 else 'Все найденные моменты показаны')
        items.itemClicked.connect(lambda item:self.choose_moment(item.data(Qt.UserRole)))
        more.clicked.connect(request)
        self.backend.event.connect(receive)
        def finished(*_):
            timeout.stop()
            self.backend.event.disconnect(receive)
            self.moments_dialog = None
        dialog.finished.connect(finished)
        request()
        dialog.open()

    def choose_moment(self, moment):
        self.asset = asset_at_moment(self.asset,moment)
        self.assetChanged.emit(self.asset)
        self.pending_seek = moment['timestamp_ms'] if self.player.mediaStatus() != QMediaPlayer.LoadedMedia else None
        self.player.setPosition(moment['timestamp_ms'])

    def position_changed(self, position):
        if not self.slider.isSliderDown():
            self.slider.setValue(position)
        self.clock.setText(f'{timestamp_text(position)} / {timestamp_text(self.player.duration())}')

    def stop(self):
        self.active = False
        self.pending_seek = None
        if self.moments_dialog:
            self.moments_dialog.reject()
        if not self.player.source().isEmpty():
            self.player.stop()
            self.player.setSource(QUrl())


class VideoPlayerDialog(QDialog):
    """Standalone compatibility surface; catalogue browsing uses Viewer."""
    def __init__(self,asset,cfg,owner=None,backend=None):
        super().__init__(owner)
        self.cfg,self.backend = cfg,backend
        self.setWindowTitle(asset['filename'])
        self.setWindowFlag(Qt.WindowMaximizeButtonHint,True)
        self.resize(1200,820)
        layout=QVBoxLayout(self)
        self.pane=VideoPane(cfg,self,backend);layout.addWidget(self.pane)
        self.player,self.slider = self.pane.player,self.pane.slider
        self.audio = self.pane.audio
        from .telegram_share import ShareButton
        self.share_button=ShareButton(cfg,lambda:self.pane.asset,self)
        self.pane.controls_layout.addWidget(self.share_button)
        self.pane.frameRequested.connect(self.show_frame)
        self.pane.load(asset)

    @property
    def asset(self):
        return self.pane.asset

    def more_moments(self):
        self.pane.more_moments()

    def show_frame(self):
        from .ui import Viewer
        self.player.pause()
        viewer = Viewer([self.asset], 0, self.cfg, self, self.backend,frame_only=True)
        owner = self.parentWidget()
        if owner and hasattr(owner, 'find_person'):
            viewer.personSelected.connect(lambda key: (self.accept(), owner.find_person(key)))
        viewer.exec()
        viewer.deleteLater()

    def done(self, result):
        self.share_button.cancel()
        self.pane.stop()
        super().done(result)
