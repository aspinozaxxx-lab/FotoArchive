import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QListView
from fotoarchive.config import Settings
from fotoarchive.ui import MainWindow
from test_ui import FakeBackend


def photo(i):
    return dict(id=i, version=1, filename=f'{i}.jpg', relative_path=f'2003/{i}.jpg',
                width=[800, 400, 1200][i % 3], height=600, extension='.jpg', size=100,
                captured_at='2003-05-01T10:00:00', media_kind='photo')


@pytest.mark.parametrize('mode', [0, 1])
@pytest.mark.parametrize('stack_row', [24, 190])
def test_stack_keeps_cover_at_same_pixel_after_expand_and_collapse(qtbot, tmp_path, mode, stack_row):
    w = MainWindow(Settings(data_dir=tmp_path), FakeBackend())
    qtbot.addWidget(w)
    w.resize(1300, 900)
    w.show()
    w.layout_combo.setCurrentIndex(mode)
    items = [photo(i+1) for i in range(400)]
    cover = items[stack_row] | dict(stack_count=4, stack_key='b:group', stack_expanded=False)
    items[stack_row] = cover
    w.on_event(dict(type='results', id=w.request_id, items=items[:200], total=403, page_total=400))
    w.model.accept_page(200, items[200:], 400, False)
    qtbot.wait(100)
    w.gallery.scrollTo(w.model.index(stack_row), QListView.PositionAtCenter)
    qtbot.wait(40)

    for expanded in (True, False, True, False):
        rect = w.gallery.visualRect(w.model.index(stack_row))
        assert rect.intersects(w.gallery.viewport().rect())
        qtbot.mouseClick(w.gallery.viewport(), Qt.LeftButton, pos=rect.topLeft()+QPoint(25, 22))
        command = next(c for c in reversed(w.backend.sent) if c['action']=='browse')
        anchor = command['refresh_anchor']
        after = items[:stack_row]+[cover | dict(stack_expanded=expanded)]+(
            [photo(1001+i) | dict(stack_key='b:group', stack_count=4, stack_expanded=True) for i in range(3)] if expanded else [])+items[stack_row+1:]
        row = next(i for i,a in enumerate(after) if a['id']==anchor['asset_id'])
        restore = dict(row=row, selected_row=stack_row, offset_y=anchor['offset_y'], loaded_count=len(after))
        offset = row//200*200
        w.on_event(dict(type='results', id=w.request_id, items=after[offset:offset+200],
                        total=403, page_total=len(after), offset=offset, restore=restore))
        qtbot.waitUntil(lambda:w.gallery.stack_motion is not None and w.gallery.stack_motion.new is not None)
        assert w.gallery.stack_motion.isVisible()
        assert len(w.gallery.stack_motion.old)<=120 and len(w.gallery.stack_motion.new)<=120
        qtbot.wait(350)
        assert w.gallery.visualRect(w.model.index(stack_row)).top()==pytest.approx(rect.top(), abs=1)
        # Supply missing pages as the real background reader would.
        for start in range(0,len(after),200):
            w.model.accept_page(start,after[start:start+200],len(after),False)
        qtbot.wait(60)
        assert w.gallery.visualRect(w.model.index(stack_row)).top()==pytest.approx(rect.top(), abs=1)
    w.close()


def test_filter_chip_edits_without_removing_and_empty_actions_remove_one(qtbot,tmp_path):
    from PySide6.QtWidgets import QPushButton
    w=MainWindow(Settings(data_dir=tmp_path),FakeBackend())
    qtbot.addWidget(w)
    w.show()
    w.format_combo.addItem('JPG','.jpg')
    w.format_combo.setCurrentIndex(1)
    w.unknown.setChecked(True)
    w.search()
    w.on_event(dict(type='results',id=w.request_id,items=[],total=0,page_total=0))
    buttons=w.chips_panel.findChildren(QPushButton)
    edit=next(b for b in buttons if b.accessibleName()=='Изменить фильтр: Без даты')
    qtbot.mouseClick(edit,Qt.LeftButton)
    assert w.unknown.isChecked() and w.filters()['extension']=='.jpg'
    assert 'снять один' in w.empty_label.text()
    remove=next(b for b in w.empty_actions.findChildren(QPushButton) if b.text()=='Снять: Без даты')
    qtbot.mouseClick(remove,Qt.LeftButton)
    assert not w.unknown.isChecked() and w.filters()['extension']=='.jpg'
    w.close()


