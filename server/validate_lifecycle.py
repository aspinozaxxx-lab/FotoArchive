"""Real Linux process-tree tests, without allocating GPU memory."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from fotoarchive.remote_service import Supervisor,LEASE_SECONDS,gpu_state


def alive(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().split()[2]!='Z'
    except FileNotFoundError:
        return False


def owned_process(supervisor):
    code="import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(600)']); print(p.pid,flush=True); time.sleep(600)"
    supervisor.worker=subprocess.Popen([sys.executable,'-u','-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True,start_new_session=True)
    child=int(supervisor.worker.stdout.readline())
    return supervisor.worker.pid,child


results=[]
foreign_before={p['pid'] for p in gpu_state()['foreign']}
with tempfile.TemporaryDirectory(dir='/data/fotoarchive',prefix='lifecycle-') as directory:
    clock=[100.]
    state=dict(utilization=0,memory_mb=0,total_mb=32000,foreign=[])
    supervisor=Supervisor(directory,clock=lambda:clock[0],probe=lambda *args:state)
    for case in ('pause','expired','foreign-task','probe-failure'):
        supervisor.lease('lifecycle-validation',True)
        pids=owned_process(supervisor)
        started=time.monotonic()
        if case=='pause':
            supervisor.lease('lifecycle-validation',False)
        elif case=='expired':
            clock[0]+=LEASE_SECONDS+1
            supervisor.tick()
        elif case=='foreign-task':
            state['foreign']=[{'pid':12345}]
            supervisor.tick()
        else:
            def failed(*_):
                raise RuntimeError('GPU query unavailable')
            supervisor.probe=failed
            supervisor.tick()
        deadline=time.monotonic()+3
        while any(alive(pid) for pid in pids) and time.monotonic()<deadline:
            time.sleep(.05)
        assert not any(alive(pid) for pid in pids),case
        results.append(dict(case=case,seconds=round(time.monotonic()-started,3),tree_released=True))
        state['foreign']=[]
    supervisor.store.db.close()
foreign_after={p['pid'] for p in gpu_state()['foreign']}
assert foreign_before==foreign_after,(foreign_before,foreign_after)
code="import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-m','fotoarchive.remote_exec',sys.executable,'-u','-c',\"import time; print('child-ready',flush=True); time.sleep(600)\"]); print(p.pid,flush=True); time.sleep(600)"
parent=subprocess.Popen([sys.executable,'-u','-c',code],stdout=subprocess.PIPE,text=True)
lines=[parent.stdout.readline().strip(),parent.stdout.readline().strip()]
child=int(next(line for line in lines if line.isdecimal()))
assert 'child-ready' in lines
parent.kill();parent.wait(3)
deadline=time.monotonic()+3
while alive(child) and time.monotonic()<deadline:
    time.sleep(.05)
assert not alive(child)
results.append(dict(case='parent-crash',tree_released=True))
print(json.dumps(dict(checks=results,existing_gpu_processes_unchanged=True),indent=2))
