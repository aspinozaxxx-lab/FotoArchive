"""Sharing preserves original bytes and never invokes the final Telegram Send."""
from pathlib import Path
from types import SimpleNamespace
import threading
import pytest
from fotoarchive.telegram_share import Contacts,TelegramDraft,username


def test_contact_add_remove_and_restart(tmp_path):
    contacts=Contacts(tmp_path)
    assert contacts.add(' @Example_User ')== 'Example_User'
    contacts.add('https://t.me/example_user');contacts.add('@second_user')
    assert Contacts(tmp_path).read()==['Example_User','second_user']
    contacts.remove('EXAMPLE_USER')
    assert Contacts(tmp_path).read()==['second_user']
    for value in ('bad name','https://evil.invalid/contact','ab','name?text=send'):
        with pytest.raises(ValueError):username(value)


@pytest.mark.parametrize('suffix',['.png','.jpg','.mp4','.nef'])
def test_original_attached_and_send_is_never_invoked(tmp_path,monkeypatch,suffix):
    import pywinauto
    import psutil
    import fotoarchive.telegram_share as share
    source=tmp_path/('original'+suffix);source.write_bytes(b'unchanged original bytes')
    stage=['profile'];actions=[];values=[]
    class Control:
        def __init__(self,name='',kind='Button',identity='',next_stage=None):
            self.name=name;self.element_info=SimpleNamespace(automation_id=identity,class_name=identity,control_type=kind)
            self.next_stage=next_stage;self.handle=id(self)
        def window_text(self):return self.name
        def class_name(self):return self.element_info.class_name
        def click(self):
            assert self.name!='Send';actions.append(self.name)
            if self.next_stage:stage[0]=self.next_stage
        def select(self):self.click()
        def set_edit_text(self,value):values.append(value)
        def get_toggle_state(self):return 1
    edit=Control(kind='Edit',identity='1148');opened=Control('Open',identity='1',next_stage='prepared')
    class Picker(Control):
        def __init__(self):super().__init__('Choose Files','Window','#32770')
        def descendants(self,control_type=None):return [c for c in [edit,opened] if not control_type or c.element_info.control_type==control_type]
    picker=Picker()
    class Window(Control):
        def process_id(self):return 101
        def descendants(self,control_type=None):
            controls={
                'profile':[Control('Example_User','Text','Info::Profile'),
                           Control('Send message',identity='Info::Profile::Cover',next_stage='chat'),
                           Control('Send message',identity='HistoryWidget.Ui::SendButton',next_stage='WRONG')],
                'chat':[Control('Add attachment',next_stage='menu')],
                'menu':[Control('Document','MenuItem',next_stage='picker')],
                'picker':[picker],
                'prepared':[Control(kind='Group',identity='SendFilesBox'),Control('Send as a document','CheckBox'),Control('Send')],
            }[stage[0]]
            return [c for c in controls if not control_type or c.element_info.control_type==control_type]
    window=Window()
    monkeypatch.setattr(pywinauto,'Desktop',lambda **_:SimpleNamespace(windows=lambda **_:[window]))
    monkeypatch.setattr(psutil,'process_iter',lambda *_:[SimpleNamespace(pid=101,info={'name':'Telegram.exe'})])
    monkeypatch.setattr(share,'telegram_executable',lambda:Path('Telegram.exe'))
    launches=[];monkeypatch.setattr(share.subprocess,'Popen',lambda argv,**kw:launches.append(argv))
    # As in the application, COM work is isolated from the Qt GUI apartment.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(TelegramDraft().prepare,source,'Example_User').result(timeout=5)
    assert actions==['Send message','Add attachment','Document','Open']
    assert values==[str(source.resolve())] and source.read_bytes()==b'unchanged original bytes'
    assert '?domain=Example_User&profile=' in launches[0][-1]
    assert 'text=' not in launches[0][-1]


def test_cancelled_preparation_stops_before_controls():
    stopped=threading.Event();stopped.set()
    def not_called():raise AssertionError('Must stop')
    with pytest.raises(RuntimeError):TelegramDraft(stopped).wait(not_called)
