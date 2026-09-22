import os
import subprocess
import sys
import pytest
from fotoarchive.windows_job import ModelJob

@pytest.mark.skipif(os.name != 'nt',reason='Windows process lifecycle')
def test_model_child_dies_when_job_closes():
    process=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        job=ModelJob(process)
        assert process.poll() is None
        job.close()
        process.wait(timeout=5)
        assert process.returncode is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
