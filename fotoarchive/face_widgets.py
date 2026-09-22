from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QWidget, QGraphicsRectItem, QFrame, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea


class FacePreview(QWidget):
    personSelected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pixmap = QPixmap()
        self.faces = []
        self.show_boxes = True
        self.setMouseTracking(True)
        self.setMinimumHeight(200)
        self.setMaximumHeight(240)
        self.setToolTip("Нажмите на лицо для поиска. При активном поиске оно добавится к примерам выбранного человека.")

    def setShowBoxes(self, visible):
        self.show_boxes = visible
        self.update()

    def setPhoto(self, pixmap):
        self.pixmap, self.faces = pixmap, []
        self.update()

    def setFaces(self, faces):
        self.faces = faces
        self.update()

    def imageRect(self):
        if self.pixmap.isNull():
            return QRectF()
        size = self.pixmap.size().scaled(self.size(), Qt.KeepAspectRatio)
        return QRectF((self.width()-size.width())/2, (self.height()-size.height())/2, size.width(), size.height())

    def faceRect(self, face):
        rect = self.imageRect()
        x1, y1, x2, y2 = face["box"]
        return QRectF(rect.x()+x1*rect.width(), rect.y()+y1*rect.height(), (x2-x1)*rect.width(), (y2-y1)*rect.height())

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        if not self.pixmap.isNull():
            painter.drawPixmap(self.imageRect(), self.pixmap, QRectF(self.pixmap.rect()))
        painter.setPen(QPen(QColor("#48efbe"), 2))
        if self.show_boxes:
            for face in self.faces:
                painter.drawRect(self.faceRect(face))

    def mouseMoveEvent(self, event):
        hit = any(self.faceRect(face).contains(event.position()) for face in self.faces)
        self.setCursor(Qt.PointingHandCursor if hit else Qt.ArrowCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            for face in sorted(self.faces, key=lambda f: self.faceRect(f).width()):
                if self.faceRect(face).adjusted(-3, -3, 3, 3).contains(event.position()):
                    self.personSelected.emit(face["id"])
                    break


class FaceBox(QGraphicsRectItem):
    def __init__(self, face, width, height, callback, show_boxes=True):
        x1, y1, x2, y2 = face["box"]
        super().__init__(x1*width, y1*height, (x2-x1)*width, (y2-y1)*height)
        self.face_id, self.callback = face["id"], callback
        self.show_boxes = show_boxes
        pen = QPen(QColor("#48efbe"), 2)
        pen.setCosmetic(True)
        self.setPen(pen)
        self.setZValue(1)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Выбрать лицо для поиска или добавить к примерам человека")

    def setShowBoxes(self, visible):
        self.show_boxes = visible
        self.update()

    def shape(self):
        path = QPainterPath()
        path.addRect(self.rect())
        return path

    def paint(self, painter, option, widget=None):
        # Painting alone is optional. The item and its full hit area remain active.
        if self.show_boxes:
            super().paint(painter, option, widget)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            event.accept()
            self.callback(self.face_id)


class FaceFilterPanel(QFrame):
    removeExample = Signal(str)
    clearRequested = Signal()
    browseRequested = Signal()
    refineRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        heading = QHBoxLayout()
        self.title = QLabel()
        self.title.setObjectName("section")
        heading.addWidget(self.title, 1)
        self.refine_button = QPushButton("Уточнить фильтр")
        self.refine_button.setObjectName("primary")
        self.refine_button.clicked.connect(self.refineRequested)
        heading.addWidget(self.refine_button)
        self.clear_button = QPushButton("Сбросить поиск по человеку")
        self.clear_button.clicked.connect(self.clearRequested)
        heading.addWidget(self.clear_button)
        layout.addLayout(heading)
        body = QHBoxLayout()
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        self.scroll.setFixedHeight(82)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.cards = QWidget()
        self.card_layout = QHBoxLayout(self.cards)
        self.card_layout.setContentsMargins(0, 0, 0, 0)
        self.card_layout.setAlignment(Qt.AlignLeft)
        self.scroll.setWidget(self.cards)
        body.addWidget(self.scroll, 1)
        controls = QVBoxLayout()
        self.browse_button = QPushButton("Добавить пример из каталога")
        self.browse_button.clicked.connect(self.browseRequested)
        controls.addWidget(self.browse_button)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setObjectName("muted")
        self.hint.setMinimumWidth(325)
        self.hint.setMaximumWidth(390)
        controls.addWidget(self.hint)
        body.addLayout(controls)
        layout.addLayout(body)
        self.remove_buttons = {}
        self.setVisible(False)

    def setExamples(self, examples, picking=False, invalid=()):
        self.setVisible(bool(examples))
        while self.card_layout.count():
            item = self.card_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.remove_buttons.clear()
        for number, face in enumerate(examples, 1):
            card = QWidget()
            card.setFixedWidth(78)
            row = QHBoxLayout(card)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(2)
            portrait = QLabel()
            portrait.setFixedSize(54, 54)
            portrait.setAlignment(Qt.AlignCenter)
            portrait.setStyleSheet("border: 1px solid #d5dfe3; border-radius: 5px; background: #eef3f2;")
            pixmap = QPixmap(face.get("thumbnail") or "")
            if face["id"] in invalid:
                portrait.setText("Устарел")
                portrait.setStyleSheet("border: 2px solid #bf755b; border-radius: 5px; color: #984426;")
            elif not pixmap.isNull():
                portrait.setPixmap(pixmap.scaled(52, 52, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            else:
                portrait.setText(str(number))
            portrait.setToolTip(face.get("relative_path") or f"Пример {number}")
            row.addWidget(portrait)
            remove = QPushButton("×")
            remove.setFixedSize(22, 25)
            remove.setStyleSheet("padding: 0;")
            remove.setToolTip(f"Удалить пример {number}")
            remove.setAccessibleName(f"Удалить пример лица {number}")
            remove.clicked.connect(lambda checked=False, key=face["id"]: self.removeExample.emit(key))
            self.remove_buttons[face["id"]] = remove
            row.addWidget(remove)
            self.card_layout.addWidget(card)
        self.title.setText(("Выбор дополнительного лица" if picking else "Поиск по человеку") + f" · примеров: {len(examples)}")
        self.browse_button.setText("Вернуться к результатам" if picking else "Добавить пример из каталога")
        self.hint.setText("Выберите фото и нажмите на лицо того же человека. Остальные фильтры продолжают действовать." if picking else
                          "Добавляйте ракурсы одного человека. Совпадения по всем примерам объединяются.")
