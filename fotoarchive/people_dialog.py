"""Explicit edits of saved reference sets; no automatic identity assignments."""
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QDialog,QHBoxLayout,QVBoxLayout,QLabel,QPushButton,QListWidget,
    QListWidgetItem,QAbstractItemView,QInputDialog)


class PeopleDialog(QDialog):
    def __init__(self,owner):
        super().__init__(owner)
        self.owner = owner
        self.backend = owner.backend
        self.setWindowTitle('Люди в каталоге')
        self.resize(860,590)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('Сохранённые люди и подтверждённые примеры разных ракурсов.'))
        row = QHBoxLayout()
        self.people = QListWidget()
        self.people.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.people.setIconSize(QSize(48,48))
        row.addWidget(self.people,1)
        self.faces = QListWidget()
        self.faces.setViewMode(QListWidget.IconMode)
        self.faces.setIconSize(QSize(76,76))
        self.faces.setSelectionMode(QAbstractItemView.ExtendedSelection)
        row.addWidget(self.faces,2)
        layout.addLayout(row,1)
        self.message = QLabel('Выберите человека. Ctrl — несколько людей или примеров.')
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        actions = QHBoxLayout()
        for label,callback in [('Переименовать',self.rename),('Объединить людей',self.merge),
                               ('Удалить профиль',self.remove),('Обложка',self.cover),('Отделить примеры',self.split)]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            actions.addWidget(button)
        layout.addLayout(actions)
        bottom = QHBoxLayout()
        edit = QPushButton('Уточнить выбранного человека')
        edit.clicked.connect(self.refine)
        bottom.addWidget(edit)
        bottom.addStretch()
        close = QPushButton('Готово')
        close.clicked.connect(self.accept)
        bottom.addWidget(close)
        layout.addLayout(bottom)
        self.people.itemSelectionChanged.connect(self.show_faces)
        self.backend.event.connect(self.event_received)
        self.refresh()

    def chosen(self):
        ids = {item.data(Qt.UserRole) for item in self.people.selectedItems()}
        return [p for p in self.owner.people_records if p['id'] in ids]

    def refresh(self):
        selected = {item.data(Qt.UserRole) for item in self.people.selectedItems()}
        self.people.blockSignals(True)
        self.people.clear()
        for person in self.owner.people_records:
            item = QListWidgetItem(QIcon(person['thumbnail']),person['name'])
            item.setData(Qt.UserRole,person['id'])
            self.people.addItem(item)
            item.setSelected(person['id'] in selected)
        self.people.blockSignals(False)
        self.show_faces()

    def show_faces(self):
        self.faces.clear()
        people = self.chosen()
        for person in people:
            valid = {f['id']:f for f in person['faces']}
            for key in person['examples']:
                face = valid.get(key,{})
                item = QListWidgetItem(QIcon(face.get('thumbnail','')),'Обложка' if key==person['cover'] else '' if face else 'Недоступен')
                item.setData(Qt.UserRole,key)
                item.setToolTip(face.get('relative_path','Исходный кадр недоступен или изменён'))
                self.faces.addItem(item)

    def save(self,person,**changes):
        values = dict(person_id=person['id'],name=person['name'],examples=person['examples'],rejected=person['rejected'],cover=person['cover'])
        values.update(changes)
        self.backend.send(action='library_save_person',**values)

    def rename(self):
        people = self.chosen()
        if len(people)!=1:
            self.message.setText('Выберите одного человека')
            return
        name,ok = QInputDialog.getText(self,'Имя человека','Имя',text=people[0]['name'])
        if ok and name.strip():
            self.save(people[0],name=name)

    def merge(self):
        people = self.chosen()
        if len(people)<2:
            self.message.setText('Выберите несколько профилей одного человека с Ctrl')
            return
        self.backend.send(action='library_merge_people',person_ids=[p['id'] for p in people])

    def remove(self):
        for person in self.chosen():
            self.backend.send(action='library_delete_person',person_id=person['id'])

    def cover(self):
        people,faces = self.chosen(),self.faces.selectedItems()
        if len(people)==len(faces)==1:
            self.save(people[0],cover=faces[0].data(Qt.UserRole))
        else:
            self.message.setText('Выберите одного человека и один пример для обложки')

    def split(self):
        people = self.chosen()
        keys = [item.data(Qt.UserRole) for item in self.faces.selectedItems()]
        if len(people)!=1 or not keys or len(keys)>=len(people[0]['examples']):
            self.message.setText('Выберите часть примеров одного профиля для нового человека')
            return
        name,ok = QInputDialog.getText(self,'Отделить человека','Имя нового человека')
        if ok and name.strip():
            self.backend.send(action='library_split_person',person_id=people[0]['id'],examples=keys,name=name)

    def refine(self):
        people = self.chosen()
        if len(people)!=1:
            self.message.setText('Выберите одного человека')
            return
        for i in range(self.owner.people_list.count()):
            item = self.owner.people_list.item(i)
            item.setSelected(item.data(Qt.UserRole)==people[0]['id'])
        self.owner.choose_people()
        self.accept()
        if self.owner.refine_person():
            self.owner.save_person()

    def event_received(self,event):
        if event['type'] in ('library_save_person','library_merge_people','library_delete_person','library_split_person'):
            self.refresh()
        elif event['type']=='library_error':
            self.message.setText(event['message'])

    def done(self,result):
        try:
            self.backend.event.disconnect(self.event_received)
        except RuntimeError:
            pass
        super().done(result)
