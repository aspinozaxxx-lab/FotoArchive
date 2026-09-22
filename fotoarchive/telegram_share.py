"""Prepare an original file in Telegram Desktop; never invoke its Send action."""
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from urllib.parse import urlencode

from PySide6.QtCore import QObject,QRunnable,QThreadPool,QTimer,Signal
from PySide6.QtWidgets import QHBoxLayout,QInputDialog,QMenu,QMessageBox,QPushButton,QToolButton,QWidget,QWidgetAction


def username(value):
    value=value.strip().removeprefix('https://t.me/').removeprefix('t.me/').lstrip('@').rstrip('/')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}',value):
        raise ValueError('Введите ник Telegram, например @username, без пробелов.')
    return value


class Contacts:
    def __init__(self,directory):self.path=Path(directory)/'telegram_contacts.json'
    def read(self):
        try:
            values=json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError,ValueError):return []
        items=[];seen=set()
        for item in values if isinstance(values,list) else []:
            if not isinstance(item,str):continue
            try:name=username(item)
            except ValueError:continue
            if name.lower() not in seen:items.append(name);seen.add(name.lower())
        return items
    def write(self,items):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temp=self.path.with_suffix('.tmp');temp.write_text(json.dumps(items,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(self.path)
    def add(self,value):
        name=username(value);items=self.read()
        if name.lower() not in {item.lower() for item in items}:items.append(name);self.write(items)
        return name
    def remove(self,value):self.write([item for item in self.read() if item.lower()!=value.lower()])


def telegram_executable():
    import psutil
    for process in psutil.process_iter(['name','exe']):
        if (process.info.get('name') or '').lower()=='telegram.exe' and process.info.get('exe'):
            return Path(process.info['exe'])
    candidates=[Path(os.environ.get('APPDATA',''))/'Telegram Desktop'/'Telegram.exe',
                Path(os.environ.get('LOCALAPPDATA',''))/'Telegram Desktop'/'Telegram.exe']
    for path in candidates:
        if path.is_file():return path
    raise RuntimeError('Установите и откройте Telegram Desktop, затем повторите попытку.')


class TelegramDraft:
    """UIA operates on verified controls of Telegram, without global keystrokes.

    Resolve the profile first to verify the public username without replacing
    existing draft text. No API credentials, bots or Telegram session files.
    """
    def __init__(self,stop=None):self.stop=stop or threading.Event()

    def wait(self,callback,timeout=12):
        deadline=time.monotonic()+timeout
        while not self.stop.is_set() and time.monotonic()<deadline:
            result=callback()
            if result:return result
            self.stop.wait(.2)
        labels={'profile':'открыть профиль контакта','message_button':'открыть чат',
                'attachment':'открыть вложения','document_or_picker':'выбрать документ',
                'picker':'найти окно выбора файла','prepared':'подтвердить вложение без сжатия'}
        raise RuntimeError('Не удалось '+labels.get(callback.__name__,'подготовить вложение')+'. Откройте Telegram и повторите попытку.')

    def prepare(self,path,contact):
        path=Path(path).resolve(strict=True)
        if not path.is_file():raise ValueError('Исходный файл недоступен.')
        contact=username(contact);exe=telegram_executable()
        # pywinauto selects its COM model before importing comtypes. Importing
        # comtypes first would initialize a fresh worker as STA implicitly.
        from pywinauto import Desktop
        import comtypes
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        try:
            import psutil
            desktop=Desktop(backend='uia')
            subprocess.Popen([str(exe),'--','tg://resolve?'+urlencode(dict(domain=contact,profile=''))],
                             creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            def windows():
                pids={p.pid for p in psutil.process_iter(['name']) if (p.info.get('name') or '').lower()=='telegram.exe'}
                return [window for window in desktop.windows(visible_only=True) if window.process_id() in pids]
            def profile():
                for window in windows():
                    for item in window.descendants():
                        identity=item.element_info.automation_id+' '+item.element_info.class_name
                        if 'Info::' not in identity and 'Profile' not in identity:continue
                        text=item.window_text().strip()
                        if text.lstrip('@').lower()==contact.lower():return window
                return None
            window=self.wait(profile,25)
            def message_button():
                buttons=[b for b in window.descendants(control_type='Button')
                    if b.window_text().strip().lower() in ('send message','message','написать сообщение','написать','сообщение')
                    and 'Info::' in (b.element_info.automation_id+' '+b.element_info.class_name)
                    and 'SendButton' not in (b.element_info.automation_id+' '+b.element_info.class_name)]
                return buttons[0] if len(buttons)==1 else None
            self.wait(message_button).click()
            def attachment():
                for current in windows():
                    for button in current.descendants(control_type='Button'):
                        if button.window_text().strip().lower() in ('add attachment','прикрепить','прикрепить файл','добавить вложение'):
                            return button
            self.wait(attachment).click()
            # Recent Desktop versions open an attachment menu first. Choosing
            # Document also starts media files in the no-compression mode.
            def document_or_picker():
                for current in windows():
                    if current.class_name()=='#32770':return current
                    for item in current.descendants(control_type='MenuItem'):
                        if item.window_text().strip().lower() in ('document','file','документ','файл'):
                            return item
            choice=self.wait(document_or_picker)
            if choice.class_name()!='#32770':choice.select()
            def picker():
                candidates=[]
                for current in windows():
                    candidates.extend([current]+current.descendants(control_type='Window'))
                matches={w.handle:w for w in candidates if w.class_name()=='#32770'}
                return next(iter(matches.values())) if len(matches)==1 else None
            dialog=self.wait(picker)
            edits=[edit for edit in dialog.descendants(control_type='Edit') if edit.element_info.automation_id in ('1148','1001')
                   or edit.window_text().lower() in ('file name:','имя файла:')]
            if len(edits)!=1:raise RuntimeError('Не найдено поле имени файла в Telegram.')
            edits[0].set_edit_text(str(path))
            buttons=[b for b in dialog.descendants(control_type='Button') if b.element_info.automation_id=='1']
            if len(buttons)!=1:raise RuntimeError('Не найдена кнопка открытия файла.')
            buttons[0].click()
            def prepared():
                for current in windows():
                    controls=current.descendants()
                    boxes=[c for c in controls if 'SendFilesBox' in c.element_info.automation_id or 'SendFilesBox' in c.element_info.class_name]
                    if not boxes:continue
                    checks=[c for c in controls if c.element_info.control_type=='CheckBox' and c.window_text().strip().lower() in
                            ('send as a document','send as documents','отправить как файл','отправить как файлы','отправить без сжатия')]
                    if checks:
                        if checks[0].get_toggle_state()!=1:checks[0].toggle()
                        return checks[0].get_toggle_state()==1
                    # Formats Telegram does not treat as media already use a
                    # document attachment and offer no compression switch.
                    if path.suffix.lower() not in ('.jpg','.jpeg','.png','.webp','.gif','.mp4','.mov','.mkv','.avi'):
                        return True
                return False
            self.wait(prepared,20)
        finally:comtypes.CoUninitialize()


class ShareSignals(QObject):
    finished=Signal(str)


class ShareJob(QRunnable):
    lock=threading.Lock()
    def __init__(self,path,contact,signals,stop):
        super().__init__();self.path,self.contact,self.signals,self.stop=path,contact,signals,stop
    def run(self):
        if not self.lock.acquire(blocking=False):
            self.signals.finished.emit('Дождитесь подготовки предыдущего вложения.');return
        try:
            TelegramDraft(self.stop).prepare(self.path,self.contact);error=''
        except Exception as exc:error=str(exc) or 'Не удалось подготовить вложение в Telegram.'
        finally:self.lock.release()
        self.signals.finished.emit(error)


class ShareButton(QToolButton):
    def __init__(self,cfg,asset_provider,parent=None):
        super().__init__(parent);self.contacts=Contacts(cfg.data_dir);self.asset_provider=asset_provider
        self.setText('Отправить');self.setToolTip('Подготовить оригинал в Telegram');self.setAccessibleName('Отправить в Telegram')
        self.menu=QMenu(self);self.setMenu(self.menu);self.setPopupMode(QToolButton.InstantPopup)
        self.menu.aboutToShow.connect(self.populate)
        self.hover=QTimer(self);self.hover.setSingleShot(True);self.hover.setInterval(180)
        self.hover.timeout.connect(lambda:self.showMenu() if self.underMouse() and self.isEnabled() else None)
        # The job owns its signals until completion. Closing a viewer disconnects
        # its slot without destroying an object still used by the worker.
        self.signals=ShareSignals();self.signals.finished.connect(self.finished)
        self.stop=threading.Event()
        if parent and hasattr(parent,'finished'):parent.finished.connect(lambda *_:self.stop.set())

    def enterEvent(self,event):self.hover.start();super().enterEvent(event)
    def leaveEvent(self,event):self.hover.stop();super().leaveEvent(event)

    def populate(self):
        self.menu.clear()
        for contact in self.contacts.read():
            row=QWidget();layout=QHBoxLayout(row);layout.setContentsMargins(4,2,4,2)
            select=QPushButton('@'+contact);select.setFlat(True)
            select.clicked.connect(lambda checked=False,name=contact:self.choose(name));layout.addWidget(select,1)
            remove=QToolButton();remove.setText('×');remove.setToolTip('Удалить @'+contact)
            remove.setAccessibleName('Удалить @'+contact);remove.clicked.connect(lambda checked=False,name=contact:self.remove(name));layout.addWidget(remove)
            action=QWidgetAction(self.menu);action.setDefaultWidget(row);self.menu.addAction(action)
        if self.contacts.read():self.menu.addSeparator()
        self.menu.addAction('Добавить контакт…',self.add)

    def add(self):
        value,ok=QInputDialog.getText(self,'Контакт Telegram','Ник Telegram:')
        if ok:
            try:self.contacts.add(value)
            except ValueError as exc:QMessageBox.information(self,'Telegram',str(exc))

    def remove(self,contact):
        self.contacts.remove(contact);self.menu.close()

    def choose(self,contact):
        self.menu.close();asset=self.asset_provider()
        if not asset:return
        self.stop.clear();self.setEnabled(False);self.setText('Подготовка…')
        QThreadPool.globalInstance().start(ShareJob(asset['path'],contact,self.signals,self.stop))

    def finished(self,error):
        self.setEnabled(True);self.setText('Отправить')
        if error and not self.stop.is_set():QMessageBox.information(self,'Telegram',error)
