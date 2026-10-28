"""Start with the computer / desktop shortcut, switched from inside the app.

Only ever creates, overwrites or removes files with this app's own names:
Startup\\작업 기록.lnk and Desktop\\작업 기록.lnk (via the shell's own COM object).

What gets started: the launcher the app was started from (the launchers set
WORKHISTORY_LAUNCHER), else this Python with `-m workhistory`.
"""

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import console_python
from .store import Invalid

APP_NAME = "작업 기록"
LNK_NAME = APP_NAME + ".lnk"
TIMEOUT_S = 30

# Windows known folders (SHGetKnownFolderPath); they follow OneDrive / folder redirection.
_FOLDER_IDS = {"startup": "{B97D20BB-F46A-4C97-BA10-5E3608430854}",
               "desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"}

# Creates a .lnk with WScript.Shell. Paths come in through environment variables,
# so nothing user-controlled is ever parsed as PowerShell code.
_PS_SHORTCUT = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:WH_LNK); "
                "$s.TargetPath = $env:WH_TARGET; $s.Arguments = $env:WH_ARGS; "
                "$s.WorkingDirectory = $env:WH_DIR; $s.WindowStyle = 7; $s.Description = $env:WH_DESC; $s.Save()")


def _known_folder(name: str) -> Path:
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    guid = GUID()
    ctypes.oledll.ole32.CLSIDFromString(_FOLDER_IDS[name], ctypes.byref(guid))
    out = ctypes.c_wchar_p()
    ctypes.oledll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out))
    try:
        return Path(out.value)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(out)


def _powershell(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _PS_SHORTCUT],
                          env={**os.environ, **env}, capture_output=True, timeout=TIMEOUT_S,
                          encoding="utf-8", errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def launch_command() -> list[str]:
    launcher = os.environ.get("WORKHISTORY_LAUNCHER")
    if launcher and Path(launcher).is_file():
        return [str(Path(launcher).resolve())]
    return [console_python(), "-m", "workhistory"]


@dataclass
class Launch:
    command: list[str] = field(default_factory=launch_command)
    folder: Callable[[str], Path] = _known_folder
    powershell: Callable[[dict], subprocess.CompletedProcess] = _powershell

    # ------------------------------------------------------------ where

    def _autostart_path(self) -> Path:
        return self.folder("startup") / LNK_NAME

    def _shortcut_path(self) -> Path:
        return self.folder("desktop") / LNK_NAME

    # ------------------------------------------------------------ state

    def status(self) -> dict:
        return {
            "autostart": {"supported": True, "enabled": self._autostart_path().exists()},
            "shortcut": {"supported": True, "exists": self._shortcut_path().exists(), "where": "바탕화면"},
            "command": self.command,
        }

    def set_autostart(self, enabled: bool) -> dict:
        path = self._autostart_path()
        if enabled:
            self._write(path, autostart=True)
        else:
            path.unlink(missing_ok=True)
        return self.status()

    def create_shortcut(self) -> dict:
        self._write(self._shortcut_path(), autostart=False)
        return self.status()

    # ------------------------------------------------------------ writing

    def _args(self, autostart: bool) -> list[str]:
        # At logon the app starts in the background; opening the shortcut also opens the browser.
        return self.command + (["--no-browser"] if autostart else [])

    def _write(self, path: Path, autostart: bool) -> None:
        args = self._args(autostart)
        path.parent.mkdir(parents=True, exist_ok=True)
        target = args[0]
        out = self.powershell({"WH_LNK": str(path), "WH_TARGET": target,
                               "WH_ARGS": subprocess.list2cmdline(args[1:]),
                               "WH_DIR": str(Path(target).parent), "WH_DESC": APP_NAME})
        if out.returncode != 0 or not path.exists():
            raise Invalid("바로가기를 만들지 못했습니다: " + (out.stderr or out.stdout).strip()[:200])


def default() -> Launch:
    return Launch()
