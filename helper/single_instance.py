"""One collector per data directory and Windows login session."""
import ctypes
import hashlib
import os
import sys


class SingleInstance:
    def __init__(self, data_path):
        self.primary = True
        self._mutex = self._event = None
        if sys.platform != 'win32':
            return
        self._kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self._kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        self._kernel.CreateMutexW.restype = ctypes.c_void_p
        self._kernel.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_bool, ctypes.c_wchar_p]
        self._kernel.CreateEventW.restype = ctypes.c_void_p
        self._kernel.SetEvent.argtypes = [ctypes.c_void_p]
        self._kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        self._kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        key = hashlib.sha256(os.path.normcase(str(data_path.resolve())).encode()).hexdigest()[:24]
        name = 'Local\\WechatHelper-' + key
        self._event = self._kernel.CreateEventW(None, False, False, name + '-show')
        self._mutex = self._kernel.CreateMutexW(None, False, name)
        existed = ctypes.get_last_error() == 183
        if not self._mutex or not self._event:
            self.close()
            raise ctypes.WinError()
        self.primary = not existed

    def activate(self):
        if self._event:
            self._kernel.SetEvent(self._event)

    def listen(self, stop, show):
        if not self._event:
            return
        while not stop.is_set():
            if self._kernel.WaitForSingleObject(self._event, 500) == 0 and not stop.is_set():
                show()

    def close(self):
        for handle in (self._mutex, self._event):
            if handle:
                self._kernel.CloseHandle(handle)
        self._mutex = self._event = None
