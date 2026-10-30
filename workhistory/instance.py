"""One running app per database, whatever port it ended up on.

The port is not the lock any more (5730 may belong to another program, and then the
app moves to the next free port), so a named mutex per database decides who runs.
A second launch finds the running app by asking each candidate port.
"""

import ctypes
import hashlib
import os
import time
from ctypes import wintypes
from pathlib import Path
from typing import Iterable, Optional

from .server import find_running_instance

ERROR_ALREADY_EXISTS = 183
WAIT_S = 15  # a just-started instance may still be migrating its database

_held: list[int] = []  # mutex handles, kept until the process exits


def _key(db_path: Path) -> str:
    return os.path.normcase(os.path.abspath(db_path))


def claim(db_path: Path) -> bool:
    """True if this process is now the app for `db_path`; False if another one already is."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    name = "Local\\WorkHistory-" + hashlib.sha256(_key(db_path).encode("utf-8")).hexdigest()[:24]
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return True  # can't tell; let the exclusive port binding be the guard
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    _held.append(handle)
    return True


def find(ports: Iterable[int], db_path: Path, wait_s: float = WAIT_S) -> Optional[int]:
    """The port where the app for `db_path` answers, waiting a little for one that is starting."""
    ports = list(ports)
    deadline = time.monotonic() + wait_s
    while True:
        for port in ports:
            if find_running_instance(port, db_path=str(db_path), timeout=1):
                return port
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.5)
