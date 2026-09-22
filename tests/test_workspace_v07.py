import json
from dataclasses import replace
from pathlib import Path
import numpy as np
import pytest
from PySide6.QtCore import Qt, QPoint
from fotoarchive.catalog import Catalog,Filters
from fotoarchive.config import Settings
from fotoarchive.library import Library
from fotoarchive.search_session import SearchSession
from fotoarchive.browse_reader import BrowseViews
from fotoarchive.ui import MainWindow
from test_ui import FakeBackend


@pytest.fixture
def catalog(tmp_path):
    cfg = Settings(data_dir=tmp_path/'data',root=tmp_path/'source')
    cat = Catalog(cfg)
    records = [(1,'2003','a.jpg','2003-05-01T10:00:00','A','photo'),
               (2,'2003','b.jpg','2003-05-01T10:00:01','A','photo'),
               (3,'2003','c.jpg','2003-05-01T10:00:03','A','photo'),
               (4,'2003','d.jpg','2003-05-01T10:00:08','A','photo'),
               (5,'2003','e.jpg',None,'A','photo'),
               (6,'2003','f.jpg',None,'A','photo'),
               (7,'2003','g.mp4','2003-05-01T10:00:01','A','video'),
               (8,'2003','h.jpg','2003-05-01T10:00:01','B','photo'),
               (9,'2004/sub','IMG_01.CR2','2004-05-01T10:00:00','A','photo'),
               (10,'2004/sub/web','IMG_01.CR2.jpg','2004-05-01T10:00:00','A','photo')]
    with cat.db:
        for i,folder,name,captured,camera,kind in records:
            cat.db.execute('''INSERT INTO assets(id,source_id,relative_path,path_key,path,folder,filename,extension,size,mtime_ns,
                metadata_ready,width,height,pixels,captured_at,camera,media_kind,updated_at)
                VALUES(?,?,?,?,?,?,?,?,1,0,1,800,600,480000,?,?,?,0)''',
                (i,cat.source_id,folder+'/'+name,str(i),str(i),folder,name,Path(name).suffix.lower(),captured,camera,kind))
    yield cat
    cat.close()


def session(cat,filters=Filters(),**options):
    return SearchSession(cat,None,1,filters,'',None,[],mode='browse',presentation={'stacks':True,'seconds':2,'versions':True,**options})


def test_stacks_keep_every_file_and_unknown_dates_separate(catalog):
    s = session(catalog)
    page = s.page()
    assert s.total==10 and page['page_total']==7
    assert catalog.db.execute('SELECT count(*) FROM active_search').fetchone()[0]==10
    burst = next(a for a in page['items'] if a['stack_count']==3)
    assert burst['stack_key']=='b:1'
    assert {a['id'] for a in page['items']} >= {5,6,7,8}
    s.configure_presentation(dict(stacks=True,seconds=2,versions=True,expanded=['b:1']))
    expanded = s.page()
    assert expanded['page_total']==9
    assert {a['id'] for a in expanded['items']} >= {1,2,3}
    assert catalog.db.execute('SELECT count(*) FROM assets').fetchone()[0]==10


def test_search_history_uses_independent_read_connections_and_preserves_checks(catalog):
    from fotoarchive.search_history import SearchHistory
    history=SearchHistory()
    command=dict(action='search',id=1,query='люди',presentation={'stacks':True,'seconds':2})
    reader=Catalog.open_reader(catalog.cfg)
    first=SearchSession(reader,None,1,Filters(),'люди',None,['люди'],presentation=command['presentation'])
    first.mark(reader.get(1),{'verdict':'yes','checks':[]})
    first.page()
    history.put(command,first)
    for n in range(2,5):
        other=Catalog.open_reader(catalog.cfg)
        history.put(command|{'id':n,'query':str(n)},SearchSession(other,None,n,Filters(),str(n),None,[]))
    restored=history.get(command|{'id':5,'refresh_anchor':{'asset_id':2}})
    assert restored is first and restored.request_id==5
    assert restored.counts()['checked']==1
    assert restored.restore_position({'asset_id':2})['row']>=0
    history.close()


