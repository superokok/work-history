"""Import the built-in collector's raw window/idle events into the app's tables.

The collector (collector.py) writes `collector_events`; this step applies the privacy
rules (pause, exclusions, "업무 앱만 기록") and upserts the result into `window_events` /
`afk_events`, which everything else reads. Re-running is idempotent: events are keyed by
(source, raw event id) and an update only ever widens the stored [start, end] range, which
covers the open event growing between syncs.
"""

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

from . import sessionstore
from .classify import OFFWORK_APP, Keyword, TrackRule, is_tracked, matches_any
from .config import IDLE_APPS, SYNC_INTERVAL_S, SYNC_OVERLAP_S
from .db import Database
from .store import load_blocked, load_exclusions, load_tracking, mark_days_dirty

log = logging.getLogger(__name__)

KINDS = ("window", "afk")


@dataclass
class SyncResult:
    ok: bool
    finished_at: float
    error: Optional[str] = None
    imported: dict[str, int] = field(default_factory=lambda: {"window": 0, "afk": 0})
    skipped_private: int = 0


def _get_source(conn: sqlite3.Connection, kind: str) -> sqlite3.Row:
    # One source per kind. Rows imported from ActivityWatch before 2026-10-28 keep their own sources.
    bucket_id = f"workhistory-{kind}"
    conn.execute("INSERT OR IGNORE INTO sources (bucket_id, bucket_created, kind, hostname) "
                 "VALUES (?, 'builtin', ?, '')", (bucket_id, kind))
    return conn.execute("SELECT * FROM sources WHERE bucket_id = ? AND bucket_created = 'builtin'",
                        (bucket_id,)).fetchone()


def store_events(
    conn: sqlite3.Connection,
    source_id: int,
    kind: str,
    events: Iterable[sqlite3.Row],
    exclusions: list[Keyword],
    blocked: list[tuple[float, float]],
    tracking: Optional[list[TrackRule]] = None,
) -> tuple[int, int]:
    """Upsert raw events (collector_events rows); returns (stored, skipped_for_privacy).

    `tracking` None records every app; otherwise windows matching no rule keep
    only their time (app and title are dropped before anything is stored).
    Re-importing an event re-applies the current rules to it.
    Days touched by stored events are marked so their sessions get regrouped.
    """
    stored = skipped = 0
    lo, hi = float("inf"), float("-inf")
    for ev in events:
        raw_id, start, end = ev["id"], ev["start_ts"], ev["end_ts"]
        if any(b_start <= start < b_end for b_start, b_end in blocked):
            skipped += 1
            continue
        if kind == "window":
            app = ev["app"] or "unknown"
            title = ev["title"] or ""
            if matches_any(exclusions, app, title):
                skipped += 1
                continue
            # The lock screen stays as-is: it is idle time, not "업무 외".
            if (tracking is not None and app.casefold() not in IDLE_APPS
                    and not is_tracked(tracking, app, title)):
                app, title = OFFWORK_APP, ""
            conn.execute(
                "INSERT INTO window_events (source_id, aw_id, start_ts, end_ts, app, title) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (source_id, aw_id) DO UPDATE SET "
                "start_ts = MIN(start_ts, excluded.start_ts), "
                "end_ts = MAX(end_ts, excluded.end_ts), "
                "app = excluded.app, title = excluded.title",
                (source_id, raw_id, start, end, app, title),
            )
        else:
            if ev["status"] not in ("afk", "not-afk"):
                continue
            conn.execute(
                "INSERT INTO afk_events (source_id, aw_id, start_ts, end_ts, status) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (source_id, aw_id) DO UPDATE SET "
                "start_ts = MIN(start_ts, excluded.start_ts), "
                "end_ts = MAX(end_ts, excluded.end_ts)",
                (source_id, raw_id, start, end, ev["status"]),
            )
        stored += 1
        lo, hi = min(lo, start), max(hi, end)
    if stored:
        mark_days_dirty(conn, lo, hi)
    return stored, skipped


