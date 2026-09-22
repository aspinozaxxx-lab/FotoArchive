from __future__ import annotations

import html
import json
import multiprocessing as mp
import os
import queue
import subprocess
from collections import OrderedDict
from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import (QAbstractListModel, QModelIndex, QObject, QPoint, QRunnable, QSize, Qt, QThreadPool, QTimer, Signal)
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen, QPixmap, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDateEdit, QDialog, QDoubleSpinBox,
    QFileDialog, QFormLayout, QFrame, QGraphicsPixmapItem, QGraphicsScene, QGraphicsView, QHBoxLayout,
    QLabel, QLayout, QLineEdit, QListView, QMainWindow, QMessageBox, QProgressBar, QPushButton, QScrollArea,
    QSpinBox, QSplitter, QStyledItemDelegate, QTextBrowser, QVBoxLayout, QWidget, QSlider, QButtonGroup)

from .catalog import Filters
from .config import Settings
from .engine import worker_main
from .media import PreviewCache
from .gallery import PhotoModel
from .smooth_scroll import PhotoGallery
from .date_slider import DateRangeSlider
from .face_widgets import FacePreview, FaceBox, FaceFilterPanel
from .preferences import Preferences
from .video import timestamp_text


STYLE = """
QWidget { font-family: 'Segoe UI'; font-size: 10pt; color: #24323c; }
QMainWindow, QDialog { background: #f4f6f8; }
QFrame#panel { background: white; border: 1px solid #e1e6ea; border-radius: 10px; }
QLabel#title { font-size: 23pt; font-weight: 700; color: #133d38; }
QLabel#muted { color: #74828b; }
QLabel#section { font-weight: 600; color: #233d3a; }
QLineEdit, QComboBox, QDateEdit, QSpinBox, QDoubleSpinBox { background: white; border: 1px solid #d5dfe3; border-radius: 6px; padding: 6px; min-height: 19px; }
QPushButton { background: #fff; border: 1px solid #d5dfe3; border-radius: 6px; padding: 7px 12px; }
QPushButton:hover { background: #eef5f3; border-color: #84b1a9; }
QPushButton:disabled { color: #a5afb4; background: #f1f3f5; }
QPushButton#primary { color: white; background: #177c70; border-color: #177c70; font-weight: 600; }
QPushButton#primary:hover { background: #116459; }
QListView { background: transparent; border: none; outline: 0; }
QProgressBar { border: 0; background: #e7edee; border-radius: 3px; height: 6px; max-height: 6px; }
QProgressBar::chunk { background: #389c8a; border-radius: 3px; }
QTextBrowser { border: none; background: transparent; }
QSplitter::handle { background: transparent; width: 10px; }
QToolTip { color: #24323c; background: #fff; border: 1px solid #d5dfe3; }
QScrollBar:vertical { background: #edf1f3; width: 10px; border: none; margin: 0; }
QScrollBar::handle:vertical { background: #bfccd0; border-radius: 5px; min-height: 35px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
"""


class Backend(QObject):
    event = Signal(dict)

    def __init__(self, cfg):
        super().__init__()
        context = mp.get_context("spawn")
        self.commands = context.Queue(maxsize=64)
        self.events = context.Queue(maxsize=256)
        self.shutdown = context.Event()
        self.interactive_state = context.Array('q', [0, 0], lock=False)
        from .browse_reader import BrowseReader
        self.reader = BrowseReader(cfg, self.event.emit)
        from .library import LibraryReader
        self.library = LibraryReader(cfg, self.event.emit)
        self.browse_id = None
        self.process = context.Process(target=worker_main, args=(str(cfg.data_dir), self.commands, self.events, self.shutdown, self.interactive_state), daemon=False)
        self.process.start()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(80)
        self.dead_reported = False

    def send(self, **message):
        from .interactive import SEARCH_ACTIONS
        action = message['action']
        if action.startswith('library_'):
            self.library.send(message)
            return
        if action in SEARCH_ACTIONS:
            self.interactive_state[0] = message['id']
            self.browse_id = message['id'] if action == 'browse' else None
            self.reader.replace(message if action == 'browse' else None)
            if action == 'browse':
                # Cancel checks in the GPU owner while the list loads separately.
                message = {'action': 'clear_search', 'id': message['id']}
        elif action == 'search_page' and message.get('id') == self.browse_id:
            self.reader.page(message)
            return
        elif action == 'faces':
            self.interactive_state[1] += 1
            message.update(selection_serial=self.interactive_state[1], request_id=self.interactive_state[0])
        try:
            self.commands.put_nowait(message)
        except queue.Full:
            self.event.emit({"type": "error", "message": "Очередь занята. Дождитесь текущей операции."})

    def poll(self):
        for _ in range(100):
            try:
                event = self.events.get_nowait()
                if event['type'] == 'ready':
                    self.reader.enable()
                    self.library.enable()
                self.event.emit(event)
            except queue.Empty:
                break
        if not self.process.is_alive() and not self.dead_reported:
            self.dead_reported = True
            self.event.emit({"type": "fatal", "message": "Фоновый процесс остановился. Результаты сохранены; перезапустите приложение."})

    def close(self):
        self.timer.stop()
        self.reader.close()
        self.library.close()
        self.shutdown.set()
        # The window closes immediately; the owner process finishes its current file and exits safely.


from .canvas import PhotoDelegate, CanvasGallery, ElideLabel
from .workspace import Workspace


class ZoomView(QGraphicsView):
    zoomed = Signal()

    def wheelEvent(self, event):
        factor = 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
        current = self.transform().m11()
        if 0.05 < current * factor < 12:
            self.scale(factor, factor)
            self.zoomed.emit()
        event.accept()


class PreviewSignals(QObject):
    ready = Signal(int, QImage, str, bool)


class PreviewTask(QRunnable):
    def __init__(self, token, asset, cfg, original, signals):
        super().__init__()
        self.token, self.asset, self.cfg, self.original, self.signals = token, asset, cfg, original, signals

    def run(self):
        try:
            if self.original:
                from .media import open_rgb
                if self.asset.get('media_kind') == 'video':
                    from .video import frame_at
                    image = frame_at(Path(self.asset['path']), self.asset.get('timestamp_ms', 0), max_side=16384)[0]
                else:
                    image = open_rgb(Path(self.asset["path"]), full_resolution=True)
                data = image.tobytes()
                result = QImage(data, image.width, image.height, image.width * 3, QImage.Format_RGB888).copy()
            else:
                path = PreviewCache(self.cfg.data_dir / "previews", self.cfg.preview_budget).get(self.asset)
                result = QImage(str(path))
            self.signals.ready.emit(self.token, result, "", self.original)
        except Exception as exc:
            self.signals.ready.emit(self.token, QImage(), str(exc), self.original)


