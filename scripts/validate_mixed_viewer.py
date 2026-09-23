"""Exercise real Qt media playback against a bounded 200k-row catalogue."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image
import av
import psutil
from PySide6.QtCore import QObject, Signal, QTimer
from PySide6.QtWidgets import QApplication
from PySide6.QtMultimedia import QMediaPlayer
from fotoarchive.config import Settings
from fotoarchive.gallery import PhotoModel
from fotoarchive.ui import Viewer


class Backend(QObject):
    event=Signal(dict)
    def send(self,**message):pass


def checksum(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def percentile(values,portion=.95):
    return sorted(values)[int((len(values)-1)*portion)]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--video',type=Path)
    parser.add_argument('--transitions',type=int,default=240)
    args=parser.parse_args()
    app=QApplication.instance() or QApplication([])
    process=psutil.Process()
    with tempfile.TemporaryDirectory(prefix='fotoarchive-viewer-') as directory:
        root=Path(directory)
        wide=root/'wide.jpg';Image.new('RGB',(1600,1000),'#405570').save(wide)
        tall=root/'tall.png';Image.new('RGB',(800,1600),'#705540').save(tall)
        movie=root/'synthetic.mp4'
        with av.open(str(movie),'w') as output:
            stream=output.add_stream('mpeg4',rate=5)
            stream.width,stream.height,stream.pix_fmt=640,360,'yuv420p'
            for i in range(15):
                for packet in stream.encode(av.VideoFrame.from_image(Image.new('RGB',(640,360),'blue' if i%2 else 'red'))):
                    output.mux(packet)
            for packet in stream.encode():output.mux(packet)
        sources=[wide,movie,tall,args.video or movie]
        before={p:checksum(p) for p in set(sources)}
        def asset(row):
            path=sources[row%4]
            return dict(id=row+1,version=1,filename=path.name,path=str(path),
                        media_kind='video' if row%2 else 'image',timestamp_ms=0)
        model=PhotoModel()
        requested=[]
        def page(offset):
            requested.append(offset)
            QTimer.singleShot(5,lambda:model.accept_page(offset,[asset(i) for i in range(offset,min(200000,offset+200))],200000,offset+200<200000))
        model.pageRequested.connect(page)
        model.reset_result([asset(i) for i in range(99800,100000)],200000,True,offset=99800,loaded_count=100000)
        backend=Backend()
        viewer=Viewer(model,99996,Settings(data_dir=root/'data'),backend=backend)
        viewer.resize(1150,750);viewer.show()
        def ready():
            if viewer.waiting:return False
            if viewer.playing_video:
                player=viewer.video_pane.player
                if player.error()!=QMediaPlayer.NoError:raise RuntimeError(player.errorString())
                return viewer.video_pane.video.videoSink().videoFrame().isValid()
            return bool(viewer.scene.items())
        def pump_until(predicate,timeout=15):
            deadline=time.monotonic()+timeout
            while not predicate():
                app.processEvents()
                if time.monotonic()>deadline:raise TimeoutError('Viewer did not become ready')
                time.sleep(.002)
            app.processEvents()
        pump_until(ready)
        geometry=viewer.geometry();native=int(viewer.winId())
        latencies=[];display=[];heartbeats=[];previous=[time.perf_counter()]
        def heartbeat():
            now=time.perf_counter();heartbeats.append((now-previous[0])*1000);previous[0]=now
        pulse=QTimer();pulse.timeout.connect(heartbeat);pulse.start(10)
        start_rss=process.memory_info().rss;peak=start_rss
        max_pages=max_previews=0;max_preview_bytes=0
        steps=[1]*(args.transitions//2)+[-1]*(args.transitions-args.transitions//2)
        for i,step in enumerate(steps):
            started=time.perf_counter();viewer.navigate(step)
            latencies.append((time.perf_counter()-started)*1000)
            pump_until(ready)
            display.append((time.perf_counter()-started)*1000)
            assert viewer.geometry()==geometry and int(viewer.winId())==native
            peak=max(peak,process.memory_info().rss)
            max_pages=max(max_pages,len(model.blocks))
            max_previews=max(max_previews,len(viewer.preview_buffer.cache))
            max_preview_bytes=max(max_preview_bytes,sum(image.sizeInBytes() for image in viewer.preview_buffer.cache.values()))
        viewer.navigate(1);pump_until(ready)
        viewer.share_button.open_menu();app.processEvents()
        started=time.perf_counter();viewer.close();close_ms=(time.perf_counter()-started)*1000
        assert viewer.video_pane.player.source().isEmpty()
        assert viewer.video_pane.player.playbackState()==QMediaPlayer.StoppedState
        pulse.stop();app.processEvents()
        assert all(checksum(path)==value for path,value in before.items())
        assert max_pages<=PhotoModel.MAX_PAGES and max_previews<=11
        assert max_preview_bytes<=viewer.preview_buffer.BUDGET
        assert percentile(latencies[4:])<200 and close_ms<500
        print(json.dumps(dict(catalogue_rows=200000,transitions=len(steps),
            navigation_p95_ms=round(percentile(latencies[4:]),2),navigation_max_ms=round(max(latencies),2),
            first_frame_p95_ms=round(percentile(display[4:]),2),first_frame_max_ms=round(max(display),2),
            event_loop_p95_ms=round(percentile(heartbeats),2),event_loop_max_ms=round(max(heartbeats),2),
            close_with_share_menu_ms=round(close_ms,2),rss_start_mb=round(start_rss/1024**2,1),rss_peak_mb=round(peak/1024**2,1),
            cached_pages=max_pages,cached_previews=max_previews,preview_peak_mb=round(max_preview_bytes/1024**2,1),
            requested_pages=sorted(set(requested)),window_geometry_unchanged=True,original_checksums_unchanged=True),indent=2))


if __name__=='__main__':main()
