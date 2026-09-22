"""Windows job object: a model subprocess cannot survive a crashed owner."""
import ctypes
from ctypes import wintypes
import os


class ModelJob:
    def __init__(self, process):
        self.handle = None
        if os.name != 'nt':
            return
        class Basic(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit',ctypes.c_int64),('PerJobUserTimeLimit',ctypes.c_int64),
                        ('LimitFlags',wintypes.DWORD),('MinimumWorkingSetSize',ctypes.c_size_t),('MaximumWorkingSetSize',ctypes.c_size_t),
                        ('ActiveProcessLimit',wintypes.DWORD),('Affinity',ctypes.c_size_t),('PriorityClass',wintypes.DWORD),('SchedulingClass',wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name,ctypes.c_uint64) for name in ('ReadOperationCount','WriteOperationCount','OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')]
        class Extended(ctypes.Structure):
            _fields_ = [('BasicLimitInformation',Basic),('IoInfo',IO),('ProcessMemoryLimit',ctypes.c_size_t),('JobMemoryLimit',ctypes.c_size_t),('PeakProcessMemoryUsed',ctypes.c_size_t),('PeakJobMemoryUsed',ctypes.c_size_t)]
        api=ctypes.WinDLL('kernel32',use_last_error=True)
        api.CreateJobObjectW.argtypes=[ctypes.c_void_p,wintypes.LPCWSTR]
        api.CreateJobObjectW.restype=wintypes.HANDLE
        api.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
        api.SetInformationJobObject.restype=wintypes.BOOL
        api.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
        api.AssignProcessToJobObject.restype=wintypes.BOOL
        api.CloseHandle.argtypes=[wintypes.HANDLE]
        self.api=api
        self.handle=api.CreateJobObjectW(None,None)
        info=Extended();info.BasicLimitInformation.LimitFlags=0x2000
        if not self.handle or not api.SetInformationJobObject(self.handle,9,ctypes.byref(info),ctypes.sizeof(info)) or not api.AssignProcessToJobObject(self.handle,wintypes.HANDLE(int(process._handle))):
            error=ctypes.get_last_error()
            self.close()
            raise OSError(error,'Не удалось привязать процесс модели к приложению')

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle=None
