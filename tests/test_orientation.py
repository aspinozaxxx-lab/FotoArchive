import json
import struct
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageOps

from fotoarchive.catalog import Catalog, Filters, enumerate_source
from fotoarchive.config import Settings
from fotoarchive.folders import add_folders
from fotoarchive.media import atomic_thumbnail, extract_metadata, open_rgb, sha256
from fotoarchive.orientation import rotate_clockwise
from fotoarchive.orientation_engine import OrientationService
from fotoarchive.orientation_store import OrientationStore
from fotoarchive.rotation_files import rotate_exif, rotate_jpeg_bytes, write_rotated


def patterned_image():
    pixels = np.arange(47*31*3,dtype=np.uint8).reshape(31,47,3)
    return Image.fromarray(pixels)


@pytest.mark.parametrize('original',range(1,9))
@pytest.mark.parametrize('rotation',[90,180,270])
def test_jpeg_rotation_preserves_encoded_pixels_and_exif(tmp_path, original, rotation):
    path = tmp_path/'photo.jpg'
    exif = Image.Exif()
    exif[274]=original
    exif[271]='Camera model'
    exif[34665]={36867:'2002:07:10 12:34:56',37510:b'private unknown comment'}
    patterned_image().save(path,quality=91,exif=exif)
    before = path.read_bytes()
    expected = rotate_clockwise(open_rgb(path),rotation)
    target = tmp_path/'rotated.tmp'
    write_rotated(path,target,rotation)
    after = target.read_bytes()
    assert np.array_equal(open_rgb(target),expected)
    assert after[after.index(b'\xff\xda'):] == before[before.index(b'\xff\xda'):]
    with Image.open(path) as a, Image.open(target) as b:
        assert np.array_equal(a,b)
        assert b.getexif()[271]=='Camera model'
        assert b.getexif().get_ifd(34665)==a.getexif().get_ifd(34665)
    assert extract_metadata(target)['captured_at']=='2002-07-10T12:34:56'


@pytest.mark.parametrize('has_exif',[False,True])
def test_insert_orientation_without_relocating_existing_values(tmp_path, has_exif):
    path=tmp_path/'photo.jpg'
    exif=Image.Exif();exif[271]='Existing camera with out of line storage';exif[34665]={36867:'2003:12:01 01:02:03'}
    patterned_image().save(path,exif=exif if has_exif else b'')
    before=path.read_bytes()
    target=tmp_path/'result.jpg';target.write_bytes(rotate_jpeg_bytes(before,90))
    assert np.array_equal(open_rgb(target),rotate_clockwise(open_rgb(path),90))
    if has_exif:
        with Image.open(target) as image:
            assert image.getexif()[271]==exif[271]
            assert image.getexif().get_ifd(34665)[36867]=='2003:12:01 01:02:03'
    with pytest.raises(ValueError):
        rotate_jpeg_bytes(b'broken',90)


def test_bmp_rotation_preserves_pixels_and_palette(tmp_path):
    for mode in ('RGB','P','L','1'):
        image=patterned_image().convert(mode)
        path=tmp_path/f'{mode}.bmp';image.save(path)
        result=tmp_path/f'{mode}.tmp';write_rotated(path,result,270)
        assert np.array_equal(open_rgb(result),rotate_clockwise(open_rgb(path),270))


def make_service(tmp_path):
    root=tmp_path/'source';root.mkdir()
    catalog=Catalog(Settings(data_dir=tmp_path/'data',root=root))
    def metadata_only(stages=None,asset_id=None):
        job=catalog.next_job(('metadata',),asset_id)
        if job:
            asset=catalog.get(asset_id);thumb=tmp_path/f'{asset_id}_{asset["version"]}.webp'
            atomic_thumbnail(Path(asset['path']),thumb)
            catalog.complete_metadata(job,extract_metadata(Path(asset['path'])),thumb,sha256(Path(asset['path'])))
            catalog.finish_job(job,0)
        else:
            with catalog.db:
                catalog.db.execute("UPDATE jobs SET status='done' WHERE asset_id=?",(asset_id,))
        return job
    engine=SimpleNamespace(catalog=catalog,emit=lambda event:None,process_one=metadata_only,index=SimpleNamespace(flush_all=lambda:None))
    return catalog,OrientationService(engine),metadata_only