def test_sparse_search_yields_before_exhausting_whole_archive(catalog):
    class Index:
        calls=0
        def candidates(self,*args,**kwargs):
            self.calls+=1
            return [catalog.get(1)]
    index=Index()
    s=SearchSession(catalog,index,1,Filters(),'query',np.ones(768),[],exclude_id=1)
    page=s.page(expand=True)
    assert index.calls==5 and page['has_more'] and page['items']==[]


def test_browse_cancel_discards_partial_view_and_rebuilds(catalog):
    views=BrowseViews(catalog.cfg)
    command=dict(id=1,filters={},presentation={'stacks':True,'seconds':2})
    reader,first,_=views.select(command,lambda:False)
    first.page()
    views.discard(reader)
    reader,second,cached=views.select(command|{'id':2},lambda:False)
    assert not cached and second is not first and second.page()['page_total']==7
    views.close()


def test_pending_profile_write_finishes_when_window_closes(catalog):
    from fotoarchive.library import LibraryReader
    events=[]
    reader=LibraryReader(catalog.cfg,events.append)
    reader.enable()
    reader.send(dict(action='library_save_person',name='Профиль',examples=['face']))
    reader.close()
    assert not reader.thread.is_alive()
    assert Library(catalog).people()[0]['name']=='Профиль'


def test_filters_match_noncover_and_restore_under_collapsed_stack(catalog):
    s = session(catalog,Filters(date_from='2003-05-01',date_to='2003-05-01'))
    assert s.restore_position({'asset_id':2,'selected_id':2})['row']>=0
    catalog.db.execute("UPDATE assets SET extension='.bmp' WHERE id=2")
    catalog.db.commit()
    s = session(catalog,Filters(extension='.bmp'))
    page = s.page()
    assert [a['id'] for a in page['items']]==[2]
    assert page['items'][0]['stack_count']==1
    assert page['items'][0]['stack_total']==3


def test_versions_unstack_cover_and_originals_unchanged(catalog):
    before = [tuple(r) for r in catalog.db.execute('SELECT id,path,size,mtime_ns,version FROM assets')]
    s = session(catalog,seconds=0)
    group = next(a for a in s.page()['items'] if a['stack_count']>1)
    assert group['stack_count']==2
    Library(catalog).command(dict(action='library_stack_cover',stack_key=group['stack_key'],asset_id=9))
    assert next(a for a in session(catalog,seconds=0).page()['items'] if a['stack_count']>1)['id']==9
    Library(catalog).command(dict(action='library_unstack',asset_id=9))
    assert session(catalog,seconds=0).page()['page_total']==10
    assert before==[tuple(r) for r in catalog.db.execute('SELECT id,path,size,mtime_ns,version FROM assets')]


def test_stacks_paginate_without_lost_or_duplicate_members(catalog):
    s = session(catalog,expanded=['b:1','b:9'])
    rows=[]
    for offset in range(0,10,2):
        rows += s.page(offset=offset,limit=2)['items']
    assert len(rows)==len({r['id'] for r in rows})==10


def test_profile_save_merge_split_delete_is_transactional(catalog):
    lib = Library(catalog)
    first = lib.save_person(dict(name='Иван',examples=['a','b'],rejected=['x']))
    second = lib.save_person(dict(name='Иван 2003',examples=['b','c'],rejected=['a']))
    lib.merge_people([first,second])
    person = lib.people()[0]
    assert person['examples']==['a','b','c'] and person['rejected']==['x']
    new = lib.split_person(dict(person_id=first,examples=['c'],name='Другой'))
    assert len(lib.people())==2
    assert {tuple(p['examples']) for p in lib.people()}=={('a','b'),('c',)}
    with pytest.raises(ValueError):
        lib.split_person(dict(person_id=first,examples=['a'],name=''))
    assert len(lib.people())==2
    lib.command(dict(action='library_delete_person',person_id=new))
    assert len(lib.people())==1