def test_sparse_empty_search_offers_continuation_not_false_no_matches(qtbot,tmp_path):
    from PySide6.QtWidgets import QPushButton
    w=MainWindow(Settings(data_dir=tmp_path),FakeBackend())
    qtbot.addWidget(w)
    w.on_event(dict(type='results',id=0,items=[],total=0,page_total=0,has_more=True,semantic=True))
    assert 'части кандидатов' in w.empty_label.text()
    next(b for b in w.empty_actions.findChildren(QPushButton) if b.text()=='Продолжить поиск').click()
    assert w.backend.sent[-1]['action']=='search_page' and w.backend.sent[-1]['offset']==0
    w.close()


def test_named_chip_refines_only_that_person_and_preserves_group(qtbot,tmp_path,monkeypatch):
    from collections import OrderedDict
    from fotoarchive.face_review import FaceReviewDialog
    from PySide6.QtWidgets import QDialog
    w=MainWindow(Settings(data_dir=tmp_path),FakeBackend())
    qtbot.addWidget(w)
    first=dict(id='first',name='Первый',examples=['one'],rejected=[])
    second=dict(id='second',name='Второй',examples=['two'],rejected=[])
    w.selected_people=[first,second]
    w.people_mode='all'
    def edit(dialog):
        assert list(dialog.examples)==['two']
        dialog.examples=OrderedDict(two={'id':'two'},angle={'id':'angle'})
        return QDialog.Accepted
    monkeypatch.setattr(FaceReviewDialog,'exec',edit)
    w.edit_person_filter('second')
    request=next(c for c in reversed(w.backend.sent) if c['action']=='face_search')
    assert request['people_mode']=='all'
    assert request['people'][0]['examples']==['one']
    assert request['people'][1]['examples']==['two','angle']
    w.close()


def test_video_card_opens_clicked_frame_without_other_frame_evidence(qtbot,tmp_path,monkeypatch):
    from types import SimpleNamespace
    from PySide6.QtGui import QFontMetrics
    opened=[]
    class Player:
        personSelected=SimpleNamespace(connect=lambda *_:None)
        def __init__(self,items,position,*args,initial_asset=None):
            opened.append(initial_asset)
            assert items is w.model and position==0
        def exec(self): pass
        def deleteLater(self): pass
    monkeypatch.setattr('fotoarchive.ui.Viewer',Player)
    w=MainWindow(Settings(data_dir=tmp_path),FakeBackend())
    qtbot.addWidget(w)
    w.show()
    moments=[dict(unit_id='1:frame:10000',timestamp_ms=10000,thumbnail='a.webp'),
             dict(unit_id='1:frame:20000',timestamp_ms=20000,thumbnail='b.webp')]
    asset=photo(1)|dict(media_kind='video',matched_moments=moments,moment_count=8,duration_ms=30000,
        unit_id=moments[0]['unit_id'],timestamp_ms=10000,description='first frame',verification={'verdict':'yes'})
    w.on_event(dict(type='results',id=0,items=[asset],total=1,page_total=1))
    qtbot.wait(30)
    rect=w.gallery.visualRect(w.model.index(0)).adjusted(2,2,-2,-2)
    targets=w.delegate.moment_targets(rect,asset,QFontMetrics(w.gallery.font()))
    qtbot.mouseClick(w.gallery.viewport(),Qt.LeftButton,pos=targets[1][0].center())
    assert opened[0]['timestamp_ms']==20000 and opened[0]['unit_id']=='1:frame:20000'
    assert opened[0]['thumbnail']=='b.webp' and 'verification' not in opened[0] and 'description' not in opened[0]
    assert asset['timestamp_ms']==10000
    w.close()


def test_uniform_full_frame_shows_uncropped_portrait(qtbot,tmp_path):
    from PySide6.QtGui import QPixmap,QColor,QPainter
    from PySide6.QtWidgets import QStyleOptionViewItem
    from PySide6.QtCore import QRect
    w=MainWindow(Settings(data_dir=tmp_path),FakeBackend())
    qtbot.addWidget(w)
    asset=photo(1)|dict(thumbnail='test',width=100,height=200)
    pixmap=QPixmap(100,200); pixmap.fill(QColor('red'))
    w.model.cache['test']=pixmap
    w.model.set_items([asset])
    w.layout_combo.setCurrentIndex(2)
    option=QStyleOptionViewItem();option.initFrom(w.gallery);option.rect=QRect(0,0,220,220)
    output=QPixmap(220,220);output.fill(Qt.white)
    painter=QPainter(output);w.delegate.paint(painter,option,w.model.index(0));painter.end()
    image=output.toImage()
    assert image.pixelColor(110,10)==QColor('red') and image.pixelColor(10,110)!=QColor('red')
    assert w.gallery.uniformItemSizes()
    w.close()