class Viewer(QDialog):
    personSelected = Signal(str)

    def __init__(self, items, position, cfg, parent=None, backend=None):
        super().__init__(parent)
        self.items, self.position, self.cfg = items, position, cfg
        self.backend = backend
        self.asset = None
        self.faces = []
        self.box_owner = parent
        while self.box_owner and not hasattr(self.box_owner, "faceBoxesChanged"):
            self.box_owner = self.box_owner.parentWidget()
        self.show_face_boxes = self.box_owner.show_face_boxes if self.box_owner else Preferences(cfg.data_dir).show_face_boxes
        self.finished_cleanup = False
        self.fit_mode = True
        self.waiting = False
        if backend:
            backend.event.connect(self.on_event)
        if isinstance(items, PhotoModel):
            items.assetsReady.connect(self.assets_ready)
        self.load_token = 0
        self.original_loaded = False
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.preview_signals = PreviewSignals()
        self.preview_signals.ready.connect(self.loaded)
        self.setWindowTitle("Просмотр фотографии")
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, True)
        self.resize(1200, 820)
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        for title, fn in [("← Предыдущая", lambda: self.navigate(-1)), ("Следующая →", lambda: self.navigate(1)), ("Вписать", self.fit), ("100%", self.actual), ("Оригинал", self.open_original)]:
            button = QPushButton(title)
            button.clicked.connect(fn)
            top.addWidget(button)
        self.label = QLabel()
        self.label.setTextFormat(Qt.PlainText)
        top.addWidget(self.label, 1)
        self.face_boxes_check = QCheckBox("Рамки лиц")
        self.face_boxes_check.setChecked(self.show_face_boxes)
        self.face_boxes_check.setToolTip("Скрытые рамки остаются доступными для нажатия на лицо")
        self.face_boxes_check.toggled.connect(self.toggle_face_boxes)
        top.addWidget(self.face_boxes_check)
        if self.box_owner:
            self.box_owner.faceBoxesChanged.connect(self.apply_face_boxes)
        layout.addLayout(top)
        self.scene = QGraphicsScene(self)
        self.view = ZoomView(self.scene)
        self.view.zoomed.connect(self.manual_zoom)
        self.view.setBackgroundBrush(QColor("#17232a"))
        self.view.setDragMode(QGraphicsView.ScrollHandDrag)
        layout.addWidget(self.view)
        hint = QLabel("Прокрутка — увеличение · перетаскивание — перемещение · нажатие на лицо — поиск или ещё один пример выбранного человека")
        hint.setObjectName("muted")
        layout.addWidget(hint)
        QShortcut(QKeySequence(Qt.Key_Left), self, activated=lambda: self.navigate(-1))
        QShortcut(QKeySequence(Qt.Key_Right), self, activated=lambda: self.navigate(1))
        self.load()

    def load(self, original=False):
        self.asset = self.items.asset(self.position) if isinstance(self.items, PhotoModel) else self.items[self.position]
        self.scene.clear()
        self.load_token += 1
        self.original_loaded = False
        self.pool.clear()
        self.faces = []
        if not self.asset:
            self.waiting = True
            self.label.setText("Загрузка следующей части каталога…")
            return
        self.waiting = False
        self.label.setText(f"Загрузка · {self.asset['filename']}")
        self.pool.start(PreviewTask(self.load_token, self.asset, self.cfg, original, self.preview_signals))
        if self.backend:
            self.backend.send(action="faces", asset_id=self.asset["id"], unit_id=self.asset.get('unit_id'))

    def assets_ready(self, start, end):
        if self.waiting and start <= self.position <= end:
            self.load()

    def on_event(self, event):
        if event["type"] == "face_list" and self.asset and (event["asset_id"], event["version"]) == (self.asset["id"], self.asset["version"]):
            if event.get('unit_id') != self.asset.get('unit_id'):
                return
            self.faces = event["faces"]
            self.draw_faces()

    def draw_faces(self):
        for item in self.scene.items():
            if isinstance(item, FaceBox):
                self.scene.removeItem(item)
        if not any(isinstance(item, QGraphicsPixmapItem) for item in self.scene.items()):
            return
        rect = self.scene.sceneRect()
        for face in self.faces:
            self.scene.addItem(FaceBox(face, rect.width(), rect.height(), self.choose_person, self.show_face_boxes))

    def toggle_face_boxes(self, visible):
        if self.box_owner:
            self.box_owner.set_face_boxes(visible)
        else:
            self.apply_face_boxes(visible)

    def apply_face_boxes(self, visible):
        self.show_face_boxes = visible
        self.face_boxes_check.blockSignals(True)
        self.face_boxes_check.setChecked(visible)
        self.face_boxes_check.blockSignals(False)
        for item in self.scene.items():
            if isinstance(item, FaceBox):
                item.setShowBoxes(visible)

    def choose_person(self, face_id):
        self.accept()
        self.personSelected.emit(face_id)

    def loaded(self, token, image, error, original):
        if token != self.load_token:
            return
        if error:
            self.label.setText(f"Оригинал недоступен: {error}")
            return
        self.original_loaded = original
        self.label.setText(f"{self.position + 1} · {self.asset['filename']}")
        if self.asset.get('media_kind') == 'video':
            self.label.setText(self.asset['filename'] + ' · кадр ' + timestamp_text(self.asset.get('timestamp_ms')))
        self.scene.addPixmap(QPixmap.fromImage(image))
        self.scene.setSceneRect(0, 0, image.width(), image.height())
        self.draw_faces()
        if original:
            self.view.resetTransform()
        else:
            self.fit()

    def navigate(self, step):
        position = self.position + step
        count = self.items.rowCount() if isinstance(self.items, PhotoModel) else len(self.items)
        if 0 <= position < count:
            self.position = position
            self.load()
        elif position == count and isinstance(self.items, PhotoModel) and self.items.canFetchMore():
            self.position = position
            self.load()
            self.items.fetchMore()

    def fit(self):
        self.fit_mode = True
        self.view.fitInView(self.scene.sceneRect(), Qt.KeepAspectRatio)

    def manual_zoom(self):
        self.fit_mode = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Wait for the layout to resize the viewport; manual zoom stays intact.
        if hasattr(self, "view") and self.fit_mode:
            QTimer.singleShot(0, self.refit)

    def refit(self):
        if self.fit_mode and not self.finished_cleanup and not self.scene.sceneRect().isEmpty():
            self.fit()

    def actual(self):
        self.fit_mode = False
        if self.original_loaded:
            self.view.resetTransform()
        else:
            self.load(original=True)

    def open_original(self):
        if not self.asset:
            return
        try:
            open_file(self.asset["path"])
        except OSError as exc:
            self.label.setText(str(exc))

    def done(self, result):
        if not self.finished_cleanup:
            self.finished_cleanup = True
            self.pool.clear()
            self.pool.waitForDone(3000)
            if self.backend:
                self.backend.event.disconnect(self.on_event)
            if isinstance(self.items, PhotoModel):
                self.items.assetsReady.disconnect(self.assets_ready)
            if self.box_owner:
                self.box_owner.faceBoxesChanged.disconnect(self.apply_face_boxes)
        super().done(result)


def open_file(path):
    if not Path(path).is_file():
        raise FileNotFoundError("Оригинал недоступен. Подключите диск с архивом.")
    os.startfile(path)


