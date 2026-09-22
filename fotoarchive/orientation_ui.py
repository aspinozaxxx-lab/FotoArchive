"""Paged review before an explicitly approved batch changes original files."""
from uuid import uuid4

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QPixmap, QTransform
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel,
                              QPushButton, QScrollArea, QVBoxLayout, QWidget)


class OrientationDialog(QDialog):
    PAGE_SIZE = 40

    def __init__(self, owner):
        super().__init__(owner)
        self.owner, self.backend = owner, owner.backend
        self.offset, self.total = 0, 0
        self.stats = {}
        self.items = []
        self.checkboxes = {}
        self.request_id = None
        self.loading = False
        self.cleaned_up = False
        self.setWindowTitle('Поворот фотографий')
        self.resize(1180, 860)
        layout = QVBoxLayout(self)
        heading = QLabel('Проверьте поворот фотографий')
        heading.setObjectName('title')
        layout.addWidget(heading)
        explanation = QLabel('Фоновая проверка учитывает людей и геометрию сцены, игнорируя впечатанную дату. '
                             'После нажатия «Повернуть выбранные» изменятся исходные файлы. Перед каждым изменением сохраняется резервная копия.')
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        controls = QHBoxLayout()
        self.scope = QComboBox()
        self.scope.addItem('Все подключённые фотографии', {})
        self.scope.addItem('Текущие фильтры каталога', owner.filters())
        controls.addWidget(self.scope, 1)
        self.start_button = QPushButton('Начать / продолжить проверку')
        self.start_button.clicked.connect(lambda: self.send('orientation_start', filters=self.scope.currentData()))
        controls.addWidget(self.start_button)
        self.pause_button = QPushButton('Пауза после текущего снимка')
        self.pause_button.clicked.connect(lambda: self.send('orientation_pause'))
        controls.addWidget(self.pause_button)
        retry = QPushButton('Повторить ошибки проверки')
        retry.clicked.connect(lambda: self.send('orientation_retry'))
        controls.addWidget(retry)
        layout.addLayout(controls)
        self.progress = QLabel('Загружаю состояние…')
        self.progress.setWordWrap(True)
        layout.addWidget(self.progress)
        tabs = QHBoxLayout()
        self.group = QComboBox()
        for label, value in [('Рекомендуется повернуть','suggestions'),('Нужна ручная проверка','uncertain'),
                             ('Исключённые','excluded'),('Ориентация верная','upright'),('Применённые повороты','applied'),
                             ('Ошибки проверки','errors'),('Ошибки изменения файлов','edit_errors')]:
            self.group.addItem(label,value)
        self.group.currentIndexChanged.connect(self.change_group)
        tabs.addWidget(self.group)
        self.page_label = QLabel()
        tabs.addWidget(self.page_label,1)
        self.previous = QPushButton('← Назад')
        self.previous.clicked.connect(lambda: self.change_page(-1))
        tabs.addWidget(self.previous)
        self.next = QPushButton('Далее →')
        self.next.clicked.connect(lambda: self.change_page(1))
        tabs.addWidget(self.next)
        refresh = QPushButton('Обновить список')
        refresh.clicked.connect(self.request_page)
        tabs.addWidget(refresh)
        layout.addLayout(tabs)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setStyleSheet('QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }')
        self.content = QWidget()
        self.rows = QVBoxLayout(self.content)
        self.rows.setAlignment(Qt.AlignTop)
        self.scroll.setWidget(self.content)
        layout.addWidget(self.scroll,1)
        self.notice = QLabel('Снимите отметку, чтобы оставить фото в рекомендациях, или исключите его из будущей проверки.')
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.PlainText)
        layout.addWidget(self.notice)
        footer = QHBoxLayout()
        self.undo = QPushButton('Отменить последнюю партию')
        self.undo.setEnabled(False)
        self.undo.clicked.connect(self.undo_batch)
        footer.addWidget(self.undo)
        cancel_pending = QPushButton('Отменить ожидающие повороты')
        cancel_pending.clicked.connect(lambda: self.send('orientation_cancel_edits'))
        footer.addWidget(cancel_pending)
        footer.addStretch()
        close = QPushButton('Закрыть')
        close.clicked.connect(self.accept)
        footer.addWidget(close)
        self.apply = QPushButton('Повернуть выбранные')
        self.apply.setEnabled(False)
        self.apply.setObjectName('primary')
        self.apply.clicked.connect(lambda: self.send('orientation_apply'))
        footer.addWidget(self.apply)
        layout.addLayout(footer)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(5000)
        self.refresh_timer.timeout.connect(self.auto_refresh)
        self.refresh_timer.start()
        self.backend.event.connect(self.on_event)
        self.send('orientation_status')
        self.request_page()

    def send(self, action, **values):
        self.backend.send(action=action, id='orientation-control-'+uuid4().hex, **values)

    def request_page(self):
        self.loading = True
        self.request_id = 'orientation-page-'+uuid4().hex
        self.backend.send(action='orientation_page', id=self.request_id, group=self.group.currentData(), offset=self.offset)

    def auto_refresh(self):
        # Keep stable rows under the user's cursor while reviewing. A new page is
        # fetched automatically only when there were no recommendations yet.
        if not self.loading and not self.items and (self.stats.get('running') or self.stats.get('edits_pending')):
            self.request_page()

    def change_group(self):
        self.offset = 0
        self.request_page()

    def change_page(self, direction):
        self.offset = max(0,self.offset + direction*self.PAGE_SIZE)
        self.request_page()

    def undo_batch(self):
        if self.stats.get('last_batch'):
            self.send('orientation_undo', batch_id=self.stats['last_batch'])

    def on_event(self, event):
        if self.cleaned_up:
            return
        kind = event['type']
        if kind == 'orientation_progress':
            self.stats = event['stats']
            state = self.stats
            phase = 'Проверка выполняется' if state['running'] else 'Проверка на паузе' if state['pending'] else 'Проверка завершена' if state['total'] else 'Проверка ещё не запускалась'
            seconds = (state.get('average_seconds') or 0)*state['pending']
            estimate = f' · осталось около {max(1,round(seconds/60))} мин' if state['running'] and seconds else ''
            self.progress.setText(f"{phase} · проверено {state['checked']} / {state['total']} · рекомендуется: {state['recommendations']}"
                                  f" · исключено: {state['excluded']} · ошибок: {state['errors']+state['edit_errors']}{estimate}\n"
                                  f"Поворот и обновление поиска: в очереди {state['edits_pending']}, применено {state['applied']}.")
            self.apply.setText(f"Повернуть выбранные ({state['selected']})")
            self.apply.setEnabled(state['selected'] > 0)
            self.undo.setEnabled(bool(state['last_batch']) and not state['edits_pending'])
        elif kind == 'orientation_page' and event.get('id') == self.request_id:
            self.loading = False
            self.items, self.total = event['items'], event['total']
            if self.offset and not self.items:
                self.offset = max(0,self.offset-self.PAGE_SIZE)
                self.request_page()
                return
            self.render()
            self.page_label.setText(f"{self.offset+1 if self.items else 0}–{self.offset+len(self.items)} из {self.total}")
            self.previous.setEnabled(self.offset>0)
            self.next.setEnabled(self.offset+len(self.items)<self.total)
        elif kind == 'orientation_notice':
            self.notice.setText(event['message'])
            self.request_page()
        elif kind == 'error' and event.get('action','').startswith('orientation_'):
            self.notice.setText(event['message'])
            if event.get('id') == self.request_id:
                self.loading = False
            elif event.get('action') == 'orientation_decide':
                self.request_page()
        elif kind == 'rotation_changed':
            self.request_page()

    def decide(self, asset, **values):
        self.send('orientation_decide', asset_id=asset['id'], version=asset['version'], **values)

    def render(self):
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.checkboxes = {}
        if not self.items:
            label = QLabel('В этой группе пока нет фотографий. Готовые рекомендации появляются по мере фоновой проверки.')
            label.setWordWrap(True)
            self.rows.addWidget(label)
        for asset in self.items:
            card = QFrame()
            card.setObjectName('panel')
            row = QHBoxLayout(card)
            editable = 'edit_id' not in asset and self.group.currentData() not in {'excluded','errors'}
            before = QPixmap(asset.get('thumbnail') or '')
            for title, angle in [('Сейчас',0),('После поворота',asset.get('proposed_rotation') or 0)]:
                column = QVBoxLayout()
                label = QLabel(title if editable else 'Фотография')
                label.setAlignment(Qt.AlignCenter)
                column.addWidget(label)
                preview = QPushButton()
                preview.setFixedSize(194,154)
                preview.setAutoDefault(False)
                from PySide6.QtGui import QIcon
                preview.setIcon(QIcon(before.transformed(QTransform().rotate(angle),Qt.SmoothTransformation)))
                preview.setIconSize(QSize(176,138))
                preview.clicked.connect(lambda checked=False, value=asset:self.open_photo(value))
                column.addWidget(preview)
                row.addLayout(column)
                if not editable:
                    break
            info = QVBoxLayout()
            name = QLabel(asset['relative_path'])
            name.setTextFormat(Qt.PlainText)
            name.setWordWrap(True)
            name.setObjectName('section')
            info.addWidget(name)
            reason = QLabel(asset.get('rotation_error') or asset.get('rotation_reason') or 'Поворот применён; резервная копия сохранена.')
            reason.setTextFormat(Qt.PlainText)
            reason.setWordWrap(True)
            info.addWidget(reason)
            if editable:
                select = QCheckBox('Применить к исходному файлу')
                select.setChecked(bool(asset.get('selected')))
                select.setEnabled(bool(asset.get('proposed_rotation')))
                select.toggled.connect(lambda checked, value=asset:self.decide(value,selected=checked))
                self.checkboxes[asset['id']] = select
                info.addWidget(select)
                angle = QComboBox()
                for text, value in [('Не поворачивать / не определено',0),('↻ На 90° вправо',90),('На 180°',180),('↺ На 90° влево',270)]:
                    angle.addItem(text,value)
                angle.setCurrentIndex(max(0,angle.findData(asset.get('proposed_rotation') or 0)))
                def change_angle(index, combo=angle, value=asset, check=select):
                    rotation = combo.currentData()
                    check.blockSignals(True)
                    check.setChecked(False)
                    check.setEnabled(bool(rotation))
                    check.blockSignals(False)
                    self.decide(value,rotation=rotation,selected=False)
                    self.request_page()
                angle.currentIndexChanged.connect(change_angle)
                info.addWidget(angle)
            if 'edit_id' not in asset:
                exclude = QPushButton('Вернуть в проверку' if asset.get('excluded') else 'Исключить эту фотографию')
                exclude.clicked.connect(lambda checked=False, value=asset:(self.decide(value,excluded=not value.get('excluded')),self.request_page()))
                info.addWidget(exclude)
            info.addStretch()
            row.addLayout(info,1)
            self.rows.addWidget(card)

    def open_photo(self, asset):
        from .ui import Viewer
        viewer = Viewer([asset],0,self.owner.cfg,self,self.backend)
        viewer.exec()
        viewer.deleteLater()

    def done(self, result):
        if not self.cleaned_up:
            self.cleaned_up = True
            self.refresh_timer.stop()
            self.backend.event.disconnect(self.on_event)
        super().done(result)
