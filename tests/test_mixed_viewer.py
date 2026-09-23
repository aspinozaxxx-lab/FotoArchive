"""Mixed catalogue navigation keeps one window, bounded pages and one decoder."""
import time
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QMediaPlayer

from fotoarchive.config import Settings
from fotoarchive.gallery import PhotoModel
from fotoarchive.ui import Viewer, MainWindow
from test_ui import FakeBackend
from test_media_formats import make_video


@pytest.fixture
def mixed(tmp_path):
    photo=tmp_path/'landscape.jpg';Image.new('RGB',(640,420),'green').save(photo)
    portrait=tmp_path/'portrait.png';Image.new('RGB',(300,600),'blue').save(portrait)
    clip=make_video(tmp_path/'clip.mp4',duration=4)
    paths=[photo,clip,portrait,clip]
    return [dict(id=i+1,version=1,path=str(p),filename=f'{i}-{p.name}',
                 media_kind='video' if p.suffix=='.mp4' else 'image',timestamp_ms=0,
                 captured_at='2021-04-28T13:04:27') for i,p in enumerate(paths)]


def wait_video(qtbot,viewer):
    pane=viewer.video_pane
    qtbot.waitUntil(lambda:pane.player.error()!=QMediaPlayer.NoError or pane.video.videoSink().videoFrame().isValid(),timeout=10000)
    assert pane.player.error()==QMediaPlayer.NoError
    assert pane.player.playbackState()==QMediaPlayer.PlayingState


@pytest.mark.parametrize('maximized',[False,True])
def test_photos_and_movies_follow_catalogue_in_same_window(qtbot,tmp_path,mixed,maximized):
    viewer=Viewer(mixed,0,Settings(data_dir=tmp_path/'data'));qtbot.addWidget(viewer)
    viewer.resize(1100,720)
    viewer.showMaximized() if maximized else viewer.show()
    qtbot.waitUntil(lambda:bool(viewer.scene.items()))
    qtbot.waitUntil(lambda:viewer.preview_buffer.get(mixed[2]) is not None)
    geometry,state=viewer.geometry(),viewer.windowState()
    native_id=int(viewer.winId())
    decoder=None
    for step,expected in ((1,1),(1,2),(1,3),(-1,2),(-1,1),(-1,0)):
        button=viewer.controls['next' if step==1 else 'previous']
        qtbot.mouseClick(button,Qt.LeftButton)
        assert viewer.position==expected and viewer.asset['id']==mixed[expected]['id']
        if expected%2:
            wait_video(qtbot,viewer)
            assert viewer.media_stack.currentWidget() is viewer.video_pane
            decoder=decoder or viewer.video_pane.player
            assert viewer.video_pane.player is decoder
            assert viewer.share_button.asset_provider()['path']==mixed[expected]['path']
        else:
            qtbot.waitUntil(lambda:bool(viewer.scene.items()))
            assert viewer.media_stack.currentWidget() is viewer.image_page
            assert decoder.source().isEmpty() and decoder.playbackState()==QMediaPlayer.StoppedState
            assert not viewer.video_pane.video.videoSink().videoFrame().isValid()
        assert viewer.geometry()==geometry and viewer.windowState()==state and int(viewer.winId())==native_id
    assert not viewer.controls['previous'].isEnabled()
    viewer.close()


def test_open_video_first_arrows_work_even_with_timeline_focus(qtbot,tmp_path,mixed):
    viewer=Viewer(mixed,1,Settings(data_dir=tmp_path/'data'));qtbot.addWidget(viewer);viewer.show()
    wait_video(qtbot,viewer)
    viewer.video_pane.slider.setFocus()
    qtbot.keyClick(viewer.video_pane.slider,Qt.Key_Right)
    assert viewer.position==2 and not viewer.playing_video
    qtbot.keyClick(viewer,Qt.Key_Left)
    assert viewer.position==1 and viewer.playing_video
    viewer.close()


def test_video_boundary_waits_for_already_requested_page_without_skipping(qtbot,tmp_path,mixed):
    model=PhotoModel();pages=[];model.pageRequested.connect(pages.append)
    first=[dict(mixed[0],id=i+100) for i in range(200)];first[-1]=mixed[1]
    model.reset_result(first,201,True)
    viewer=Viewer(model,199,Settings(data_dir=tmp_path/'data'));qtbot.addWidget(viewer);viewer.show()
    assert 200 in model.pending  # Prefetch already owns this request.
    geometry=viewer.geometry()
    qtbot.mouseClick(viewer.controls['next'],Qt.LeftButton)
    assert viewer.waiting and viewer.position==200 and pages.count(200)==1
    assert viewer.video_pane.player.source().isEmpty()
    assert not viewer.controls['next'].isEnabled()
    model.accept_page(200,[mixed[2]],201,False)
    qtbot.waitUntil(lambda:not viewer.waiting and bool(viewer.scene.items()))
    assert viewer.asset['id']==mixed[2]['id'] and viewer.geometry()==geometry
    qtbot.mouseClick(viewer.controls['previous'],Qt.LeftButton)
    assert viewer.position==199 and viewer.playing_video
    viewer.close()


