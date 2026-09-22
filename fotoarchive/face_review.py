"""A transactional editor for one person's face filter."""
from collections import OrderedDict
from uuid import uuid4

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
                              QPushButton, QScrollArea, QVBoxLayout, QWidget)

from .face_widgets import FaceFilterPanel


class FaceReviewDialog(QDialog):
    def __init__(self, owner, *, examples=None, rejected=None, skipped=None):
        super().__init__(owner)
        self.owner, self.backend = owner, owner.backend
        self.examples = OrderedDict(owner.face_examples if examples is None else examples)
        self.rejected = set(owner.face_rejected if rejected is None else rejected)
        self.skipped = set(owner.face_skipped if skipped is None else skipped)
        self.seen = set()
        self.faces = dict(self.examples)
        self.decisions = {}
        self.history = []
        self.items = []
        self.request_id = None
        self.loading = False
        self.cleaned_up = False
        self.setWindowTitle("Уточнить фильтр человека")
        self.resize(1020, 860)
        self.setMinimumSize(810, 620)
        layout = QVBoxLayout(self)
        title = QLabel("Уточните, кого искать")
        title.setObjectName("title")
        layout.addWidget(title)
        hint = QLabel("Все выбранные примеры должны показывать одного человека. Подтвердите новые ракурсы или удалите ошибочные примеры. Изменения вступят в силу после применения.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.examples_panel = FaceFilterPanel()
        self.examples_panel.clear_button.hide()
        self.examples_panel.refine_button.hide()
        self.examples_panel.browse_button.hide()
        self.examples_panel.removeExample.connect(self.remove_example)
        layout.addWidget(self.examples_panel)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.status)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        cards = QWidget()
        self.grid = QGridLayout(cards)
        self.grid.setAlignment(Qt.AlignTop)
        scroll.setWidget(cards)
        layout.addWidget(scroll, 1)
        self.decision_buttons = {}
        tools = QHBoxLayout()
        self.undo_button = QPushButton("Отменить последнее решение")
        self.undo_button.clicked.connect(self.undo)
        tools.addWidget(self.undo_button)
        self.restore_button = QPushButton()
        self.restore_button.clicked.connect(self.restore_rejected)
        tools.addWidget(self.restore_button)
        tools.addStretch()
        self.next_button = QPushButton("Предложить ещё")
        self.next_button.clicked.connect(self.request_suggestions)
        tools.addWidget(self.next_button)
        layout.addLayout(tools)
        note = QLabel("Это предложения модели, а не установленные личности. «Другой» исключает только отмеченное лицо; «Пропустить» не меняет совпадения.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        layout.addWidget(note)
        actions = QHBoxLayout()
        self.summary = QLabel()
        actions.addWidget(self.summary, 1)
        cancel = QPushButton("Отмена")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        self.apply_button = QPushButton("Применить фильтр")
        self.apply_button.setObjectName("primary")
        self.apply_button.clicked.connect(self.accept)
        actions.addWidget(self.apply_button)
        layout.addLayout(actions)
        self.timeout = QTimer(self)
        self.timeout.setSingleShot(True)
        self.timeout.timeout.connect(self.timed_out)
        self.backend.event.connect(self.on_event)
        self.refresh()
        self.request_suggestions()

    def remember(self):
        if self.loading:
            self.backend.send(action="cancel_face_suggestions", id=self.request_id)
            self.request_id = None
            self.loading = False
            self.timeout.stop()
            self.status.setText("Примеры изменились. Нажмите «Предложить ещё», чтобы учесть изменения.")
        self.history.append((OrderedDict(self.examples), set(self.rejected), set(self.skipped), dict(self.decisions)))
        self.history = self.history[-50:]

    def refresh(self):
        self.examples_panel.setExamples(list(self.examples.values()))
        self.examples_panel.title.setText(f"Примеры одного человека · {len(self.examples)}")
        self.examples_panel.hint.setText("Удалите лишний пример кнопкой ×. Для другого ракурса выберите «Тот же человек» ниже.")
        self.summary.setText(f"Примеров: {len(self.examples)} · исключено лиц: {len(self.rejected)}")
        self.apply_button.setText("Применить фильтр" if self.examples else "Сбросить фильтр")
        self.undo_button.setEnabled(bool(self.history))
        self.restore_button.setText(f"Отменить исключения ({len(self.rejected)})")
        self.restore_button.setVisible(bool(self.rejected))
        self.next_button.setEnabled(bool(self.examples) and not self.loading)
        for face_id, (buttons, label) in self.decision_buttons.items():
            value = self.decisions.get(face_id)
            for name, button in buttons.items():
                button.setEnabled(name != value)
            label.setText({"yes": "Добавлен пример", "no": "Это лицо будет исключено", "skip": "Пропущено"}.get(value, ""))

    def decide(self, face_id, decision):
        if self.decisions.get(face_id) == decision:
            return
        self.remember()
        self.examples.pop(face_id, None)
        self.rejected.discard(face_id)
        self.skipped.discard(face_id)
        if decision == "yes":
            self.examples[face_id] = self.faces[face_id]
        elif decision == "no":
            self.rejected.add(face_id)
        else:
            self.skipped.add(face_id)
        self.decisions[face_id] = decision
        self.refresh()

    def remove_example(self, face_id):
        self.remember()
        self.examples.pop(face_id, None)
        self.skipped.add(face_id)
        self.decisions[face_id] = "skip"
        self.refresh()

    def restore_rejected(self):
        self.remember()
        for face_id in self.rejected:
            self.decisions.pop(face_id, None)
        self.rejected.clear()
        self.refresh()

    def undo(self):
        if self.history:
            if self.loading:
                self.backend.send(action="cancel_face_suggestions", id=self.request_id)
                self.request_id = None
                self.loading = False
                self.timeout.stop()
            self.examples, self.rejected, self.skipped, self.decisions = self.history.pop()
            self.refresh()

    def request_suggestions(self):
        if not self.examples:
            return
        if self.request_id:
            self.backend.send(action="cancel_face_suggestions", id=self.request_id)
        self.request_id = "face-review-" + uuid4().hex
        self.loading = True
        self.status.setText("Подбираю лица около границы сходства — среди слабых совпадений и сразу за пределами фильтра…")
        self.refresh()
        self.backend.send(action="face_suggestions", id=self.request_id, face_ids=list(self.examples),
                          filters=self.owner.filters(), excluded=sorted(self.seen | self.rejected | self.skipped))
        self.timeout.start(60000)

    def timed_out(self):
        self.backend.send(action="cancel_face_suggestions", id=self.request_id)
        self.request_id = None
        self.loading = False
        self.status.setText("Подбор занял слишком много времени. Можно повторить или применить выбранные примеры.")
        self.refresh()

    def on_event(self, event):
        if self.cleaned_up:
            return
        if event["type"] == "face_list":
            for face in event["faces"]:
                self.faces[face["id"]] = face
            return
        if event["type"] != "fatal" and (not self.request_id or event.get("id") != self.request_id):
            return
        if event["type"] == "face_suggestions":
            self.timeout.stop()
            self.request_id = None
            self.loading = False
            self.items = event["items"]
            self.seen.update(face["id"] for face in self.items)
            self.faces = dict(self.examples)
            self.faces.update((face["id"], face) for face in self.items)
            self.render_cards()
            self.status.setText("Сравните эти лица с примерами выше. Нажмите на портрет, чтобы открыть фотографию целиком." if self.items else
                                "Новых лиц около границы сходства в выбранных папках и периоде не найдено. Другой ракурс можно добавить из каталога.")
            self.refresh()
        elif event["type"] in {"error", "fatal"}:
            self.timeout.stop()
            self.request_id = None
            self.loading = False
            self.status.setText(event["message"])
            self.refresh()
            self.examples_panel.setExamples(list(self.examples.values()), invalid=event.get("invalid_face_ids", []))

    def render_cards(self):
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.decision_buttons.clear()
        for i, face in enumerate(self.items):
            card = QFrame()
            card.setObjectName("panel")
            layout = QVBoxLayout(card)
            portrait = QPushButton()
            portrait.setIcon(QIcon(face["thumbnail"]))
            portrait.setIconSize(QSize(100, 100))
            portrait.setFixedHeight(112)
            portrait.setToolTip("Открыть фотографию целиком")
            portrait.setAccessibleName("Открыть фотографию " + face["filename"])
            portrait.clicked.connect(lambda checked=False, key=face["id"]: self.open_photo(key))
            layout.addWidget(portrait)
            label = QLabel("Слабое совпадение" if face["inside_filter"] else "Чуть за границей поиска")
            label.setObjectName("section")
            layout.addWidget(label)
            name = QLabel(face["filename"])
            name.setTextFormat(Qt.PlainText)
            name.setToolTip(face["relative_path"])
            layout.addWidget(name)
            row = QHBoxLayout()
            buttons = {}
            for value, text in (("yes", "Тот же человек"), ("no", "Другой"), ("skip", "Пропустить")):
                button = QPushButton(text)
                button.setAutoDefault(False)
                button.setStyleSheet("padding: 6px 5px;")
                button.clicked.connect(lambda checked=False, key=face["id"], choice=value: self.decide(key, choice))
                buttons[value] = button
                row.addWidget(button)
            layout.addLayout(row)
            verdict = QLabel()
            verdict.setObjectName("muted")
            layout.addWidget(verdict)
            self.decision_buttons[face["id"]] = buttons, verdict
            self.grid.addWidget(card, i // 3, i % 3)

    def open_photo(self, face_id):
        from .ui import Viewer
        asset = next(item["asset"] for item in self.items if item["id"] == face_id)
        viewer = Viewer([asset], 0, self.owner.cfg, self, self.backend)
        viewer.personSelected.connect(lambda key: self.decide(key, "yes") if key in self.faces else None)
        viewer.exec()
        viewer.deleteLater()

    def done(self, result):
        if not self.cleaned_up:
            self.cleaned_up = True
            self.timeout.stop()
            if self.request_id:
                self.backend.send(action="cancel_face_suggestions", id=self.request_id)
            self.backend.event.disconnect(self.on_event)
        super().done(result)