def sync_once(db: Database, collector=None, now: Optional[float] = None) -> SyncResult:
    """Import raw events from the last cursor (minus an overlap) up to now.

    `collector` (if given) first writes its open events, so the latest stretch is included.
    """
    now = time.time() if now is None else now
    if collector is not None:
        collector.flush()
    result = SyncResult(ok=False, finished_at=now)
    with db.connect() as conn:
        for kind in KINDS:
            source = _get_source(conn, kind)
            start = (source["cursor_ts"] - SYNC_OVERLAP_S) if source["cursor_ts"] else 0.0
            # Overlapping, not only starting after: a long open event is extended every time.
            raw = conn.execute(
                "SELECT id, start_ts, end_ts, app, title, status FROM collector_events "
                "WHERE kind = ? AND end_ts >= ? AND start_ts <= ?", (kind, start, now)).fetchall()
            stored, skipped = store_events(conn, source["id"], kind, raw, load_exclusions(conn),
                                           load_blocked(conn, now), load_tracking(conn))
            conn.execute("UPDATE sources SET cursor_ts = ?, last_sync_at = ? WHERE id = ?",
                         (now, now, source["id"]))
            result.imported[kind] += stored
            result.skipped_private += skipped
    result.ok = True
    log.info("sync ok: window=%d afk=%d skipped=%d",
             result.imported["window"], result.imported["afk"], result.skipped_private)
    return result


def recent_apps(conn: sqlite3.Connection, tracking: list[TrackRule], days: int = 7,
                now: Optional[float] = None) -> list[dict]:
    """App names and focus time from the raw events (the list itself is not stored).

    Lets the user pick which apps to record, including apps that are currently
    recorded only as "업무 외".
    """
    now = time.time() if now is None else now
    totals: dict[str, float] = {}
    titled: dict[str, bool] = {}
    for r in conn.execute("SELECT start_ts, end_ts, app, title FROM collector_events "
                          "WHERE kind = 'window' AND end_ts >= ? AND start_ts <= ?", (now - days * 86400, now)):
        app = r["app"] or "unknown"
        if app.casefold() in IDLE_APPS:
            continue
        totals[app] = totals.get(app, 0.0) + (min(r["end_ts"], now) - max(r["start_ts"], now - days * 86400))
        # Is any of this app's time recorded with the current rules?
        titled[app] = titled.get(app, False) or is_tracked(tracking, app, r["title"])
    return [{"app": app, "seconds": secs, "tracked": titled[app]}
            for app, secs in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0].casefold()))]


def reset_window_cursors(conn: sqlite3.Connection, days: int, now: Optional[float] = None) -> None:
    """Make the next sync re-import the last `days` days of window events.

    Re-imported events get the current tracking rules applied (upsert replaces
    app/title), so a forgotten work app can be recovered while the raw events are kept.
    """
    now = time.time() if now is None else now
    conn.execute("UPDATE sources SET cursor_ts = ? WHERE kind = 'window'", (now - days * 86400,))


class Syncer:
    """Runs sync_once periodically on a background thread."""

    def __init__(self, db: Database, collector=None, interval_s: float = SYNC_INTERVAL_S):
        self.db = db
        self.collector = collector
        self.interval_s = interval_s
        self.last_result: Optional[SyncResult] = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def run_now(self) -> SyncResult:
        with self._lock:
            try:
                self.last_result = sync_once(self.db, self.collector)
                # Regroup sessions in the background so page loads stay fast.
                with self.db.connect() as conn:
                    sessionstore.ensure_all(conn)
            except Exception as e:  # keep the loop alive; never log event content
                log.exception("sync failed")
                self.last_result = SyncResult(ok=False, finished_at=time.time(),
                                              error=f"동기화 오류: {type(e).__name__}")
            return self.last_result

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_now()
            self._wake.wait(self.interval_s)
            self._wake.clear()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