def test_places_unicode_manual_gps_rectangle_and_folder_scope(catalog):
    lib = Library(catalog)
    with catalog.db:
        catalog.db.execute("UPDATE assets SET latitude=55.75,longitude=37.62,geo_text='Москва, Россия' WHERE id=1")
    assert catalog.browse(Filters(place='мОСКвА'))[1]==1
    lib.set_place(dict(asset_id=2,version=1,name='Дом',latitude=55.76,longitude=37.6))
    assert catalog.get(2)['latitude'] is None and catalog.get(2)['user_latitude']==55.76
    assert catalog.browse(Filters(geo_bounds='37,55,38,56'))[1]==2
    assert catalog.browse(Filters(folder='2004/sub',include_subfolders=False))[1]==1
    assert catalog.browse(Filters(folder='2004/sub'))[1]==2
    assert sum(point['count'] for point in lib.places(Filters())['points'])==2
    with pytest.raises(ValueError):
        lib.set_place(dict(asset_id=3,version=1,name='',latitude=float('nan'),longitude=0))


def test_browse_cache_expansion_sort_and_anchor(catalog):
    views = BrowseViews(catalog.cfg)
    command = dict(action='browse',id=1,filters={},presentation=dict(stacks=True,seconds=2,versions=True,sort='oldest'))
    cat,s,cached = views.select(command,lambda:False)
    s.page()
    _,same,cached = views.select(command|{'id':2},lambda:False)
    assert same is s and cached
    _,same,cached = views.select(command|{'id':3,'presentation':command['presentation']|{'expanded':['b:1']}},lambda:False)
    assert cached and same.page()['page_total']==9
    _,same,cached = views.select(command|{'id':4,'refresh_anchor':{'asset_id':2}},lambda:False)
    assert cached
    views.close()


def test_navigation_history_persistence_and_visible_filter_reset(qtbot,tmp_path):
    cfg=Settings(data_dir=tmp_path)
    backend=FakeBackend()
    w=MainWindow(cfg,backend)
    qtbot.addWidget(w)
    w.folder_combo.addItem('2004','2004')
    w.folder_combo.setCurrentIndex(1)
    w.search()
    assert backend.sent[-1]['filters']['folder']=='2004'
    w.media_combo.setCurrentIndex(2)
    assert backend.sent[-1]['filters']['media_kind']=='video'
    w.navigate_history(-1)
    assert backend.sent[-1]['filters']['media_kind']=='' and backend.sent[-1]['filters']['folder']=='2004'
    w.navigate_history(1)
    assert backend.sent[-1]['filters']['media_kind']=='video'
    w.close()
    second=MainWindow(cfg,FakeBackend())
    qtbot.addWidget(second)
    assert second.filters()['folder']=='2004' and second.filters()['media_kind']=='video'
    assert not second.details_toggle.isChecked()
    second.reset_filters()
    assert not second.filters()['folder'] and not second.filters()['media_kind']
    second.close()


def test_saved_query_and_video_reference_restore_without_stale_face_filter(qtbot,tmp_path):
    cfg=Settings(data_dir=tmp_path)
    w=MainWindow(cfg,FakeBackend())
    qtbot.addWidget(w)
    w.face_examples={'face':{'id':'face'}}
    w.search_box.setText('Закат на море')
    w.reference=None
    w.complex_check.setChecked(True)
    w.close()
    backend=FakeBackend()
    second=MainWindow(cfg,backend)
    qtbot.addWidget(second)
    assert backend.sent[-1]['action']=='search' and backend.sent[-1]['query']=='Закат на море'
    assert backend.sent[-1]['complex']
    second.search_box.clear()
    second.reference=('similar','asset_id',7)
    second.reference_unit='7:frame:1000'
    second.close()
    backend=FakeBackend()
    third=MainWindow(cfg,backend)
    qtbot.addWidget(third)
    assert backend.sent[-1]['action']=='similar' and backend.sent[-1]['unit_id']=='7:frame:1000'
    third.close()