def add_photo(catalog,index=0):
    path=catalog.cfg.root/f'{index}.jpg';patterned_image().save(path,quality=91)
    asset_id,_=catalog.register(path)
    thumb=catalog.cfg.data_dir/f'{index}.webp';atomic_thumbnail(path,thumb)
    catalog.complete_metadata({'asset_id':asset_id,'file_version':1},extract_metadata(path),thumb,sha256(path))
    with catalog.db:
        catalog.db.execute("UPDATE jobs SET status='done' WHERE asset_id=?",(asset_id,))
    return catalog.get(asset_id)


def test_persistent_exclusions_apply_backup_reindex_and_exact_undo(tmp_path):
    catalog,service,_=make_service(tmp_path)
    a,b=add_photo(catalog),add_photo(catalog,1)
    store=service.store;store.enqueue()
    for asset in (a,b):
        store.complete(asset,{'rotation':90,'certain':True,'reason':'test'},.01)
    store.decision(b['id'],1,excluded=True)
    store.enqueue()
    assert store.stats()['selected']==1 and store.stats()['excluded']==1
    original=Path(a['path']).read_bytes()
    batch=store.queue_apply();assert batch['count']==1
    assert store.queue_apply()['count']==0
    service.tick(False)
    assert catalog.get(a['id'])['version']==2
    assert Path(a['path']).read_bytes()!=original
    assert Path(b['path']).read_bytes()==original
    while service.tick(False):
        pass
    edit=dict(catalog.db.execute('SELECT * FROM rotation_edits').fetchone())
    assert edit['status']=='done' and Path(edit['backup_path']).read_bytes()==original
    assert catalog.get(a['id'])['width']==a['height']
    assert store.queue_undo(batch['batch_id'])==1
    while service.tick(False):
        pass
    assert Path(a['path']).read_bytes()==original
    assert catalog.get(a['id'])['version']==3
    assert store.stats()['applied']==0
    assert catalog.db.execute('SELECT status FROM rotation_edits').fetchone()[0]=='undone'
    catalog.close()


def test_crash_after_replace_recovers_once_and_later_edits_block_undo(tmp_path, monkeypatch):
    catalog,service,_=make_service(tmp_path)
    asset=add_photo(catalog);store=service.store;store.enqueue()
    store.complete(asset,{'rotation':90,'certain':True,'reason':'test'},.01)
    batch=store.queue_apply()
    edit=dict(catalog.db.execute('SELECT * FROM rotation_edits').fetchone())
    register=catalog.register
    monkeypatch.setattr(catalog,'register',lambda path:(_ for _ in ()).throw(RuntimeError('crash after replace')))
    with pytest.raises(RuntimeError):
        service.apply_file(edit)
    assert catalog.db.execute('SELECT status FROM rotation_edits').fetchone()[0]=='prepared'
    once=Path(asset['path']).read_bytes()
    monkeypatch.setattr(catalog,'register',register)
    recovered=OrientationService(service.engine)
    while recovered.tick(False):
        pass
    assert Path(asset['path']).read_bytes()==once and catalog.get(asset['id'])['version']==2
    Path(asset['path']).write_bytes(once+b'user-edit')
    store.queue_undo(batch['batch_id'])
    recovered.tick(False)
    assert Path(asset['path']).read_bytes()==once+b'user-edit'
    assert 'изменился' in catalog.db.execute('SELECT error FROM rotation_edits').fetchone()[0]
    catalog.close()


def test_stale_suggestion_and_missing_disk_never_overwrite_source(tmp_path):
    catalog,service,_=make_service(tmp_path)
    asset=add_photo(catalog);store=service.store;store.enqueue()
    store.complete(asset,{'rotation':90,'certain':False,'reason':'test'},.01)
    assert store.stats()['selected']==0
    store.decision(asset['id'],1,selected=True)
    store.queue_apply()
    path=Path(asset['path']);content=path.read_bytes()+b'new'
    path.write_bytes(content)
    service.tick(False)
    assert path.read_bytes()==content and store.stats()['edit_errors']==1
    catalog.register(path)
    with pytest.raises(ValueError):
        store.decision(asset['id'],1,selected=True)
    catalog.close()


