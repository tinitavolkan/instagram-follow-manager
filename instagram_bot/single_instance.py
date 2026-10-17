"""Keep one desktop application writing a data folder at a time."""
from hashlib import sha256
import os
from pathlib import Path


class SingleInstance:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).resolve()
        self._handle = None
        self._kernel = None
        self._file = None

    def acquire(self):
        if self._handle is not None or self._file is not None:
            return True
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            kernel.CreateMutexW.restype = wintypes.HANDLE
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            key = sha256(str(self.data_dir).casefold().encode('utf-8')).hexdigest()
            handle = kernel.CreateMutexW(None, False, 'Local\\InstagramTakipExport-' + key)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
                kernel.CloseHandle(handle)
                return False
            self._kernel, self._handle = kernel, handle
        else:
            import errno
            import fcntl
            self.data_dir.mkdir(parents=True, exist_ok=True)
            stream = (self.data_dir / '.instagram_takip.lock').open('a+b')
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                stream.close()
                if exc.errno in {errno.EAGAIN, errno.EACCES}:
                    return False
                raise
            self._file = stream
        return True

    def release(self):
        if self._handle is not None:
            self._kernel.CloseHandle(self._handle)
            self._handle = self._kernel = None
        if self._file is not None:
            self._file.close()
            self._file = None