def test_backwards_into_evicted_page_loads_video_in_large_catalogue(qtbot,tmp_path,mixed):
    model=PhotoModel();pages=[];model.pageRequested.connect(pages.append)
    rows=[dict(mixed[0],id=i+1800) for i in range(200)]
    model.reset_result(rows,200000,True,offset=1800,loaded_count=2000)
    for offset in (0,200,400,600):model.request_page(offset)
    viewer=Viewer(model,1800,Settings(data_dir=tmp_path/'data'));qtbot.addWidget(viewer);viewer.show()
    viewer.navigate(-1)
    assert viewer.waiting and 1600 not in pages
    model.accept_page(0,[dict(mixed[0],id=i) for i in range(200)],200000,True)
    assert viewer.waiting and 1600 in pages
    previous=[dict(mixed[0],id=i+1600) for i in range(200)];previous[-1]=mixed[1]
    model.accept_page(1600,previous,200000,True)
    assert not viewer.waiting and viewer.position==1799 and viewer.playing_video
    assert len(model.blocks)<=model.MAX_PAGES
    viewer.close()


def test_found_frame_and_playback_stay_in_same_window_and_discard_old_images(qtbot,tmp_path,mixed):
    backend=FakeBackend()
    moment=dict(unit_id='2:frame:2000',timestamp_ms=2000,thumbnail='')
    mixed[1].update(unit_id='2:frame:0',matched_moments=[moment],moment_count=1)
    viewer=Viewer(mixed,1,Settings(data_dir=tmp_path/'data'),backend=backend)
    qtbot.addWidget(viewer);viewer.show();wait_video(qtbot,viewer)
    geometry=viewer.geometry()
    viewer.video_pane.choose_moment(moment)
    assert viewer.asset['unit_id']==moment['unit_id']
    viewer.show_video_frame()
    qtbot.waitUntil(lambda:bool(viewer.scene.items()))
    assert not viewer.playing_video and viewer.resume_video_button.isVisible()
    assert backend.sent[-1]['unit_id']==moment['unit_id']
    old_token=viewer.load_token
    qtbot.mouseClick(viewer.resume_video_button,Qt.LeftButton)
    viewer.loaded(old_token,QImage(10,10,QImage.Format_RGB888),'',False)
    assert viewer.playing_video and viewer.media_stack.currentWidget() is viewer.video_pane
    qtbot.waitUntil(lambda:viewer.video_pane.player.position()>=2000,timeout=10000)
    assert viewer.geometry()==geometry
    viewer.close()


def test_share_menu_navigation_and_close_release_video(qtbot,tmp_path,mixed):
    viewer=Viewer(mixed,1,Settings(data_dir=tmp_path/'data'));qtbot.addWidget(viewer);viewer.show()
    wait_video(qtbot,viewer)
    viewer.share_button.open_menu();assert viewer.share_button.menu.isVisible()
    viewer.navigate(1)
    assert not viewer.share_button.menu.isVisible() and not viewer.share_button.closed
    assert viewer.video_pane.player.source().isEmpty()
    viewer.navigate(-1);wait_video(qtbot,viewer)
    viewer.share_button.open_menu()
    start=time.perf_counter();viewer.close()
    assert time.perf_counter()-start<.5
    assert viewer.share_button.closed and viewer.video_pane.player.source().isEmpty()


def test_moment_requests_disconnect_on_navigation(qtbot,tmp_path,mixed):
    backend=FakeBackend();owner=MainWindow(Settings(data_dir=tmp_path/'data'),backend);qtbot.addWidget(owner)
    viewer=Viewer(mixed,1,owner.cfg,owner,backend);qtbot.addWidget(viewer);viewer.show()
    viewer.more_moments();request=backend.sent[-1]
    assert request['action']=='match_moments' and request['id']==owner.request_id
    viewer.navigate(1)
    assert viewer.video_pane.moments_dialog is None
    backend.event.emit(dict(type='match_moments',serial=request['serial'],items=[]))
    assert viewer.asset['id']==mixed[2]['id']
    viewer.close();owner.close()


def test_consecutive_videos_cancel_loading_and_keep_navigation_after_bad_file(qtbot,tmp_path,mixed):
    broken=tmp_path/'broken.mp4';broken.write_bytes(b'not a movie')
    items=[mixed[1],dict(mixed[1],id=90,path=str(broken),filename=broken.name),mixed[3],mixed[2]]
    viewer=Viewer(items,0,Settings(data_dir=tmp_path/'data'));qtbot.addWidget(viewer);viewer.show()
    wait_video(qtbot,viewer)
    geometry=viewer.geometry();decoder=viewer.video_pane.player
    viewer.navigate(1)
    qtbot.waitUntil(lambda:decoder.error()!=QMediaPlayer.NoError,timeout=10000)
    assert viewer.controls['next'].isEnabled() and viewer.geometry()==geometry
    viewer.navigate(1);wait_video(qtbot,viewer)
    assert decoder is viewer.video_pane.player and decoder.source()==QUrl.fromLocalFile(mixed[3]['path'])
    for _ in range(15):
        viewer.navigate(-1);viewer.navigate(-1);viewer.navigate(1);viewer.navigate(1)
        qtbot.wait(2)
    viewer.navigate(1)
    qtbot.waitUntil(lambda:bool(viewer.scene.items()))
    assert viewer.position==3 and decoder.source().isEmpty() and viewer.geometry()==geometry
    viewer.close()
