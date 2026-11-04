"""Where a window is: a browser's address (read from its address bar) or a file in a work folder.

Browsers: Windows UI Automation (COM through ctypes, no extra packages) reads the
address bar of the browser window in front. Only the address is kept, without the
query string, fragment or user name/password, where tokens and session ids live.

Files: editors and IDEs don't tell Windows which file they have open and their
titles hold only the file name, so the name in the title is looked up in the
files of the registered work folders (query time, nothing new is recorded).
"""

import logging
import re
import sqlite3
import threading
from collections import defaultdict
from pathlib import Path
from typing import Optional

from .sessions import BROWSER_APPS, app_key

log = logging.getLogger(__name__)

# What a typed search looks like in the address bar: spaces, or no dot/colon/slash at all.
_NOT_AN_ADDRESS = re.compile(r"\s|^[^.:/]*$")
_USERINFO = re.compile(r"^((?:[a-z][a-z0-9+.-]*://)?)[^/@]*@", re.I)


def is_browser(app: str) -> bool:
    return app_key(app) in BROWSER_APPS


def clean_url(value: str) -> str:
    """An address bar value → the address without query, fragment and user info; "" if not an address."""
    value = (value or "").strip()
    if not value or _NOT_AN_ADDRESS.search(value):
        return ""
    value = re.split(r"[?#]", value, maxsplit=1)[0]
    return _USERINFO.sub(r"\1", value)[:500]


class AddressBarReader:
    """Reads the first edit box (the address bar) of a browser window through UI Automation.

    COM is set up per thread on first use (the collector reads on its own thread).
    """

    # UIAutomationClient.h vtable slots (IUnknown takes 0-2).
    _RELEASE = 2
    _ELEMENT_FROM_HANDLE = 6        # IUIAutomation
    _CREATE_PROPERTY_CONDITION = 23  # IUIAutomation
    _FIND_FIRST = 5                  # IUIAutomationElement
    _GET_CURRENT_PROPERTY_VALUE = 10  # IUIAutomationElement
    _CONTROL_TYPE_PROPERTY = 30003
    _EDIT_CONTROL_TYPE = 50004
    _VALUE_PROPERTY = 30045
    _TREE_SCOPE_DESCENDANTS = 4
    _VT_I4, _VT_BSTR = 3, 8

    def __init__(self):
        import ctypes

        self._ct = ctypes
        self._local = threading.local()

        class VARIANT(ctypes.Structure):
            _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                        ("r3", ctypes.c_ushort), ("val", ctypes.c_void_p), ("val2", ctypes.c_void_p)]

        class GUID(ctypes.Structure):
            _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort), ("d3", ctypes.c_ushort),
                        ("d4", ctypes.c_ubyte * 8)]

        self._VARIANT, self._GUID = VARIANT, GUID
        self._ole32 = ctypes.WinDLL("ole32")
        self._oleaut32 = ctypes.WinDLL("oleaut32")
        self._oleaut32.VariantClear.argtypes = [ctypes.POINTER(VARIANT)]

    def _method(self, index: int, *argtypes):
        ct = self._ct
        return ct.WINFUNCTYPE(ct.HRESULT, *argtypes)(index, "uia")

    def _guid(self, text: str):
        guid = self._GUID()
        self._ole32.CLSIDFromString(self._ct.c_wchar_p(text), self._ct.byref(guid))
        return guid

    def _release(self, obj) -> None:
        if obj:
            self._ct.WINFUNCTYPE(self._ct.c_ulong)(self._RELEASE, "Release")(obj)

    def _setup(self):
        """(IUIAutomation, edit-box condition) for this thread."""
        if getattr(self._local, "automation", None):
            return self._local.automation, self._local.condition
        ct = self._ct
        self._ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED; S_FALSE if already set up
        automation = ct.c_void_p()
        self._ole32.CoCreateInstance(
            ct.byref(self._guid("{ff48dba4-60ef-4201-aa87-54103eef594e}")), None, 1,  # CUIAutomation, inproc
            ct.byref(self._guid("{30cbe57d-d9d0-452a-ab13-7ac5ac4825ee}")), ct.byref(automation))
        if not automation:
            raise OSError("UI Automation unavailable")
        edit = self._VARIANT(self._VT_I4, 0, 0, 0, self._EDIT_CONTROL_TYPE, None)
        condition = ct.c_void_p()
        self._method(self._CREATE_PROPERTY_CONDITION, ct.c_int, self._VARIANT,
                     ct.POINTER(ct.c_void_p))(automation, self._CONTROL_TYPE_PROPERTY, edit, ct.byref(condition))
        self._local.automation, self._local.condition = automation, condition
        return automation, condition

    def address(self, hwnd: int) -> str:
        """The raw address bar text of the browser window `hwnd`, or ""."""
        ct = self._ct
        automation, condition = self._setup()
        window, box = ct.c_void_p(), ct.c_void_p()
        try:
            self._method(self._ELEMENT_FROM_HANDLE, ct.c_void_p, ct.POINTER(ct.c_void_p))(
                automation, hwnd, ct.byref(window))
            if not window:
                return ""
            self._method(self._FIND_FIRST, ct.c_int, ct.c_void_p, ct.POINTER(ct.c_void_p))(
                window, self._TREE_SCOPE_DESCENDANTS, condition, ct.byref(box))
            if not box:
                return ""
            value = self._VARIANT()
            self._method(self._GET_CURRENT_PROPERTY_VALUE, ct.c_int, ct.POINTER(self._VARIANT))(
                box, self._VALUE_PROPERTY, ct.byref(value))
            try:
                return ct.wstring_at(value.val) if value.vt == self._VT_BSTR and value.val else ""
            finally:
                self._oleaut32.VariantClear(ct.byref(value))
        finally:
            self._release(box)
            self._release(window)


# ---------------------------------------------------------------- files in work folders

_FILE_NAME = re.compile(r"[\w.\-]+\.[A-Za-z0-9]{1,8}", re.UNICODE)


def file_paths(conn: sqlite3.Connection, titles: list[str]) -> dict[str, str]:
    """Window title → full path of the work-folder file it names, where that is unambiguous.

    A name found in several places is narrowed down by the other words of the title
    (IDE titles usually start with the project folder's name).
    """
    wanted: dict[str, list[str]] = {}
    for title in titles:
        names = [n for n in _FILE_NAME.findall(title or "") if not n.replace(".", "").isdigit()]
        if names:
            wanted[title] = names
    if not wanted:
        return {}
    by_name: dict[str, list[str]] = defaultdict(list)
    needed = {n.casefold() for names in wanted.values() for n in names}
    for r in conn.execute("SELECT f.path AS root, s.path AS rel FROM file_state s "
                          "JOIN watched_folders f ON f.id = s.folder_id"):
        name = r["rel"].rsplit("/", 1)[-1]
        if name.casefold() in needed:
            by_name[name.casefold()].append(str(Path(r["root"], *r["rel"].split("/"))))
    out = {}
    for title, names in wanted.items():
        words = [w.casefold() for w in re.split(r"\s+[-–—|·]\s+|\s+", title) if len(w) > 2]
        for name in names:
            found = by_name.get(name.casefold(), [])
            if len(found) > 1:
                found = [p for p in found if any(w in p.casefold() for w in words if w != name.casefold())]
            if len(found) == 1:
                out[title] = found[0]
                break
    return out


def location_kind(path: str) -> Optional[str]:
    if not path:
        return None
    return "url" if not re.match(r"^[A-Za-z]:[\\/]|^\\\\|^/", path) else "file"