def test_folder_batch_skips_existing_coverage_and_continues(tmp_path):
    root=tmp_path/'photos'
    for name in ('2003/inside','2004','2005/nested','2006'):
        (root/name).mkdir(parents=True)
    cfg=Settings(data_dir=tmp_path/'data',root=root,includes=['2003'])
    cfg.save();before=(cfg.data_dir/'settings.json').read_bytes()
    result=add_folders(cfg,[root/'2003',root/'2003/inside'])
    assert not result['added'] and len(result['skipped'])==2
    assert (cfg.data_dir/'settings.json').read_bytes()==before
    result=add_folders(cfg,[root/'2004',root/'2004/.',root/'2005/nested',root/'2005',root/'missing',tmp_path/'outside',root/'2006'])
    assert result['added']==['2004','2005','2006']
    assert len(result['skipped'])==2 and len(result['errors'])==2
    assert cfg.includes==['2003','2004','2005','2006']
    for name in ('2003/old.jpg','2004/new.jpg','2005/nested/new.jpg','2006/new.jpg'):
        (root/name).write_bytes(b'test')
    from dataclasses import replace
    files=list(enumerate_source(replace(cfg,includes=result['added']),result['scan_excludes']))
    assert len(files)==3 and all('2003' not in path.parts for path,_ in files)


def test_apply_pause_and_exclusion_cancel_only_not_started_files(tmp_path):
    catalog,service,_=make_service(tmp_path)
    a,b=add_photo(catalog),add_photo(catalog,1)
    store=service.store;store.enqueue()
    for asset in (a,b):
        store.complete(asset,{'rotation':90,'certain':True,'reason':'test'},.01)
    originals={a['id']:Path(a['path']).read_bytes(),b['id']:Path(b['path']).read_bytes()}
    store.queue_apply()
    catalog.set_state('orientation_apply_paused',True)
    assert not service.tick(False)
    store.decision(b['id'],1,excluded=True)
    assert catalog.db.execute('SELECT status FROM rotation_edits WHERE asset_id=?',(b['id'],)).fetchone()[0]=='cancelled'
    catalog.set_state('orientation_apply_paused',False)
    while service.tick(False):
        pass
    assert Path(a['path']).read_bytes()!=originals[a['id']]
    assert Path(b['path']).read_bytes()==originals[b['id']]
    assert catalog.get(b['id'])['version']==1
    catalog.close()


def test_unavailable_original_records_error_without_removing_catalog_entry(tmp_path):
    catalog,service,_=make_service(tmp_path)
    asset=add_photo(catalog);store=service.store;store.enqueue()
    store.complete(asset,{'rotation':90,'certain':True,'reason':'test'},.01)
    store.queue_apply()
    path=Path(asset['path']);disconnected=path.with_suffix('.disconnected')
    path.rename(disconnected)
    service.tick(False)
    assert disconnected.exists() and not path.exists()
    assert store.stats()['edit_errors']==1 and catalog.get(asset['id'])['present']==1
    catalog.close()


def test_adding_parent_keeps_already_included_child_out_of_scan(tmp_path):
    from dataclasses import replace
    root=tmp_path/'photos';(root/'2003/inside').mkdir(parents=True)
    (root/'2003/inside/already.jpg').write_bytes(b'x')
    (root/'2003/new.jpg').write_bytes(b'x')
    cfg=Settings(data_dir=tmp_path/'data',root=root,includes=['2003/inside'])
    cfg.save()
    result=add_folders(cfg,[root/'2003'])
    assert cfg.includes==['2003'] and result['scan_excludes']==['2003/inside']
    files=list(enumerate_source(replace(cfg,includes=result['added']),result['scan_excludes']))
    assert [p.name for p,_ in files]==['new.jpg']