class MainWindow(Workspace, QMainWindow):
    faceBoxesChanged = Signal(bool)

    def __init__(self, cfg: Settings, backend=None):
        super().__init__()
        self.cfg = cfg
        self.preferences = Preferences(cfg.data_dir)
        self.workspace_init()
        self.show_face_boxes = self.preferences.show_face_boxes
        self.face_examples = OrderedDict((key, {"id": key}) for key in self.preferences.face_ids)
        self.face_rejected = self.preferences.rejected_faces - set(self.face_examples) if self.face_examples else set()
        self.face_skipped = set()
        self.face_cache = OrderedDict()
        self.face_picking = False
        self.invalid_faces = set()
        self.backend = backend or Backend(cfg)
        self.backend.event.connect(self.on_event)
        self.request_id = 0
        self.view_id = 0
        self.reference = None
        self.reference_unit = None
        self.mode = "browse"
        self.loading = False
        self.conditions = []
        self.offset = 0
        self.total = 0
        self.page_total = 0
        self.paused = True
        self.semantic = False
        self.selected_asset = None
        self.orientation_stats = {}
        self.last_metadata_count = -1
        self.result_metadata_count = -1
        self.latest_stats = {}
        self.source_inventory = {'phase': 'counting', 'total': None, 'counts': {},
                                 'root': str(cfg.root), 'includes': list(cfg.includes)}
        self.setWindowTitle("FotoArchive — личная фототека")
        self.resize(1480, 940)
        self.setMinimumSize(1060, 680)
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(10, 8, 10, 6)
        outer.setSpacing(5)
        header = QHBoxLayout()
        brand = QVBoxLayout()
        title = QLabel("FotoArchive")
        title.setStyleSheet('font-size:13pt; font-weight:600;')
        brand.addWidget(title)
        subtitle = QLabel("Локальный каталог фотографий")
        subtitle.setObjectName("muted")
        subtitle.hide()
        header.addLayout(brand)
        self.back_button = QPushButton('←')
        self.forward_button = QPushButton('→')
        self.back_button.setToolTip('Назад · Alt+←')
        self.forward_button.setToolTip('Вперёд · Alt+→')
        header.addWidget(self.back_button)
        header.addWidget(self.forward_button)
        header.addStretch()
        self.summary = QLabel("Открываю каталог…")
        self.summary.hide()
        add = QPushButton("Добавить папки")
        add.clicked.connect(self.add_folder)
        add.hide()
        self.rotation_button = QPushButton('Поворот фото')
        self.rotation_button.clicked.connect(self.orientation_dialog)
        self.rotation_button.hide()
        models = QPushButton("Модели")
        models.clicked.connect(self.models_dialog)
        models.hide()
        self.sidebar_toggle = QPushButton('Папки')
        self.details_toggle = QPushButton('Сведения')
        for button in (self.sidebar_toggle,self.details_toggle):
            button.setCheckable(True)
            header.addWidget(button)
        self.library_menu = QPushButton('Библиотека ▾')
        header.addWidget(self.library_menu)
        outer.addLayout(header)
        search_row = QHBoxLayout()
        self.search_box = QLineEdit()
        self.search_box.setObjectName("searchBox")
        self.search_box.setPlaceholderText("Например: люди у реки, соревнования в зале, три человека без автомобиля…")
        self.search_box.setMinimumHeight(32)
        self.search_box.returnPressed.connect(self.search)
        self.search_box.textEdited.connect(self.edit_query)
        search_row.addWidget(self.search_box, 1)
        self.complex_check = QCheckBox("Проверять условия на фото")
        self.complex_check.setToolTip("Дополнительная проверка количества, действий и исключений. Результаты появляются постепенно.")
        search_row.addWidget(self.complex_check)
        find = QPushButton("Найти")
        find.setObjectName("primary")
        find.clicked.connect(self.search)
        search_row.addWidget(find)
        clear = QPushButton("Сбросить поиск")
        clear.setToolTip("Снять поиск по человеку и текстовый запрос. Папка и период сохранятся.")
        clear.clicked.connect(self.clear_query)
        search_row.addWidget(clear)
        outer.addLayout(search_row)
        self.message = ElideLabel("Визуальные эмбеддинги, описания и поиск по лицам. Всё обрабатывается локально.")
        self.message.setFixedHeight(22)
        self.message.setObjectName("muted")
        self.message.setTextFormat(Qt.PlainText)
        outer.addWidget(self.message)
        self.face_panel = FaceFilterPanel()
        self.face_panel.clearRequested.connect(self.clear_person)
        self.face_panel.removeExample.connect(self.remove_face_example)
        self.face_panel.browseRequested.connect(self.toggle_face_picker)
        self.face_panel.refineRequested.connect(self.refine_person)
        outer.addWidget(self.face_panel)
        self.splitter = QSplitter()
        outer.addWidget(self.splitter, 1)
        self.build_filters()
        self.build_gallery()
        self.build_details()
        self.splitter.setSizes([244, 850, 330])
        main_outer = outer
        self.processing_panel = QWidget()
        main_outer.addWidget(self.processing_panel)
        outer = QVBoxLayout(self.processing_panel)
        outer.setContentsMargins(0,0,0,0)
        footer = QHBoxLayout()
        self.pipeline_label = QLabel('')
        self.pipeline_label.setObjectName('muted')
        self.pipeline_label.setWordWrap(True)
        outer.addWidget(self.pipeline_label)
        source_row = QHBoxLayout()
        self.source_label = QLabel('Подсчитываю состав добавленных папок…')
        self.source_label.setObjectName('muted')
        self.source_label.setWordWrap(True)
        self.source_label.setTextFormat(Qt.PlainText)
        source_row.addWidget(self.source_label, 1)
        self.check_updates_button = QPushButton('Проверить обновления')
        self.check_updates_button.setToolTip('Найти новые, заменённые и удалённые файлы в подключённых папках. '
                                            'Неизменённые снимки сохраняют готовые результаты обработки.')
        self.check_updates_button.clicked.connect(self.check_updates)
        source_row.addWidget(self.check_updates_button)
        source_details = QPushButton('Состав каталога')
        source_details.clicked.connect(self.show_source_details)
        source_row.addWidget(source_details)
        outer.addLayout(source_row)
        self.updates_label = QLabel()
        self.updates_label.setTextFormat(Qt.PlainText)
        self.updates_label.setWordWrap(True)
        self.updates_label.setObjectName('muted')
        self.updates_label.hide()
        outer.addWidget(self.updates_label)
        self.stage_label = ElideLabel("Каталог готов")
        self.stage_label.setTextFormat(Qt.PlainText)
        footer.addWidget(self.stage_label, 1)
        self.error_button = QPushButton("Ошибки: 0")
        self.error_button.clicked.connect(lambda: self.backend.send(action="errors"))
        footer.addWidget(self.error_button)
        self.pause_button = QPushButton("Продолжить индексацию")
        self.pause_button.clicked.connect(self.toggle_pause)
        footer.addWidget(self.pause_button)
        outer.addLayout(footer)
        self.progress_bars = {}
        progress_row = QHBoxLayout()
        for key, label in [("metadata", "Каталог"), ("embeddings", "Поиск"), ("captions", "Описания"), ("faces", "Лица"), ("locations", "Места")]:
            box = QVBoxLayout()
            text = QLabel(label)
            text.setObjectName("muted")
            bar = QProgressBar()
            bar.setTextVisible(False)
            box.addWidget(text)
            box.addWidget(bar)
            progress_row.addLayout(box)
            self.progress_bars[key] = (text, bar, label)
        outer.addLayout(progress_row)
        compact_footer = QHBoxLayout()
        self.compact_status = ElideLabel('Открываю каталог…')
        compact_footer.addWidget(self.compact_status,1)
        self.processing_toggle = QPushButton('Обработка ▾')
        self.processing_toggle.setCheckable(True)
        compact_footer.addWidget(self.processing_toggle)
        main_outer.addLayout(compact_footer)
        self.install_workspace()
        self.update_face_panel()
        self.rotation_refresh = QTimer(self)
        self.rotation_refresh.setSingleShot(True)
        self.rotation_refresh.timeout.connect(self.search)
        if self.reference or self.search_box.text().strip():
            self.search()
        elif self.face_examples and not self.preferences.values.get('browser_state'):
            self.activate_person_search()
        else:
            self.backend.send(action="browse", id=self.request_id, filters=self.filters(),presentation=self.presentation_options(),
                              **({'refresh_anchor':self._pending_anchor} if self._pending_anchor else {}))
            self._pending_anchor = None

    def build_filters(self):
        panel = QFrame()
        self.sidebar = panel
        panel.setObjectName("panel")
        panel.setMinimumWidth(220)
        panel.setMaximumWidth(360)
        layout = QVBoxLayout(panel)
        self.sidebar_layout = layout
        layout.setContentsMargins(8,8,8,8)
        title = QLabel("Архив")
        title.setObjectName("section")
        layout.addWidget(title)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        form = QFormLayout(content)
        form.setRowWrapPolicy(QFormLayout.WrapAllRows)
        self.folder_combo = QComboBox()
        self.folder_combo.addItem("Все выбранные папки", "")
        self.folder_combo.setParent(panel)
        self.folder_combo.hide()
        self.media_combo = QComboBox()
        for label, value in [('Фото и видео', ''), ('Только фото', 'photo'), ('Только видео', 'video')]:
            self.media_combo.addItem(label, value)
        self.media_combo.setParent(panel)
        self.media_combo.hide()
        self.media_combo.currentIndexChanged.connect(lambda _:self.search() if not self._restoring_controls else None)
        self.date_enabled = QCheckBox("Период съёмки · EXIF")
        form.addRow(self.date_enabled)
        from PySide6.QtCore import QDate
        self.date_from = QDateEdit(QDate(2003, 1, 1))
        self.date_to = QDateEdit(QDate(2003, 12, 31))
        for item in (self.date_from, self.date_to):
            item.setCalendarPopup(True)
            item.setDisplayFormat("dd.MM.yyyy")
        self.date_slider = DateRangeSlider()
        self.date_slider.rangeChanged.connect(self.slider_dates)
        self.date_slider.rangeCommitted.connect(self.commit_dates)
        self.date_from.dateChanged.connect(self.edit_dates)
        self.date_to.dateChanged.connect(self.edit_dates)
        form.addRow(self.date_slider)
        form.addRow("Начало", self.date_from)
        form.addRow("Конец", self.date_to)
        self.slider_dates(self.date_slider.lower, self.date_slider.upper)
        self.unknown = QCheckBox("Без даты съёмки")
        form.addRow(self.unknown)
        self.unknown.toggled.connect(lambda checked: self.date_enabled.setChecked(False) if checked else None)
        self.date_enabled.toggled.connect(lambda checked: self.unknown.setChecked(False) if checked else None)
        self.format_combo = QComboBox()
        self.format_combo.addItem("Все форматы", "")
        form.addRow("Формат", self.format_combo)
        self.camera_combo = QComboBox()
        self.camera_combo.addItem("Все камеры", "")
        form.addRow("Камера", self.camera_combo)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        apply = QPushButton("Применить фильтры")
        apply.setObjectName("primary")
        apply.clicked.connect(self.search)
        apply.hide()
        reset = QPushButton("Сбросить фильтры")
        reset.clicked.connect(self.reset_filters)
        layout.addWidget(reset)
        self.splitter.addWidget(panel)

    def build_gallery(self):
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        self.breadcrumbs = ElideLabel('Весь архив')
        layout.addWidget(self.breadcrumbs)
        controls = QHBoxLayout()
        self.media_buttons = {}
        self.media_group = QButtonGroup(self)
        for number,(key,label) in enumerate([('','Все'),('photo','Фото'),('video','Видео')]):
            button = QPushButton(label)
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False,n=number:self.media_combo.setCurrentIndex(n))
            self.media_group.addButton(button)
            self.media_buttons[key] = button
            controls.addWidget(button)
        controls.addStretch()
        self.sort_combo = QComboBox()
        for label,key in [('Сначала новые','newest'),('Сначала старые','oldest'),('По имени','name'),('По добавлению','added')]:
            self.sort_combo.addItem(label,key)
        controls.addWidget(self.sort_combo)
        self.stacks_check = QCheckBox('Стопки')
        self.stacks_check.setToolTip('Версии и серии показываются одной обложкой. Каждый кадр участвует в поиске.')
        controls.addWidget(self.stacks_check)
        self.map_toggle = QPushButton('Карта')
        self.map_toggle.setCheckable(True)
        controls.addWidget(self.map_toggle)
        layout.addLayout(controls)
        chip_scroll = QScrollArea()
        chip_scroll.setWidgetResizable(True)
        chip_scroll.setFrameShape(QFrame.NoFrame)
        chip_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        chip_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        chip_scroll.setFixedHeight(47)
        self.chips_panel = chip_scroll
        chip_contents = QWidget()
        self.chips_layout = QHBoxLayout(chip_contents)
        self.chips_layout.setContentsMargins(0,0,0,0)
        chip_scroll.setWidget(chip_contents)
        layout.addWidget(chip_scroll)
        top = QHBoxLayout()
        self.result_label = QLabel("Фотографии")
        self.result_label.setObjectName("section")
        top.addWidget(self.result_label, 1)
        self.refresh_results_button = QPushButton("Показать новые фото")
        self.refresh_results_button.setToolTip("Показать актуальный список с текущими фильтрами. Сканирование папок не запускается.")
        self.refresh_results_button.clicked.connect(self.search)
        policy = self.refresh_results_button.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.refresh_results_button.setSizePolicy(policy)
        self.refresh_results_button.hide()
        top.addWidget(self.refresh_results_button)
        self.verdict_combo = QComboBox()
        for title, value in [("Все кандидаты", ""), ("Подходят", "yes"), ("Неясно", "uncertain"), ("Не подходят", "no"), ("Ещё не проверены", "pending")]:
            self.verdict_combo.addItem(title, value)
        self.verdict_combo.currentIndexChanged.connect(self.change_verdict)
        self.verdict_combo.setVisible(False)
        top.addWidget(self.verdict_combo)
        layout.addLayout(top)
        period = QHBoxLayout()
        self.month_header = QLabel()
        self.month_header.setObjectName('section')
        period.addWidget(self.month_header,1)
        self.timeline_combo = QComboBox()
        self.timeline_combo.addItem('Перейти к дате…','')
        self.timeline_combo.setMaximumWidth(200)
        period.addWidget(self.timeline_combo)
        self.layout_combo = QComboBox()
        self.layout_combo.addItems(['Плотно','Пропорции','Целиком'])
        self.layout_combo.setToolTip('Целиком — одинаковые ячейки без обрезки кадра')
        period.addWidget(self.layout_combo)
        self.thumbnail_size = QSlider(Qt.Horizontal)
        self.thumbnail_size.setRange(120,420)
        self.thumbnail_size.setValue(220)
        self.thumbnail_size.setFixedWidth(100)
        self.thumbnail_size.setToolTip('Размер миниатюр · Ctrl + колесо')
        period.addWidget(self.thumbnail_size)
        layout.addLayout(period)
        self.map_panel = QWidget()
        map_layout = QVBoxLayout(self.map_panel)
        map_layout.setContentsMargins(0,0,0,0)
        from .map_view import MapView
        self.map_widget = MapView()
        map_layout.addWidget(self.map_widget,1)
        map_legend = QLabel('● Из файла  ·  синий — вручную  ·  фиолетовый — смешанная группа. Догадки модели на карту не наносятся.')
        map_legend.setObjectName('muted')
        map_legend.setWordWrap(True)
        map_layout.addWidget(map_legend)
        map_actions = QHBoxLayout()
        for label,callback in [('Искать в этой области',lambda:self.select_map_bounds(self.map_widget.visible_bounds())),
                               ('Снять область',lambda:self.select_map_bounds('')),('К снимкам',self.map_widget.fit_points),('Весь мир',self.map_widget.reset)]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            map_actions.addWidget(button)
        map_layout.addLayout(map_actions)
        self.map_panel.setMaximumHeight(330)
        self.map_panel.hide()
        layout.addWidget(self.map_panel)
        self.conditions_label = QLabel()
        self.conditions_label.setWordWrap(True)
        self.conditions_label.setTextFormat(Qt.PlainText)
        self.conditions_label.setVisible(False)
        layout.addWidget(self.conditions_label)
        self.model = PhotoModel()
        self.gallery = CanvasGallery()
        self.gallery.setObjectName("photoGallery")
        self.gallery.setViewMode(QListView.IconMode)
        self.gallery.setResizeMode(QListView.Adjust)
        self.gallery.setMovement(QListView.Static)
        self.gallery.setUniformItemSizes(True)
        self.gallery.setLayoutMode(QListView.Batched)
        self.gallery.setBatchSize(100)
        self.gallery.setVerticalScrollMode(QListView.ScrollPerPixel)
        self.gallery.setWrapping(True)
        self.gallery.setSpacing(0)
        self.gallery.setModel(self.model)
        self.model.pageRequested.connect(self.request_page)
        self.model.countChanged.connect(self.show_items)
        self.model.assetsReady.connect(self.assets_ready)
        self.model.visualsReady.connect(self.gallery.viewport().update)
        self.gallery.verticalScrollBar().valueChanged.connect(self.scroll_more)
        self.delegate = PhotoDelegate(self.gallery)
        self.gallery.setItemDelegate(self.delegate)
        self.gallery.selectionModel().currentChanged.connect(self.select_photo)
        self.gallery.doubleClicked.connect(self.view_photo)
        open_shortcut = QShortcut(QKeySequence('Return'),self.gallery,activated=lambda:self.view_photo(self.gallery.currentIndex()))
        open_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        layout.addWidget(self.gallery, 1)
        self.empty_label = QLabel("Здесь появятся фотографии. Нажмите «Добавить папки», чтобы начать.")
        self.empty_label.setWordWrap(True)
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setObjectName("muted")
        layout.addWidget(self.empty_label)
        self.empty_actions = QWidget()
        self.empty_actions_layout = QHBoxLayout(self.empty_actions)
        self.empty_actions_layout.setContentsMargins(0,0,0,0)
        layout.addWidget(self.empty_actions)
        self.empty_actions.hide()
        bottom = QHBoxLayout()
        bottom.addStretch()
        self.more_button = QPushButton("Проверить ещё 40")
        self.more_button.clicked.connect(self.verify_more)
        self.more_button.setVisible(False)
        bottom.addWidget(self.more_button)
        self.cancel_button = QPushButton("Остановить проверку")
        self.cancel_button.clicked.connect(self.cancel_check)
        self.cancel_button.setVisible(False)
        bottom.addWidget(self.cancel_button)
        layout.addLayout(bottom)
        self.splitter.addWidget(body)

    def build_details(self):
        panel = QFrame()
        self.details_panel = panel
        panel.setObjectName("panel")
        panel.setMinimumWidth(260)
        panel.setMaximumWidth(430)
        outer = QVBoxLayout(panel)
        label = QLabel("О фотографии")
        label.setObjectName("section")
        outer.addWidget(label)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        self.details_scroll = scroll
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 4, 0)
        layout.setSizeConstraint(QLayout.SetMinimumSize)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        self.detail_image = FacePreview()
        self.detail_image.setShowBoxes(self.show_face_boxes)
        self.detail_image.personSelected.connect(self.find_person)
        layout.addWidget(self.detail_image)
        self.face_boxes_check = QCheckBox("Показывать рамки лиц")
        self.face_boxes_check.setChecked(self.show_face_boxes)
        self.face_boxes_check.setToolTip("Рамки можно скрыть; нажатия на лица продолжат работать.")
        self.face_boxes_check.toggled.connect(self.set_face_boxes)
        layout.addWidget(self.face_boxes_check)
        self.face_hint = QLabel("Нажмите на лицо для поиска. Это работает и со скрытыми рамками.")
        self.face_hint.setWordWrap(True)
        self.face_hint.setObjectName("muted")
        layout.addWidget(self.face_hint)
        self.details = QLabel()
        self.details.setTextFormat(Qt.RichText)
        self.details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.details.setWordWrap(True)
        self.details.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.details.setText("<p style='color:#82909a'>Выберите фотографию, чтобы увидеть дату съёмки, сведения и описание.</p>")
        layout.addWidget(self.details, 1)
        for title, callback in [("Найти похожие", self.similar), ("Открыть просмотр", lambda: self.view_photo(self.gallery.currentIndex())), ("Открыть оригинал", self.external_open), ("Показать в Проводнике", self.reveal)]:
            button = QPushButton(title)
            button.clicked.connect(callback)
            outer.addWidget(button)
        place = QPushButton('Указать место…')
        place.clicked.connect(self.assign_place)
        outer.addWidget(place)
        self.splitter.addWidget(panel)

    def slider_dates(self, lower, upper):
        from datetime import date
        from PySide6.QtCore import QDate
        for widget, value in ((self.date_from, lower), (self.date_to, upper)):
            value = date.fromordinal(value)
            widget.blockSignals(True)
            widget.setDate(QDate(value.year, value.month, value.day))
            widget.blockSignals(False)

    def edit_dates(self):
        from datetime import date
        lower = date.fromisoformat(self.date_from.date().toString("yyyy-MM-dd")).toordinal()
        upper = date.fromisoformat(self.date_to.date().toString("yyyy-MM-dd")).toordinal()
        self.date_slider.setRange(lower, max(lower, upper))
        self.slider_dates(self.date_slider.lower, self.date_slider.upper)
        self.date_enabled.setChecked(True)

    def commit_dates(self):
        self.date_enabled.setChecked(True)
        self.search()

    def filters(self):
        return asdict(Filters(folder=self.folder_combo.currentData() or "", media_kind=self.media_combo.currentData() or '',
            date_from=self.date_from.date().toString("yyyy-MM-dd") if self.date_enabled.isChecked() else "",
            date_to=self.date_to.date().toString("yyyy-MM-dd") if self.date_enabled.isChecked() else "",
            unknown_date=self.unknown.isChecked(), extension=self.format_combo.currentData() or "",
            camera=self.camera_combo.currentData() or "",
            include_subfolders=self.subfolders.isChecked() if hasattr(self,'subfolders') else True,
            place=self.place_query.text().strip() if hasattr(self,'place_query') else '',geo_bounds=self.geo_bounds,
            has_gps=self.gps_check.isChecked() if hasattr(self,'gps_check') else False))

    def search(self, preserve_position=False):
        if self._restoring_controls:
            return
        anchor = self.before_workspace_search(preserve_position)
        self.gallery.stop_hover()
        self.request_id += 1
        if not self._stack_change or self._stack_change['request_id'] != self.request_id:
            self._stack_change = None
            self.gallery.stop_stack_transition()
        self.view_id = 0
        self.loading = True
        if not self.model.rowCount():
            self.show_items()
        self.refresh_results_button.hide()
        self.more_button.setEnabled(False)
        self.cancel_button.setVisible(False)
        query = self.search_box.text().strip()
        self.message.setText("Ищу фотографии…" if query or self.reference else "Обновляю список…")
        action = "search" if query else "browse"
        extra = {}
        if self.reference:
            action, key, value = self.reference
            extra[key] = value
        if action == "face_search":
            extra["excluded_faces"] = sorted(self.face_rejected)
            if self.selected_people:
                extra['people'] = [{k:p[k] for k in ('id','name','examples','rejected')} for p in self.selected_people]
                if len(extra['people'])==1:
                    extra['people'][0].update(examples=list(self.face_examples),rejected=sorted(self.face_rejected))
                extra['people_mode'] = self.people_mode
        if action == 'similar':
            extra['unit_id'] = self.reference_unit
        self.backend.send(action=action, id=self.request_id, query=query,
                          filters=self.filters(), complex=self.complex_check.isChecked(),
                          presentation=self.presentation_options(),
                          **({'refresh_anchor': anchor} if anchor else {}), **extra)

    def check_updates(self):
        self.show_library_check({'phase': 'queued'})
        self.backend.send(action='check_updates')

    def show_library_check(self, check, paused=False):
        if not check:
            return
        phase = check.get('phase')
        active = phase in ('queued', 'checking', 'reconciling')
        self.check_updates_button.setEnabled(not active)
        self.check_updates_button.setText('Проверка обновлений…' if active else 'Проверить обновления')
        counts = (f"новых: {check.get('added', 0):,} · изменённых: {check.get('changed', 0):,}"
                  f" · возвращённых: {check.get('restored', 0):,}").replace(',', ' ')
        if phase == 'queued':
            text = 'Проверка обновлений поставлена в очередь.'
        elif phase == 'complete':
            text = f"Проверка завершена · {counts} · удалено из каталога: {check.get('removed', 0):,}".replace(',', ' ')
        elif phase == 'error':
            text = 'Проверка не завершена: ' + check.get('error', '') + ' Повторите её после восстановления доступа.'
        else:
            text = ('Проверка на паузе' if paused else 'Проверяю отсутствующие файлы' if phase == 'reconciling'
                    else f"Проверено файлов: {check.get('checked', 0):,}".replace(',', ' ')) + ' · ' + counts
        self.updates_label.setText(text)
        self.updates_label.setToolTip(check.get('folder', ''))
        self.updates_label.show()

    def restore_gallery_position(self, restore, request_id, attempt=0):
        if request_id != self.request_id or not self.model.rowCount():
            return
        row = min(restore['row'], self.model.rowCount() - 1)
        # In a batched QListView the anchor can already have a rectangle while
        # the scroll range still covers only the first batch. Wait for the last
        # row as well; otherwise the scrollbar clamps and the next batch jumps.
        if (not self.gallery.visualRect(self.model.index(row)).isValid() or
                not self.gallery.visualRect(self.model.index(self.model.rowCount()-1)).isValid()) and attempt < 100:
            QTimer.singleShot(20, lambda: self.restore_gallery_position(restore, request_id, attempt + 1))
            return
        bar = self.gallery.verticalScrollBar()
        self.gallery.stop_scroll()
        bar.setValue(bar.value() + self.gallery.visualRect(self.model.index(row)).y() - restore.get('offset_y', 0))
        if self._stack_change and self._stack_change['request_id'] == request_id:
            self._stack_change = None
            self.gallery.finish_stack_transition()

    def clear_query(self):
        self.selected_people = []
        self.current_person_id = None
        self.face_examples.clear()
        self.face_rejected.clear()
        self.face_skipped.clear()
        self.face_picking = False
        self.invalid_faces.clear()
        self.update_face_panel(save=True)
        self.reference = None
        self.search_box.clear()
        self.search()

    def reset_filters(self):
        self._restoring_controls = True
        for combo in (self.folder_combo, self.media_combo, self.format_combo, self.camera_combo):
            combo.setCurrentIndex(0)
        self.date_enabled.setChecked(False)
        self.unknown.setChecked(False)
        self.date_slider.setRange(self.date_slider.minimum, self.date_slider.maximum)
        self.place_query.clear()
        self.geo_bounds = ''
        self.gps_check.setChecked(False)
        self.subfolders.setChecked(True)
        self._restoring_controls = False
        self.search()

    def request_page(self, offset):
        if self.loading:
            self.model.pending.discard(offset)
            return
        self.backend.send(action="search_page", id=self.request_id, view=self.view_id, offset=offset,
                          verdict=self.verdict_combo.currentData() if self.conditions else "",
                          **({'layout_until':offset+self.model.PAGE_SIZE} if offset>self.model.rowCount() else {}))

    def scroll_more(self):
        bar = self.gallery.verticalScrollBar()
        if not self.loading and bar.maximum() - bar.value() < max(500, self.gallery.viewport().height() * 2) and self.model.canFetchMore():
            self.model.fetchMore()

    def change_verdict(self):
        if self.conditions and not self.loading:
            self.view_id += 1
            self.model.reset_result([], 0, False)
            self.clear_details()
            self.model.request_page(0)

    def apply_search_page(self, page):
        self.page_total = page["page_total"]
        self.model.accept_page(page["offset"], page["items"], self.page_total, page.get("has_more", False),
                               layout_geometry=page.get('layout_geometry'))

    def similar(self):
        if self.selected_asset:
            self.face_picking = bool(self.face_examples)
            self.update_face_panel()
            self.reference = ("similar", "asset_id", self.selected_asset["id"])
            self.reference_unit = self.selected_asset.get('unit_id')
            self.search_box.clear()
            self.search()

    def find_person(self, face_id):
        if len(self.selected_people)>1:
            self.selected_people = []
            self.face_examples.clear()
            self.current_person_id = None
        if face_id in self.face_examples and not self.face_picking:
            self.message.setText("Это лицо уже добавлено к примерам выбранного человека.")
            return
        self.face_examples[face_id] = self.face_cache.get(face_id, {"id": face_id})
        self.face_rejected.discard(face_id)
        self.face_skipped.discard(face_id)
        self.update_face_panel(save=True)
        self.activate_person_search()

    def activate_person_search(self):
        self.face_picking = False
        self.reference = ("face_search", "face_ids", list(self.face_examples))
        self.search_box.clear()
        self.update_face_panel()
        self.search()

    def clear_person(self):
        self.clear_query()

    def refine_person(self):
        from .face_review import FaceReviewDialog
        dialog = FaceReviewDialog(self)
        accepted = dialog.exec() == QDialog.Accepted
        if accepted:
            self.apply_face_refinement(dialog.examples, dialog.rejected, dialog.skipped)
        dialog.deleteLater()
        return accepted

    def apply_face_refinement(self, examples, rejected, skipped):
        self.face_examples = OrderedDict(examples)
        self.face_rejected = set(rejected) - set(examples)
        self.face_skipped = set(skipped) - set(examples)
        self.invalid_faces.intersection_update(examples)
        if len(self.selected_people)==1:
            self.selected_people[0] = self.selected_people[0] | dict(examples=list(examples),rejected=sorted(self.face_rejected))
        self.update_face_panel(save=True)
        if self.face_examples:
            self.activate_person_search()
        else:
            self.clear_person()

    def remove_face_example(self, face_id):
        self.face_examples.pop(face_id, None)
        self.invalid_faces.discard(face_id)
        self.update_face_panel(save=True)
        if self.face_examples:
            self.activate_person_search()
        else:
            self.clear_query()

    def toggle_face_picker(self):
        if self.face_picking:
            self.activate_person_search()
        else:
            self.face_picking = True
            self.reference = None
            self.search_box.clear()
            self.update_face_panel()
            self.search()

    def edit_query(self):
        self.reference = None
        self.face_picking = bool(self.face_examples)
        self.update_face_panel()

    def update_face_panel(self, save=False):
        self.face_panel.setExamples(list(self.face_examples.values()), self.face_picking, self.invalid_faces)
        if len(self.selected_people)>1:
            self.face_panel.hide()
        if save:
            self.save_preferences(face_examples=list(self.face_examples), rejected_faces=sorted(self.face_rejected))

    def save_preferences(self, **values):
        try:
            self.preferences.save(**values)
        except OSError as exc:
            self.message.setText(f"Не удалось сохранить настройки окна: {exc}")

    def set_face_boxes(self, visible):
        self.show_face_boxes = visible
        self.face_boxes_check.blockSignals(True)
        self.face_boxes_check.setChecked(visible)
        self.face_boxes_check.blockSignals(False)
        self.detail_image.setShowBoxes(visible)
        self.faceBoxesChanged.emit(visible)
        self.save_preferences(show_face_boxes=visible)

    def show_items(self):
        count = self.model.rowCount()
        self.empty_label.setVisible(not count)
        self.empty_actions.setVisible(not count and not self.loading)
        if not count:
            while self.empty_actions_layout.count():
                child = self.empty_actions_layout.takeAt(0)
                if child.widget():
                    child.widget().deleteLater()
            entries = getattr(self,'_filter_entries',[])
            actions = []
            if self.loading:
                text = 'Ищу материалы…'
            elif self.model.has_more:
                text = 'В просмотренной части кандидатов совпадений пока нет. Можно продолжить поиск.'
                actions.append(('Продолжить поиск',lambda:self.model.fetchMore()))
            elif self.conditions and self.verdict_combo.currentData():
                text = 'В этой группе пока нет материалов. Посмотрите всех кандидатов или продолжите проверку условий.'
                actions.append(('Все кандидаты',lambda:self.verdict_combo.setCurrentIndex(0)))
            elif entries:
                text = 'По этим условиям материалов не найдено. Попробуйте снять один из фильтров; остальные сохранятся.'
            else:
                text = 'Здесь пока нет готовых материалов. Добавьте папки или дождитесь подготовки каталога.'
                actions.append(('Добавить папки',self.add_folder))
            if not self.loading:
                actions += [('Снять: '+title,remove) for title,remove,_ in entries[:3]]
            self.empty_label.setText(text)
            self.empty_actions_layout.addStretch()
            for title,callback in actions:
                button = QPushButton(title)
                button.setMaximumWidth(250)
                button.setToolTip(title)
                button.clicked.connect(callback)
                self.empty_actions_layout.addWidget(button)
            self.empty_actions_layout.addStretch()
        total = self.model.known_total
        suffix = "+" if self.model.has_more else ""
        noun = "совпадений лиц" if self.mode == "face" else "в группе" if self.conditions else "файлов"
        tail = " · прокрутите для продолжения" if count < total or self.model.has_more else ""
        self.result_label.setText(f"{count} из {total}{suffix} {noun}{tail}")
        if self.stacks_check.isChecked() and self.mode=='browse' and self.total!=total:
            self.result_label.setText(f'{self.total:,} материалов · {total:,} карточек со стопками'.replace(',',' '))

    def assets_ready(self, start, end):
        index = self.gallery.currentIndex()
        if start <= index.row() <= end:
            self.select_photo(index)

    def select_photo(self, index, previous=None):
        asset = index.data(PhotoModel.AssetRole)
        if not asset:
            return
        if not self.selected_asset or self.selected_asset["id"] != asset["id"]:
            self.details_scroll.verticalScrollBar().setValue(0)
        self.selected_asset = asset
        self.save_context_timer.start()
        pixmap = QPixmap(asset.get("thumbnail") or "")
        self.detail_image.setPhoto(pixmap)
        self.face_hint.setText("Определяю лица…")
        self.backend.send(action="faces", asset_id=asset["id"], unit_id=asset.get('unit_id'))
        from datetime import datetime
        date_text = datetime.fromisoformat(asset["captured_at"]).strftime("%d.%m.%Y %H:%M:%S") if asset.get("captured_at") else "Неизвестна"
        blocks = [f"<h3>{html.escape(asset['filename'])}</h3>",
                  f"<p><b>Дата съёмки · {'метаданные видео' if asset.get('media_kind') == 'video' else 'EXIF'}</b><br>{html.escape(date_text)}</p>",
                  f"<p>{asset['width']} × {asset['height']} · {asset['extension'].upper().lstrip('.')} · {asset['size']/1024**2:.2f} МБ</p>",
                  f"<p>{html.escape(asset.get('camera') or 'Камера не указана')}</p>"]
        if asset.get('media_kind') == 'video':
            from .video import search_coverage_text
            blocks.append(f"<p><b>Видео · {timestamp_text(asset.get('duration_ms'))}</b><br>Найденный кадр: {timestamp_text(asset.get('timestamp_ms'))}. "
                          +html.escape(search_coverage_text(asset))+'</p>')
        if asset.get("geo_text") or asset.get("latitude") is not None:
            location = html.escape(asset.get("geo_text") or "")
            if asset.get("latitude") is not None:
                location += f"<br>GPS: {asset['latitude']:.6f}, {asset['longitude']:.6f}"
            source = html.escape(asset.get('geo_source') or 'Метаданные файла')
            try:
                approximate = json.loads(asset.get('geo_json') or '{}').get('place_names_are_approximate',False)
            except (ValueError,TypeError):
                approximate = False
            if approximate:
                location += '<br>Название подобрано по ближайшему городу; это приблизительный ориентир.'
            blocks += ["<h4>Место съёмки · из файла</h4>", f"<p>{location}<br>Источник: {source}</p>"]
        if asset.get('user_place') or asset.get('user_latitude') is not None:
            location = html.escape(asset.get('user_place') or '')
            if asset.get('user_latitude') is not None:
                location += f"<br>{asset['user_latitude']:.6f}, {asset['user_longitude']:.6f}"
            blocks += ['<h4>Место · указано вручную в каталоге</h4>',f'<p>{location}</p>']
        if asset.get("face_score") is not None:
            blocks.append(f"<p>Сходство лиц: {asset['face_score']:.3f} · оценка модели</p>")
        if asset.get("description"):
            blocks += ["<h4>Описание · оценка модели</h4>", f"<p>{html.escape(asset['description'])}</p>"]
        else:
            blocks += ["<p style='color:#82909a'>Описание ещё не подготовлено.</p>"]
        verification = asset.get("verification")
        if verification:
            blocks.append("<h4>Проверка условий</h4>")
            if verification.get('scope') == 'frame':
                blocks.append(f"<p>Проверен кадр {timestamp_text(verification.get('timestamp_ms'))}. Вывод не относится ко всему ролику.</p>")
            for check in verification.get("checks", []):
                verdict = {"yes": "✓", "no": "✗", "uncertain": "?"}[check["verdict"]]
                blocks.append(f"<p><b>{verdict} {html.escape(check['condition'])}</b><br>{html.escape(check['evidence'])}</p>")
            if verification.get("error"):
                blocks.append(f"<p>{html.escape(verification['error'])}</p>")
        blocks += [f"<p style='color:#82909a'>{html.escape(asset['relative_path'])}</p>"]
        self.details.setText("".join(blocks))

    def clear_details(self):
        self.selected_asset = None
        self.detail_image.setPhoto(QPixmap())
        self.face_hint.setText("Выберите фотографию для поиска по лицу.")
        self.details.setText("<p style='color:#82909a'>Выберите фотографию, чтобы увидеть её сведения.</p>")

    def view_photo(self, index):
        if index.isValid():
            asset = index.data(PhotoModel.AssetRole)
            if asset and asset.get('media_kind') == 'video':
                self.open_video_moment(asset)
                return
            viewer = Viewer(self.model, index.row(), self.cfg, self, self.backend)
            viewer.personSelected.connect(self.find_person)
            viewer.exec()

    def open_video_moment(self, asset, moment=False):
        from .video_player import VideoPlayerDialog
        from .video import asset_at_moment
        player = VideoPlayerDialog(asset_at_moment(asset,moment) if moment else asset, self.cfg, self, self.backend)
        if moment is None:
            QTimer.singleShot(0,player.more_moments)
        player.exec()
        player.deleteLater()

    def external_open(self):
        if self.selected_asset:
            try:
                open_file(self.selected_asset["path"])
            except OSError as exc:
                self.message.setText(str(exc))

    def reveal(self):
        if self.selected_asset:
            path = self.selected_asset["path"]
            if Path(path).exists():
                subprocess.Popen(["explorer.exe", "/select,", path], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                self.message.setText("Оригинал недоступен. Подключите диск с архивом.")

    def toggle_pause(self):
        self.backend.send(action="resume" if self.paused else "pause")

    def verify_more(self):
        self.more_button.setEnabled(False)
        self.cancel_button.setVisible(True)
        self.backend.send(action="verify_more", id=self.request_id)

    def cancel_check(self):
        self.backend.send(action="cancel", id=self.request_id)
        self.more_button.setEnabled(True)
        self.cancel_button.setVisible(False)
        self.message.setText("Проверка остановится после текущей фотографии. Результаты сохранены.")

    def add_folder(self):
        from .folders import choose_folders
        paths = choose_folders(self)
        if paths:
            self.backend.send(action="add_folders", paths=paths)

    def orientation_dialog(self):
        from .orientation_ui import OrientationDialog
        dialog = OrientationDialog(self)
        dialog.exec()
        dialog.deleteLater()

    def models_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Локальные модели")
        dialog.resize(550, 280)
        layout = QVBoxLayout(dialog)
        label = QLabel("SigLIP 2 — поиск по смыслу, DirectML.\nQwen3-VL 4B — описания и проверка условий, Vulkan.\nYuNet + SFace — обнаружение и эмбеддинги лиц, DirectML.\nGeoNames — локальный справочник мест, CC BY 4.0.\n\nМодели хранятся в " + str(self.cfg.data_dir / "models") + ".\nПосле загрузки интернет для работы не нужен.")
        label.setWordWrap(True)
        label.setTextFormat(Qt.PlainText)
        layout.addWidget(label)
        button = QPushButton("Загрузить или проверить файлы моделей")
        button.clicked.connect(lambda: (self.backend.send(action="download_models"), dialog.accept()))
        layout.addWidget(button)
        dialog.exec()

    def update_facets(self, facets):
        restoring = self._restoring_controls
        self._restoring_controls = True
        from datetime import date
        bounds = facets.get("date_bounds")
        if bounds and all(bounds):
            self.date_slider.setBounds(*(date.fromisoformat(value).toordinal() for value in bounds))
        for field, combo, title in [("folder", self.folder_combo, "Все выбранные папки"), ("camera", self.camera_combo, "Все камеры"), ("extension", self.format_combo, "Все форматы")]:
            current = combo.currentData()
            combo.clear()
            combo.addItem(title, "")
            for value in facets.get(field, []):
                combo.addItem(value, value)
            position = combo.findData(current)
            if position < 0 and current:
                combo.addItem(current, current)
                position = combo.count() - 1
            combo.setCurrentIndex(max(0, position))
        self._restoring_controls = restoring

    def on_event(self, event):
        if self.workspace_event(event):
            return
        kind = event["type"]
        if kind == 'library_check':
            check = event['check']
            self.show_library_check(check)
            if check.get('phase') == 'complete' and any(check.get(k) for k in ('added', 'changed', 'removed', 'restored')):
                self.search(preserve_position=True)
            return
        if kind == 'orientation_progress':
            stats = event['stats']
            self.orientation_stats = stats
            self.rotation_button.setText(f"Поворот фото · {stats['checked']}/{stats['total']}" if stats['running'] else
                                         f"Поворот фото · {stats['recommendations']}" if stats['recommendations'] else 'Поворот фото')
            self.rotation_button.setToolTip(f"Проверено {stats['checked']} из {stats['total']}; исключено {stats['excluded']}")
            return
        if kind == 'rotation_changed':
            if not self.rotation_refresh.isActive():
                self.rotation_refresh.start(500)
            return
        if kind in {"results", "verified", "verification_done", "search_page"} and event.get("id") != self.request_id:
            return
        if kind == "results":
            self.loading = False
            self.result_metadata_count = event.get("metadata_count", self.last_metadata_count)
            self.refresh_results_button.hide()
            self.total = event["total"]
            self.semantic = event.get("semantic", False)
            self.mode = event.get("mode", "semantic" if self.semantic else "browse")
            self.sort_combo.setEnabled(self.mode=='browse')
            if self.mode == "face":
                for face in event.get("examples", []):
                    if face["id"] in self.face_examples:
                        self.face_examples[face["id"]] = face
                self.invalid_faces.clear()
                self.update_face_panel()
            self.conditions = event.get("conditions", [])
            self.verdict_combo.blockSignals(True)
            self.verdict_combo.setCurrentIndex(0)
            self.verdict_combo.blockSignals(False)
            self.verdict_combo.setVisible(bool(self.conditions))
            self.conditions_label.setVisible(bool(self.conditions))
            self.conditions_label.setText("  •  ".join(self.conditions))
            self.more_button.setVisible(bool(self.conditions))
            self.more_button.setEnabled(False)
            self.cancel_button.setVisible(bool(self.conditions))
            self.page_total = event.get("page_total", self.total)
            restore = event.get('restore')
            stack_change = self._stack_change if self._stack_change and self._stack_change['request_id'] == self.request_id else None
            # Restoring a deep viewport with 100-row batches needs hundreds of
            # event-loop turns. Larger bounded batches keep work per turn short
            # while finishing the offscreen geometry without a long delay.
            self.gallery.setBatchSize(1000 if restore and restore.get('loaded_count',0)>2000 else 100)
            self.model.reset_result(event["items"], self.page_total, event.get("has_more", False),
                                    offset=event.get('offset', 0), loaded_count=restore.get('loaded_count', 0) if restore else 0,
                                    geometry_prefix=stack_change['geometry_prefix'] if stack_change else None,
                                    layout_geometry=restore.get('layout_geometry') if restore else None)
            self.clear_details()
            if event["items"]:
                self.gallery.setCurrentIndex(self.model.index(restore['selected_row'] if restore else 0))
            if restore:
                QTimer.singleShot(0, lambda state=restore, request=self.request_id: self.restore_gallery_position(state, request))
            self.update_month_header()
            self.save_context_timer.start()
            if self.map_toggle.isChecked():
                self.request_map()
            self.message.setText("Проверяю условия на фотографиях. Неопределённые случаи будут показаны отдельно." if self.conditions else
                                 f"Фильтр человека: {len(self.face_examples)} примеров. Добавьте другой ракурс или нажмите «Уточнить фильтр». Совпадения могут быть неточными." if self.mode == "face" else
                                 "Результаты упорядочены по близости к запросу. Дальние результаты могут не соответствовать смыслу." if self.semantic else
                                 "Дата съёмки берётся из EXIF. Нажмите на выделенное лицо в просмотре или справа для поиска человека.")
        elif kind == "verified":
            verdict = self.verdict_combo.currentData() or ""
            if verdict:
                self.view_id += 1
            total = event["groups"][verdict]
            if self.stacks_check.isChecked():
                # Verification counts materials; the gallery counts stack cards.
                total = event.get('presentation_total',self.model.known_total) if event.get('presentation_verdict','')==verdict else self.model.known_total
            self.model.update_verification(event["asset"] | {"verification": event["result"]}, total,
                                           event["has_more"] and verdict in ("", "pending"), regroup=bool(verdict))
            if not verdict and self.selected_asset and self.selected_asset["id"] == event["asset"]["id"]:
                self.select_photo(self.gallery.currentIndex())
            if verdict:
                visible = self.gallery.indexAt(self.gallery.viewport().rect().topLeft()).row()
                self.model.request_page(max(0, visible))
            self.message.setText(f"Проверено {event['checked']} из {event['candidates']} кандидатов. Выводы модели могут быть неточными.")
        elif kind == "search_page":
            if event.get("view", 0) == self.view_id and event["verdict"] == (self.verdict_combo.currentData() if self.conditions else ""):
                self.apply_search_page(event)
        elif kind == "face_list":
            for face in event["faces"]:
                self.face_cache[face["id"]] = face
                self.face_cache.move_to_end(face["id"])
            while len(self.face_cache) > 512:
                self.face_cache.popitem(last=False)
            if self.selected_asset and (event["asset_id"], event["version"]) == (self.selected_asset["id"], self.selected_asset["version"]):
                if event.get('unit_id') != self.selected_asset.get('unit_id'):
                    return
                self.detail_image.setFaces(event["faces"])
                self.face_hint.setText(("Нажмите на лицо этого же человека, чтобы добавить ещё один пример." if self.face_examples else "Нажмите на лицо для поиска. Скрытые рамки тоже работают.") if event["faces"] else "Лиц для поиска не обнаружено. Можно увеличить снимок в просмотре.")
        elif kind == "verification_done":
            self.more_button.setEnabled(not event.get("exhausted", False))
            self.cancel_button.setVisible(False)
        elif kind == "source_inventory":
            self.source_inventory = event['inventory']
            self.show_processing_progress()
        elif kind == "status":
            stats = event["stats"]
            self.latest_stats = stats
            if 'inventory' in event:
                self.source_inventory = event['inventory']
            total = self.source_inventory.get('total') or self.source_inventory.get('last_total') or stats.get('total',0)
            done = self.source_inventory.get('counts',{}).get('scanned',stats.get('metadata',0))
            self.compact_status.setText(f"Каталог {done:,} / {total:,} · поиск {stats.get('embeddings',0):,} · лиц {stats.get('faces',0):,} · ошибок {stats.get('errors',0):,}".replace(',',' '))
            pipeline = event.get('pipeline',{})
            self.show_library_check(pipeline.get('library_check'), event['paused'])
            if pipeline:
                active = min(pipeline['workers'],pipeline['in_flight'])
                cpu = f"Подготовка CPU: до {active} из {pipeline['workers']} процессов · в очереди {pipeline['in_flight']}"
                names = {'embedding':'эмбеддинги','faces':'лица','location':'места','caption':'описания'}
                gpu = names.get(pipeline.get('gpu_stage'),'ожидает готовые фотографии')
                if pipeline.get('caption_running'):
                    gpu = gpu + ' + описания' if pipeline.get('gpu_stage') in names else 'описания'
                if event['paused']:
                    gpu = ('завершает текущее описание' if pipeline.get('caption_running') else
                           'проверка ориентации' if self.orientation_stats.get('running') else
                           'поворот и обновление поиска' if self.orientation_stats.get('edits_pending') else
                           'индексация на паузе' if stats['pending'] else 'индексация завершена')
                frames = stats.get('video_frames', {})
                frame_text = (f"  ·  Кадры видео: поиск {frames.get('embedding', 0)}, лица {frames.get('faces', 0)}, "
                              f"описания {frames.get('caption', 0)} из {frames['total']}") if frames.get('total') else ''
                self.pipeline_label.setText(cpu + '  ·  GPU: ' + gpu + ('  ·  сканирование папок' if event.get('scanning') else '') + frame_text)
            if stats["metadata"] != self.last_metadata_count:
                self.last_metadata_count = stats["metadata"]
                if stats["metadata"] == stats["total"] and stats["total"]:
                    self.backend.send(action="facets")
            if (not self.loading and self.mode == "browse" and not self.reference
                    and not self.search_box.text().strip() and self.result_metadata_count >= 0
                    and stats["metadata"] != self.result_metadata_count):
                if self.model.rowCount():
                    self.refresh_results_button.show()
                else:
                    # An empty catalogue may populate immediately: there is no
                    # selection or scrolling position to displace yet.
                    self.search()
            self.paused = event["paused"]
            self.summary.setText(f"{stats['total']:,} файлов · локальный каталог".replace(",", " "))
            self.pause_button.setText("Продолжить индексацию" if self.paused else "Пауза")
            self.error_button.setText(f"Ошибки: {stats['errors']}")
            self.show_processing_progress()
            if self.paused and self.orientation_stats.get('running'):
                self.stage_label.setText(f"Проверка ориентации · {self.orientation_stats['checked']} / {self.orientation_stats['total']}")
            elif self.orientation_stats.get('edits_pending'):
                self.stage_label.setText(f"Поворот и обновление поиска · осталось {self.orientation_stats['edits_pending']} файлов")
            elif self.paused:
                self.stage_label.setText("Индексация на паузе" if stats["pending"] else
                                        f"Обработка завершена · ошибок: {stats['errors']}" if stats["errors"] else "Все выбранные фотографии обработаны")
            if event.get("scanning") and not pipeline:
                self.stage_label.setText("Сканирую выбранные папки…")
        elif kind == "working":
            names = {"metadata": "Подготовка каталога", "embedding": "Поисковый индекс", "caption": "Описание фотографии", "query": "Сложный поиск", "faces": "Поиск лиц", "location": "Место съёмки", "orientation": "Проверка ориентации", "rotation": "Поворот и обновление поиска"}
            self.stage_label.setText(names.get(event["stage"], event["stage"]) + " · " + event["filename"])
        elif kind in {"ready", "scan_done", "index_done"}:
            if "includes" in event:
                self.cfg.includes = event["includes"]
            self.update_facets(event.get("facets", {}))
            self.navigation_timer.start()
        elif kind == "folders_added":
            self.cfg.includes = event["includes"]
            self.message.setText(f"Добавлено папок: {len(event['added'])}. Уже в каталоге, пропущено: {len(event['skipped'])}." +
                                 (" Не удалось добавить: " + "; ".join(item["path"] + ": " + item["error"] for item in event["errors"]) if event["errors"] else ""))
        elif kind in {"error", "fatal", "job_error", "message"}:
            if event.get("id") is not None and event["id"] != self.request_id:
                return
            self.message.setText(event["message"])
            if kind in {"error", "fatal"}:
                self.loading = False
                if event.get("action") == "search_page":
                    self.model.pending.discard(event.get("offset"))
                elif event.get("action") in {"search", "similar", "browse", "face_search"}:
                    self.model.reset_result([], 0, False)
                    self.invalid_faces = set(event.get("invalid_face_ids", []))
                    self.update_face_panel()
        elif kind == "errors":
            dialog = QDialog(self)
            dialog.setWindowTitle("Ошибки обработки")
            dialog.resize(850, 450)
            layout = QVBoxLayout(dialog)
            text = QTextBrowser()
            text.setPlainText("\n\n".join(f"{r['path']}\n{r['stage']}: {r['error']}" for r in event["items"]) or "Ошибок нет.")
            layout.addWidget(text)
            retry = QPushButton("Повторить обработку файлов с ошибками")
            retry.clicked.connect(lambda: (self.backend.send(action="retry"), dialog.accept()))
            layout.addWidget(retry)
            dialog.exec()

    def show_processing_progress(self):
        from .source_details import source_summary
        inventory = self.source_inventory
        self.source_label.setText(source_summary(inventory))
        counts = inventory.get('counts', {})
        total = inventory.get('total')
        ready = inventory.get('phase') == 'ready' and total is not None
        for key, (label, bar, name) in self.progress_bars.items():
            done = counts.get('scanned' if key == 'metadata' else key, self.latest_stats.get(key, 0))
            if ready:
                label.setText(f"{name}  {done:,} / {total:,}".replace(',', ' '))
                bar.setRange(0, max(1, total))
                bar.setValue(done)
                tooltip = 'Все изображения (включая RAW) и видео во всех добавленных папках и их подпапках. Видео считается одним файлом.'
                if key == 'metadata':
                    tooltip += (f"\nПросканировано: {done}. Готово к просмотру: {counts.get('metadata', 0)}."
                                f" Не удалось прочитать: {counts.get('unreadable', 0)}. Подробности — в списке ошибок.")
            elif inventory.get('phase') == 'error':
                label.setText('Каталог · подсчёт недоступен' if key == 'metadata' else f'{name}  {done:,} / ?'.replace(',', ' '))
                bar.setRange(0, 1)
                bar.setValue(0)
                tooltip = (inventory.get('error') or 'Не удалось подсчитать снимки.') + '\nКаталог сохранён. Подсчёт повторится при продолжении индексации.'
                if inventory.get('last_total') is not None:
                    tooltip += f"\nПоследнее известное количество: {inventory['last_total']}."
            else:
                label.setText('Каталог · считаю снимки…' if key == 'metadata' else f'{name}  {done:,} / …'.replace(',', ' '))
                bar.setRange(0, 0)
                tooltip = 'Подсчитываю изображения и видео во всех добавленных папках. Подготовка файлов продолжается.'
            label.setToolTip(tooltip)
            bar.setToolTip(tooltip)

    def show_source_details(self):
        from .source_details import SourceDetailsDialog
        dialog = SourceDetailsDialog(self)
        dialog.exec()
        dialog.deleteLater()

    def closeEvent(self, event):
        self.persist_workspace()
        self.auto_filter.stop()
        self.save_context_timer.stop()
        self.navigation_timer.stop()
        self.folder_filter_timer.stop()
        self.map_delay.stop()
        self.gallery.stop_hover()
        self.backend.close()
        self.model.pool.waitForDone(3000)
        event.accept()
