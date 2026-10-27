"""Start with the computer / desktop shortcut, switched from inside the app.

Only ever creates, overwrites or removes files with this app's own names:
  Windows  Startup\\작업 기록.lnk, Desktop\\작업 기록.lnk  (via the shell's own COM object)
  macOS    ~/Library/LaunchAgents/com.workhistory.app.plist  (no desktop shortcut)
  Linux    ~/.config/autostart/workhistory.desktop, ~/.local/share/applications/workhistory.desktop

What gets started: the launcher the app was started from (the launchers set
WORKHISTORY_LAUNCHER), else this Python with `-m workhistory`.
"""

import os
import platform
import plistlib
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .config import console_python
from .store import Invalid

APP_NAME = "작업 기록"
LNK_NAME = APP_NAME + ".lnk"
MAC_LABEL = "com.workhistory.app"
LINUX_FILE = "workhistory.desktop"
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


def _desktop_quote(arg: str) -> str:
    """Quote one argument for an Exec= line (freedesktop Desktop Entry spec)."""
    if arg and all(c.isalnum() or c in "-_./:=+@" for c in arg):
        return arg
    escaped = "".join("\\" + c if c in '"`$\\' else c for c in arg)
    return f'"{escaped}"'


@dataclass
class Launch:
    system: str = field(default_factory=platform.system)  # Windows | Darwin | Linux
    home: Path = field(default_factory=Path.home)
    command: list[str] = field(default_factory=launch_command)
    folder: Callable[[str], Path] = _known_folder        # Windows only
    powershell: Callable[[dict], subprocess.CompletedProcess] = _powershell

    # ------------------------------------------------------------ where

    def _autostart_path(self) -> Optional[Path]:
        if self.system == "Windows":
            return self.folder("startup") / LNK_NAME
        if self.system == "Darwin":
            return self.home / "Library" / "LaunchAgents" / f"{MAC_LABEL}.plist"
        if self.system == "Linux":
            config = Path(os.environ.get("XDG_CONFIG_HOME") or self.home / ".config")
            return config / "autostart" / LINUX_FILE
        return None

    def _shortcut_path(self) -> Optional[Path]:
        if self.system == "Windows":
            return self.folder("desktop") / LNK_NAME
        if self.system == "Linux":
            data = Path(os.environ.get("XDG_DATA_HOME") or self.home / ".local" / "share")
            return data / "applications" / LINUX_FILE  # the app menu
        return None

    # ------------------------------------------------------------ state

    def status(self) -> dict:
        auto, short = self._autostart_path(), self._shortcut_path()
        return {
            "system": self.system,
            "autostart": {"supported": auto is not None, "enabled": bool(auto and auto.exists())},
            "shortcut": {"supported": short is not None, "exists": bool(short and short.exists()),
                         "where": "바탕화면" if self.system == "Windows" else "프로그램 메뉴"},
            "command": self.command,
        }

    def set_autostart(self, enabled: bool) -> dict:
        path = self._autostart_path()
        if path is None:
            raise Invalid("이 운영체제에서는 자동 실행을 설정할 수 없습니다")
        if enabled:
            self._write(path, autostart=True)
        else:
            path.unlink(missing_ok=True)
        return self.status()

    def create_shortcut(self) -> dict:
        path = self._shortcut_path()
        if path is None:
            raise Invalid("이 운영체제에서는 바로가기를 만들 수 없습니다")
        self._write(path, autostart=False)
        return self.status()

    # ------------------------------------------------------------ writing

    def _args(self, autostart: bool) -> list[str]:
        # At logon the app starts in the background; opening the shortcut also opens the browser.
        return self.command + (["--no-browser"] if autostart else [])

    def _write(self, path: Path, autostart: bool) -> None:
        args = self._args(autostart)
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.system == "Windows":
            target = args[0]
            out = self.powershell({"WH_LNK": str(path), "WH_TARGET": target,
                                   "WH_ARGS": subprocess.list2cmdline(args[1:]),
                                   "WH_DIR": str(Path(target).parent), "WH_DESC": APP_NAME})
            if out.returncode != 0 or not path.exists():
                raise Invalid("바로가기를 만들지 못했습니다: " + (out.stderr or out.stdout).strip()[:200])
        elif self.system == "Darwin":
            path.write_bytes(plistlib.dumps({"Label": MAC_LABEL, "ProgramArguments": args, "RunAtLoad": True}))
        else:
            lines = ["[Desktop Entry]", "Type=Application", f"Name={APP_NAME}", "Name[en]=WorkHistory",
                     "Comment=결과물 중심 업무 기록", "Exec=" + " ".join(_desktop_quote(a) for a in args),
                     "Terminal=false", "Categories=Office;Utility;"]
            if autostart:
                lines.append("X-GNOME-Autostart-enabled=true")
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def default() -> Launch:
    return Launch() if sys.platform == "win32" else Launch(folder=lambda name: Path())