def test_map_keeps_aspect_ratio_and_coordinate_roundtrip(qtbot):
    from fotoarchive.map_view import MapView
    w=MapView()
    qtbot.addWidget(w)
    w.resize(1000,250)
    rect=w.map_rect()
    assert rect.width()/rect.height()==pytest.approx(2)
    point=w.pixel(37,55)
    lon,lat=w.coordinate(point)
    assert (lon,lat)==pytest.approx((37,55))
    w.set_points([dict(longitude=37,latitude=55,count=1)])
    assert w.bounds[0]<37<w.bounds[2] and w.bounds[1]<55<w.bounds[3]
    w.reset()
    assert w.bounds==(-180,-90,180,90)


def test_verification_does_not_replace_stack_count_with_file_count(qtbot,tmp_path):
    w=MainWindow(Settings(data_dir=tmp_path),FakeBackend())
    qtbot.addWidget(w)
    asset=dict(id=1,version=1,filename='a.jpg',relative_path='a.jpg',extension='.jpg',size=100,
               width=400,height=300,stack_count=3,stack_key='b:1')
    w.on_event(dict(type='results',id=0,items=[asset],total=3,page_total=1,conditions=['Люди'],semantic=True))
    w.on_event(dict(type='verified',id=0,asset=asset,result={'verdict':'yes'},
        groups={'':3,'yes':1,'no':0,'uncertain':0,'pending':2},has_more=False,checked=1,candidates=3,
        presentation_total=1,presentation_verdict=''))
    assert w.model.known_total==1 and w.model.rowCount()==1
    w.close()


def test_full_bleed_thumbnail_tooltip_zoom_and_stack_click(qtbot,tmp_path):
    w=MainWindow(Settings(data_dir=tmp_path),FakeBackend())
    qtbot.addWidget(w)
    w.show()
    asset=dict(id=1,version=1,filename='test.jpg',relative_path='2003/test.jpg',width=800,height=600,
        extension='.jpg',size=10,captured_at='2002-05-01T10:00:01',stack_key='b:1',stack_count=3)
    w.on_event(dict(type='results',id=0,items=[asset],total=3,page_total=1))
    tooltip=w.model.index(0).data(Qt.ToolTipRole)
    assert '01.05.2002' in tooltip and 'test.jpg' in tooltip
    qtbot.waitUntil(lambda:w.gallery.visualRect(w.model.index(0)).isValid())
    rectangle=w.gallery.visualRect(w.model.index(0))
    qtbot.mouseClick(w.gallery.viewport(),Qt.LeftButton,pos=rectangle.topLeft()+QPoint(25,22))
    assert 'b:1' in w.expanded_stacks
    assert w.backend.sent[-1]['presentation']['expanded']==['b:1']
    w.thumbnail_size.setValue(300)
    assert w.delegate.edge==300
    w.close()


def test_map_and_delayed_events_are_scoped(qtbot,tmp_path):
    w=MainWindow(Settings(data_dir=tmp_path),FakeBackend())
    qtbot.addWidget(w)
    w.map_toggle.setChecked(True)
    serial=w.map_serial
    w.on_event(dict(type='library_places',serial=serial-1,points=[dict(latitude=1,longitude=2,count=1)],places=[]))
    assert not w.map_widget.points
    w.on_event(dict(type='library_places',serial=serial,points=[dict(latitude=1,longitude=2,count=1)],places=[]))
    assert len(w.map_widget.points)==1
    w.select_map_bounds('10,20,30,40')
    assert w.backend.sent[-1]['filters']['geo_bounds']=='10,20,30,40'
    w.close()


