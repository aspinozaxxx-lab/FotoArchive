"""Native Windows acceptance: detailed vector map, restart/offline and UI latency."""
import json
import os
from pathlib import Path
import time


def run(data_dir,offline=False):
    os.environ.setdefault('QT_QPA_PLATFORM','windows')
    from PySide6.QtCore import QCoreApplication,Qt
    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    from PySide6.QtWidgets import QApplication
    from .map_view import MapView
    import requests
    import psutil
    folder=Path(data_dir)/'reports'/'v082-detailed-map';folder.mkdir(parents=True,exist_ok=True)
    suffix='offline' if offline else 'online';report=dict(passed=False,offline=offline,views=[])
    original_get=requests.Session.get
    if offline:
        def blocked(*args,**kwargs):raise requests.ConnectionError('Network disabled for acceptance test')
        requests.Session.get=blocked
    app=QApplication([]);w=MapView(data_dir=Path(data_dir));w.resize(1400,780)
    w.setWindowTitle('FotoArchive — проверка карты');gaps=[];last_tick=time.perf_counter()
    def pump():
        nonlocal last_tick
        app.processEvents();now=time.perf_counter();gaps.append((now-last_tick)*1000);last_tick=now;time.sleep(.005)
    def inspect():
        result=[]
        w.page.runJavaScript('JSON.stringify(fotoMap.inspect())',lambda value:result.append(value))
        until=time.monotonic()+3
        while not result and time.monotonic()<until:pump()
        try:return json.loads(result[0]) if result else {}
        except (ValueError,TypeError):return {}
    try:
        began=time.monotonic();w.show()
        while not w.ready:
            pump()
            if time.monotonic()-began>45:raise TimeoutError('Map initialization')
        for name,bounds in [('region',w.DEFAULT_BOUNDS),('streets',(37.55,55.70,37.68,55.79)),
                            ('buildings',(37.605,55.747,37.625,55.757)),('world',(-179,-75,179,80))]:
            began=time.monotonic();w.fit(bounds);state={};stable=0
            while time.monotonic()-began<45:
                until=time.monotonic()+.15
                while time.monotonic()<until:pump()
                state=inspect()
                if state.get('loaded') and state.get('features',0)>30:stable+=1
                else:stable=0
                if stable>=3:break
            if stable<3:raise AssertionError((name,state,w.server.errors))
            report['views'].append(dict(name=name,seconds=round(time.monotonic()-began,3),**state))
            w.grab().save(str(folder/f'{name}-{suffix}.png'))
            print(report['views'][-1],flush=True)
        report['errors']=w.server.errors
        assert not report['errors'],report['errors']
        assert report['views'][2].get('buildings',0)>10,report['views'][2]
        assert w.server.cache.base_ready()
        report['cache_bytes']=w.server.cache.path.stat().st_size
        report['ui_gap_p95_ms']=sorted(gaps)[int(.95*(len(gaps)-1))]
        report['ui_gap_max_ms']=max(gaps)
        process=psutil.Process();report['ram_tree_mib']=sum(p.memory_info().rss for p in [process]+process.children(recursive=True))/1024**2
        report['passed']=True
    except Exception as exc:report['error']=repr(exc)
    finally:
        started=time.monotonic();w.close_resources();w.close();app.processEvents()
        report['close_ms']=(time.monotonic()-started)*1000
        requests.Session.get=original_get
        (folder/f'{suffix}.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)
    return 0 if report['passed'] else 1
