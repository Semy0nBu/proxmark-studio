"""Launch native children without PyInstaller's inherited DLL search directory."""
from contextlib import contextmanager
import ctypes
import os
from threading import RLock

from .paths import is_frozen


_dll_directory_lock = RLock()


def _kernel32():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetDllDirectoryW.argtypes = [ctypes.c_uint32, ctypes.c_wchar_p]
    kernel.GetDllDirectoryW.restype = ctypes.c_uint32
    kernel.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
    kernel.SetDllDirectoryW.restype = ctypes.c_int
    return kernel


@contextmanager
def external_process_libraries():
    """Restore the GUI's DLL directory immediately after creating the child."""
    if not is_frozen() or os.name != 'nt':
        yield
        return
    with _dll_directory_lock:
        kernel = _kernel32()
        ctypes.set_last_error(0)
        size = kernel.GetDllDirectoryW(0, None)
        if size == 0 and ctypes.get_last_error():
            raise ctypes.WinError(ctypes.get_last_error())
        previous = None
        if size:
            buffer = ctypes.create_unicode_buffer(size + 1)
            ctypes.set_last_error(0)
            copied = kernel.GetDllDirectoryW(len(buffer), buffer)
            if (copied == 0 and ctypes.get_last_error()) or copied >= len(buffer):
                raise ctypes.WinError(ctypes.get_last_error())
            previous = buffer.value
        if not kernel.SetDllDirectoryW(None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield
        finally:
            if not kernel.SetDllDirectoryW(previous):
                raise ctypes.WinError(ctypes.get_last_error())
