"""Linux exec wrapper: parent-death handling without fork hooks in CUDA threads."""
import ctypes
import os
import signal
import sys


def die_with_parent():
    parent = os.getppid()
    if ctypes.CDLL(None).prctl(1,signal.SIGKILL)!=0:
        raise OSError('Cannot set parent-death signal')
    if parent==1 or os.getppid()!=parent:
        os._exit(1)


if __name__=='__main__':
    die_with_parent()
    os.execv(sys.argv[1],sys.argv[1:])