def test_multiple_people_need_distinct_faces_and_respect_rejections(catalog):
    from PIL import Image
    from fotoarchive.face_search import matches_people
    portrait=Image.new('RGB',(112,112),'gray')
    first=np.eye(128,dtype=np.float32)[0]
    second=np.eye(128,dtype=np.float32)[1]
    def record(vector):
        return dict(box=[.1,.1,.4,.4],landmarks=[[.2,.2]]*5,confidence=.9,vector=vector,portrait=portrait)
    catalog.complete_faces(dict(asset_id=1,file_version=1),[record(first),record(second)])
    catalog.complete_faces(dict(asset_id=2,file_version=1),[record(first)])
    groups=[dict(vectors=[first],rejected=[]),dict(vectors=[second],rejected=[])]
    assert matches_people(catalog,1,None,groups)
    assert not matches_people(catalog,2,None,groups)
    assert matches_people(catalog,2,None,groups,require_all=False)
    assert not matches_people(catalog,2,None,[groups[0],groups[0]])
    key=catalog.faces_for(2)[0]['id']
    assert not matches_people(catalog,2,None,[dict(vectors=[first],rejected=[key]),groups[1]],require_all=False)


def test_geographic_filters_before_vector_top_k(catalog):
    from fotoarchive.search import SearchIndex
    from fotoarchive.config import EMBED_VERSION
    index=SearchIndex(catalog)
    query=np.eye(768,dtype=np.float32)[0]
    rows=[]
    for number in (1,2):
        asset=catalog.get(number)
        base={name:asset[name] for name in ('version','folder','captured_at','camera','extension','orientation','width','height','pixels','size','present','metadata_ready')}
        vector=query.copy() if number==2 else np.eye(768,dtype=np.float32)[1]
        rows.append(base|dict(unit_id=f'{number}:image',asset_id=number,model_version=EMBED_VERSION,vector=vector.tolist(),search_text=''))
    index.table.add(rows)
    with catalog.db:
        catalog.db.execute('UPDATE assets SET latitude=55,longitude=37 WHERE id=1')
    assert index.candidates(query,'',Filters(),limit=1)[0]['id']==2
    assert index.candidates(query,'',Filters(geo_bounds='36,54,38,56'),limit=1)[0]['id']==1
    index.close()


def test_video_results_keep_all_discovered_moments_in_one_card(catalog):
    with catalog.db:
        for n in range(16):
            key=f'7:frame:{n*10000}'
            catalog.db.execute("INSERT INTO units(id,asset_id,kind,timestamp_ms) VALUES(?,7,'frame',?)",(key,n*10000))
            catalog.db.execute('INSERT INTO unit_details(unit_id,file_version,thumbnail) VALUES(?,1,?)',(key,'missing.webp'))
    class Index:
        def candidates(self,*args,**kwargs):
            if kwargs['offset']:
                return []
            return [catalog.get(7)|dict(matched_units=[f'7:frame:{n*10000}' for n in range(16)])]
    s=SearchSession(catalog,Index(),1,Filters(),'scene',np.ones(768),[],presentation={'stacks':True,'seconds':2})
    page=s.page(expand=True)
    assert len(page['items'])==1
    video=page['items'][0]
    assert video['moment_count']==16 and len(video['matched_moments'])==12
    assert catalog.db.execute('SELECT count(*) FROM search_moments').fetchone()[0]==16


def test_named_person_new_example_is_used_immediately(qtbot,tmp_path):
    w=MainWindow(Settings(data_dir=tmp_path),FakeBackend())
    qtbot.addWidget(w)
    w.selected_people=[dict(id='person',name='Человек',examples=['old'],rejected=[])]
    w.face_examples['old']={'id':'old'}
    w.find_person('new')
    assert w.backend.sent[-1]['people'][0]['examples']==['old','new']
    w.close()
