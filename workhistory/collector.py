"""Window/idle collector (Windows) running inside the app.

Every few seconds it reads the foreground window (app = exe file name, title) and the
time since the last keyboard/mouse input, and keeps them as heartbeat-merged raw events
in `collector_events` for RAW_RETENTION_S. sync.py imports them into window_events /
afk_events with the current rules; the raw events let "다시 적용" and the app list work.

Nothing is recorded while paused or for windows matching an exclusion. Only the window
title and app name are read: no keystrokes, clipboard, screen or document contents.
"""

import logging
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional, Protocol

from .classify import matches_any
from .config import AFK_AFTER_S, COLLECT_FLUSH_S, COLLECT_POLL_S, RAW_RETENTION_S
from .db import Database
from .store import load_exclusions

log = logging.getLogger(__name__)

# A longer gap between two reads (sleep, hibernate, app closed) starts a new event.
MAX_GAP_S = 30


class Probe(Protocol):
    def foreground(self) -> tuple[str, str]:
        """(app, title) of the window in front; ("unknown", "") when it can't be read."""

    def idle_seconds(self) -> float:
        """Seconds since the last keyboard or mouse input."""


class WindowsProbe:
    """Win32 calls through ctypes (no extra packages)."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._ct, self._wt = ctypes, wintypes
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k, u = self._kernel32, self._user32
        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.GetTickCount.restype = wintypes.DWORD

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        self._lii = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)

    def foreground(self) -> tuple[str, str]:
        ct, wt, u = self._ct, self._wt, self._user32
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return "unknown", ""
        n = u.GetWindowTextLengthW(hwnd)
        buf = ct.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        pid = wt.DWORD()
        u.GetWindowThreadProcessId(hwnd, ct.byref(pid))
        return self._exe_name(pid.value) or "unknown", buf.value

    def _exe_name(self, pid: int) -> str:
        # Limited query rights also work for most elevated (admin) processes.
        handle = self._kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            size = self._wt.DWORD(1024)
            buf = self._ct.create_unicode_buffer(size.value)
            if not self._kernel32.QueryFullProcessImageNameW(handle, 0, buf, self._ct.byref(size)):
                return ""
            return buf.value.replace("/", "\\").rsplit("\\", 1)[-1]
        finally:
            self._kernel32.CloseHandle(handle)

    def idle_seconds(self) -> float:
        if not self._user32.GetLastInputInfo(self._ct.byref(self._lii)):
            return 0.0
        # Both are 32-bit millisecond tick counts; the subtraction survives the 49-day wrap.
        return ((self._kernel32.GetTickCount() - self._lii.dwTime) & 0xFFFFFFFF) / 1000


def default_probe() -> Optional[Probe]:
    """The Windows probe, or None when it can't be used (no window activity is recorded then)."""
    if sys.platform != "win32":
        return None
    try:
        return WindowsProbe()
    except (OSError, AttributeError):
        log.exception("built-in collector unavailable")
        return None


@dataclass
class _Open:
    """The event being extended by heartbeats."""
    id: Optional[int]
    start: float
    end: float
    key: tuple


class Collector:
    """Reads the probe every COLLECT_POLL_S on a background thread."""

    def __init__(self, db: Database, probe: Probe, poll_s: float = COLLECT_POLL_S,
                 flush_s: float = COLLECT_FLUSH_S):
        self.db = db
        self.probe = probe
        self.poll_s = poll_s
        self.flush_s = flush_s
        self.last_error: Optional[str] = None
        self._open: dict[str, Optional[_Open]] = {"window": None, "afk": None}
        self._dirty = False
        self._last_flush = 0.0
        self._last_prune = 0.0
        self._lock = threading.Lock()  # tick() runs on the collector thread, flush() on the sync thread
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ one read

    def tick(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        with self._lock, self.db.connect() as conn:
            if conn.execute("SELECT 1 FROM blocked_ranges WHERE kind = 'pause' AND end_ts IS NULL").fetchone():
                self._close_all(conn)
                return
            app, title = self.probe.foreground()
            idle = max(0.0, self.probe.idle_seconds())
            if matches_any(load_exclusions(conn), app, title):
                self._close(conn, "window")
            else:
                self._beat(conn, "window", now, now, (app, title))
            if idle >= AFK_AFTER_S:
                # Away since the last input: the active stretch ends there and the away one starts.
                last_input = now - idle
                current = self._open["afk"]
                if current and current.key == ("not-afk",):
                    current.end = max(current.start, min(current.end, last_input))
                    self._persist(conn, "afk", current)
                    self._open["afk"] = None
                self._beat(conn, "afk", now, last_input, ("afk",))
            else:
                current = self._open["afk"]
                if current and current.key == ("afk",):
                    current.end = max(current.start, now - idle)
                    self._persist(conn, "afk", current)
                    self._open["afk"] = None
                self._beat(conn, "afk", now, now - idle if current else now, ("not-afk",))
            if self._dirty and now - self._last_flush >= self.flush_s:
                self._flush(conn, now)
            if now - self._last_prune >= 3600:
                conn.execute("DELETE FROM collector_events WHERE end_ts < ?", (now - RAW_RETENTION_S,))
                self._last_prune = now

    def _beat(self, conn: sqlite3.Connection, kind: str, now: float, start: float, key: tuple) -> None:
        current = self._open[kind]
        if current and current.key == key and now - current.end <= MAX_GAP_S:
            current.end = now
            self._dirty = True
            return
        if current:
            self._persist(conn, kind, current)
        opened = _Open(None, min(start, now), now, key)
        self._persist(conn, kind, opened)
        self._open[kind] = opened

    def _persist(self, conn: sqlite3.Connection, kind: str, ev: _Open) -> None:
        if ev.id is None:
            app, title = ev.key if kind == "window" else ("", "")
            status = ev.key[0] if kind == "afk" else ""
            ev.id = conn.execute(
                "INSERT INTO collector_events (kind, start_ts, end_ts, app, title, status) "
                "VALUES (?, ?, ?, ?, ?, ?)", (kind, ev.start, ev.end, app, title, status)).lastrowid
        else:
            conn.execute("UPDATE collector_events SET end_ts = ? WHERE id = ?", (ev.end, ev.id))

    def flush(self) -> None:
        """Write the open events' latest end time (before a sync reads them)."""
        with self._lock, self.db.connect() as conn:
            self._flush(conn, time.time())

    def _flush(self, conn: sqlite3.Connection, now: float) -> None:
        for kind, ev in self._open.items():
            if ev:
                self._persist(conn, kind, ev)
        self._dirty = False
        self._last_flush = now

    def _close(self, conn: sqlite3.Connection, kind: str) -> None:
        if self._open[kind]:
            self._persist(conn, kind, self._open[kind])
            self._open[kind] = None

    def _close_all(self, conn: sqlite3.Connection) -> None:
        for kind in self._open:
            self._close(conn, kind)
        self._dirty = False

    # ------------------------------------------------------------ thread

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
                self.last_error = None
            except Exception as e:  # keep collecting; never log window titles
                log.exception("collector tick failed")
                self.last_error = f"창 기록 오류: {type(e).__name__}"
            self._stop.wait(self.poll_s)
        try:
            with self._lock, self.db.connect() as conn:
                self._close_all(conn)
        except Exception:
            log.exception("collector final flush failed")

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="collector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

