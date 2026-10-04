"""Reuse validated weights, isolate runtimes from unrelated production projects."""
from pathlib import Path
import os
import pwd
import shutil

DATA = Path('/data/fotoarchive')
EXPERIMENT = Path('/data/fotoarchive-experiments/video-caption-20260926')
uid, gid = pwd.getpwnam('fotoarchive').pw_uid, pwd.getpwnam('fotoarchive').pw_gid
# Permit only the FotoArchive group to traverse these private model ancestors.
# No media directory listing or permissions on experiment photographs change.
for ancestor in (EXPERIMENT, EXPERIMENT/'omni-audio-20260926'):
    os.chown(ancestor, -1, gid)
    ancestor.chmod(0o710)
models = DATA/'models/video-torch'
models.mkdir(parents=True, exist_ok=True)
for name, source, revision in (
    ('omni', EXPERIMENT/'omni-audio-20260926/model', '503e754207c94da6bb26850b4469f367c9ea3582'),
    ('internvideo', EXPERIMENT/'models/internvideo25', '87680fe232af8681b55322f68541679b91f99df1')):
    target = models/name
    if not target.exists():
        target.symlink_to(source, target_is_directory=True)
    (source/'fotoarchive-revision').write_text(revision)
runtime = DATA/'runtime/video-torch'
runtime.mkdir(parents=True, exist_ok=True)
shared = runtime/'shared'
if not shared.exists():
    shutil.copytree('/opt/mlsystem2/repo/.venv/lib/python3.12/site-packages', shared, symlinks=False)
for name, source in (('omni',EXPERIMENT/'omni-audio-20260926/venv'), ('internvideo',EXPERIMENT/'legacy')):
    target = runtime/name
    if not target.exists():
        shutil.copytree(source, target, symlinks=True)
    (target/'lib/python3.12/site-packages/shared-readonly.pth').write_text(str(shared)+'\n')
for root in (runtime, models):
    os.chown(root, uid, gid)
for name in ('hf-cache','torch-cache','cache'):
    folder = runtime/name
    folder.mkdir(exist_ok=True)
    os.chown(folder, uid, gid)
print('Independent video environments and pinned weights ready')
