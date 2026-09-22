"""Local video playback starts at the moment found by semantic or face search."""
from PySide6.QtCore import Qt, QUrl, QRectF, QTimer
from PySide6.QtGui import QPainter, QColor
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QListWidget, QListWidgetItem

from .video import timestamp_text


class MomentSlider(QSlider):
    def __init__(self,moments,parent=None):
        super().__init__(Qt.Horizontal,parent)
        self.moments = moments

    def paintEvent(self,event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setBrush(QColor('#159e8f'))
        painter.setPen(Qt.NoPen)
        for moment in self.moments:
            x = 8+moment['timestamp_ms']/max(1,self.maximum())*(self.width()-16)
            painter.drawEllipse(QRectF(x-3,1,6,6))


class VideoPlayerDialog(QDialog):
    def __init__(self, asset, cfg, owner=None, backend=None):
        super().__init__(owner)
        self.asset, self.cfg, self.backend = asset, cfg, backend
        self.setWindowTitle(asset['filename'])
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, True)
        self.resize(1100, 800)
        layout = QVBoxLayout(self)
        self.video = QVideoWidget()
        layout.addWidget(self.video, 1)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.audio.setVolume(.7)
        controls = QHBoxLayout()
        self.toggle = QPushButton('Пауза')
        self.toggle.clicked.connect(lambda: self.player.pause() if self.player.playbackState() == QMediaPlayer.PlayingState else self.player.play())
        controls.addWidget(self.toggle)
        moments = asset.get('matched_moments',[])
        self.slider = MomentSlider(moments)
        self.slider.sliderMoved.connect(self.player.setPosition)
        controls.addWidget(self.slider, 1)
        self.clock = QLabel()
        controls.addWidget(self.clock)
        frame = QPushButton('Найденный кадр · лица')
        frame.clicked.connect(self.show_frame)
        controls.addWidget(frame)
        layout.addLayout(controls)
        if moments:
            moments_row = QHBoxLayout()
            moments_row.addWidget(QLabel('Моменты:'))
            for moment in moments:
                button = QPushButton(timestamp_text(moment['timestamp_ms']))
                button.clicked.connect(lambda checked=False,t=moment['timestamp_ms']:self.player.setPosition(t))
                moments_row.addWidget(button)
            remaining = asset.get('moment_count',len(moments))-len(moments)
            if remaining>0:
                more = QPushButton(f'Ещё {remaining}…')
                more.clicked.connect(self.more_moments)
                moments_row.addWidget(more)
            moments_row.addStretch()
            layout.addLayout(moments_row)
        self.message = QLabel('Поиск использует кадры через 10 секунд. Проверка условий относится к показанному кадру.')
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.player.durationChanged.connect(lambda duration: self.slider.setRange(0, duration))
        self.player.positionChanged.connect(self.position_changed)
        self.player.playbackStateChanged.connect(lambda state: self.toggle.setText('Пауза' if state == QMediaPlayer.PlayingState else 'Продолжить'))
        self.player.errorOccurred.connect(lambda error, text: self.message.setText('Не удалось воспроизвести видео: ' + text))
        self.pending_seek = int(asset.get('timestamp_ms') or 0)
        self.player.mediaStatusChanged.connect(self.media_ready)
        self.player.setSource(QUrl.fromLocalFile(asset['path']))
        self.player.play()

    def media_ready(self, status):
        if status == QMediaPlayer.LoadedMedia and self.pending_seek is not None:
            self.player.setPosition(self.pending_seek)
            self.pending_seek = None

    def more_moments(self):
        from uuid import uuid4
        if not self.backend:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('Моменты в найденном видео')
        dialog.resize(330,460)
        layout = QVBoxLayout(dialog)
        items = QListWidget()
        layout.addWidget(items)
        more = QPushButton('Загрузить ещё')
        layout.addWidget(more)
        serial = str(uuid4())
        state = {'offset':0}
        timeout = QTimer(dialog)
        timeout.setSingleShot(True)
        timeout.timeout.connect(lambda:(more.setText('Повторить загрузку'),more.setEnabled(True)))
        def request():
            more.setEnabled(False)
            more.setText('Загружаю…')
            timeout.start(10000)
            self.backend.send(action='match_moments',id=self.parentWidget().request_id,serial=serial,
                              asset_id=self.asset['id'],offset=state['offset'])
        def receive(event):
            if event['type']!='match_moments' or event.get('serial')!=serial:
                return
            timeout.stop()
            for row in event['items']:
                item = QListWidgetItem(timestamp_text(row['timestamp_ms']))
                item.setData(Qt.UserRole,row['timestamp_ms'])
                items.addItem(item)
            state['offset'] += len(event['items'])
            more.setEnabled(len(event['items'])==100)
            more.setText('Загрузить ещё' if len(event['items'])==100 else 'Все найденные моменты показаны')
        items.itemClicked.connect(lambda item:self.player.setPosition(item.data(Qt.UserRole)))
        more.clicked.connect(request)
        self.backend.event.connect(receive)
        request()
        dialog.exec()
        self.backend.event.disconnect(receive)
        dialog.deleteLater()

    def position_changed(self, position):
        if not self.slider.isSliderDown():
            self.slider.setValue(position)
        self.clock.setText(f'{timestamp_text(position)} / {timestamp_text(self.player.duration())}')

    def show_frame(self):
        from .ui import Viewer
        self.player.pause()
        viewer = Viewer([self.asset], 0, self.cfg, self, self.backend)
        owner = self.parentWidget()
        if owner and hasattr(owner, 'find_person'):
            viewer.personSelected.connect(lambda key: (self.accept(), owner.find_person(key)))
        viewer.exec()
        viewer.deleteLater()

    def done(self, result):
        self.player.stop()
        self.player.setSource(QUrl())
        super().done(result)
