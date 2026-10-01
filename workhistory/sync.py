"""Import window/AFK events from ActivityWatch into the local database.

Re-running a sync is idempotent: events are keyed by (source, aw_id) and an
update only ever widens the stored [start, end] range, which covers both AW's
range trimming and heartbeat extension of the latest event.
"""

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from .awclient import AWClient, AWError, parse_iso
from . import sessionstore
from .classify import OFFWORK_APP, Keyword, TrackRule, is_tracked, matches_any
from .config import IDLE_APPS
from .config import (DEFAULT_BACKFILL_DAYS, RETRY_INTERVAL_S, SYNC_CHUNK_S, SYNC_INTERVAL_S,
                     SYNC_OVERLAP_S)
from .db import Database
from .store import load_blocked, load_exclusions, load_tracking, mark_days_dirty

log = logging.getLogger(__name__)

BUCKET_KINDS = {"currentwindow": "window", "afkstatus": "afk"}


@dataclass
class SyncResult:
    ok: bool
    finished_at: float
    error: Optional[str] = None
    aw_version: Optional[str] = None
    hostname: Optional[str] = None
    imported: dict[str, int] = field(default_factory=lambda: {"window": 0, "afk": 0})
    skipped_private: int = 0
    buckets_found: dict[str, bool] = field(default_factory=lambda: {"window": False, "afk": False})


def _get_source(conn: sqlite3.Connection, bucket: dict, kind: str) -> sqlite3.Row:
    conn.execute(
        "INSERT OR IGNORE INTO sources (bucket_id, bucket_created, kind, hostname) "
        "VALUES (?, ?, ?, ?)",
        (bucket["id"], bucket.get("created") or "", kind, bucket.get("hostname") or ""),
    )
    return conn.execute(
        "SELECT * FROM sources WHERE bucket_id = ? AND bucket_created = ?",
        (bucket["id"], bucket.get("created") or ""),
    ).fetchone()


def _parse_event(raw: dict) -> Optional[tuple[int, float, float, dict]]:
    try:
        aw_id = int(raw["id"])
        start = parse_iso(raw["timestamp"])
        end = start + max(float(raw.get("duration") or 0), 0.0)
        data = raw.get("data") or {}
    except (KeyError, TypeError, ValueError):
        return None
    return aw_id, start, end, data


def store_events(
    conn: sqlite3.Connection,
    source_id: int,
    kind: str,
    raw_events: list[dict],
    exclusions: list[Keyword],
    blocked: list[tuple[float, float]],
    tracking: Optional[list[TrackRule]] = None,
) -> tuple[int, int]:
    """Upsert events; returns (stored, skipped_for_privacy).

    `tracking` None records every app; otherwise windows matching no rule keep
    only their time (app and title are dropped before anything is stored).
    Re-importing an event re-applies the current rules to it.
    Days touched by stored events are marked so their sessions get regrouped.
    """
    stored = skipped = 0
    lo, hi = float("inf"), float("-inf")
    for raw in raw_events:
        parsed = _parse_event(raw)
        if parsed is None:
            continue
        aw_id, start, end, data = parsed
        if any(b_start <= start < b_end for b_start, b_end in blocked):
            skipped += 1
            continue
        if kind == "window":
            app = str(data.get("app") or "unknown")
            title = str(data.get("title") or "")
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
                (source_id, aw_id, start, end, app, title),
            )
        else:
            status = str(data.get("status") or "")
            if status not in ("afk", "not-afk"):
                continue
            conn.execute(
                "INSERT INTO afk_events (source_id, aw_id, start_ts, end_ts, status) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (source_id, aw_id) DO UPDATE SET "
                "start_ts = MIN(start_ts, excluded.start_ts), "
                "end_ts = MAX(end_ts, excluded.end_ts)",
                (source_id, aw_id, start, end, status),
            )
        stored += 1
        lo, hi = min(lo, start), max(hi, end)
    if stored:
        mark_days_dirty(conn, lo, hi)
    return stored, skipped


def sync_once(
    db: Database,
    client: AWClient,
    now: Optional[float] = None,
    backfill_days: int = DEFAULT_BACKFILL_DAYS,
) -> SyncResult:
    now = time.time() if now is None else now
    result = SyncResult(ok=False, finished_at=now)
    try:
        info = client.info()
        buckets = client.buckets()
    except AWError as e:
        result.error = str(e)
        return result

    result.aw_version = info.get("version")
    result.hostname = info.get("hostname")
    selected = [
        (bucket_id, {**b, "id": bucket_id}, BUCKET_KINDS[b.get("type")])
        for bucket_id, b in sorted(buckets.items())
        if b.get("type") in BUCKET_KINDS and b.get("hostname") == result.hostname
    ]

    with db.connect() as conn:
        for bucket_id, bucket, kind in selected:
            result.buckets_found[kind] = True
            source = _get_source(conn, bucket, kind)
            cursor = source["cursor_ts"]
            start = (cursor - SYNC_OVERLAP_S) if cursor else now - backfill_days * 86400
            while start < now:
                end = min(start + SYNC_CHUNK_S, now)
                try:
                    raw = client.events(bucket_id, start, end)
                except AWError as e:
                    result.error = str(e)
                    return result
                stored, skipped = store_events(
                    conn, source["id"], kind, raw, load_exclusions(conn), load_blocked(conn, now),
                    load_tracking(conn),
                )
                conn.execute(
                    "UPDATE sources SET cursor_ts = ?, last_sync_at = ? WHERE id = ?",
                    (end, now, source["id"]),
                )
                # Commit per chunk so an interruption keeps the progress made so far.
                conn.commit()
                result.imported[kind] += stored
                result.skipped_private += skipped
                start = end

    result.ok = True
    log.info("sync ok: window=%d afk=%d skipped=%d",
             result.imported["window"], result.imported["afk"], result.skipped_private)
    return result


def recent_apps(client: AWClient, tracking: list[TrackRule], days: int = 7,
                now: Optional[float] = None) -> list[dict]:
    """App names and focus time from ActivityWatch's own data (never stored here).

    Lets the user pick which apps to record, including apps that are currently
    recorded only as "업무 외".
    """
    now = time.time() if now is None else now
    hostname = client.info().get("hostname")
    totals: dict[str, float] = {}
    titled: dict[str, bool] = {}
    for bucket_id, b in client.buckets().items():
        if b.get("type") != "currentwindow" or b.get("hostname") != hostname:
            continue
        for raw in client.events(bucket_id, now - days * 86400, now):
            parsed = _parse_event(raw)
            if parsed is None:
                continue
            _, start, end, data = parsed
            app = str(data.get("app") or "unknown")
            if app.casefold() in IDLE_APPS:
                continue
            totals[app] = totals.get(app, 0.0) + (end - start)
            # Is any of this app's time recorded with the current rules?
            titled[app] = titled.get(app, False) or is_tracked(tracking, app, str(data.get("title") or ""))
    return [{"app": app, "seconds": secs, "tracked": titled[app]}
            for app, secs in sorted(totals.items(), key=lambda kv: -kv[1])]


def reset_window_cursors(conn: sqlite3.Connection, days: int, now: Optional[float] = None) -> None:
    """Make the next sync re-import the last `days` days of window events.

    Re-imported events get the current tracking rules applied (upsert replaces
    app/title), so a forgotten work app can be recovered from ActivityWatch.
    """
    now = time.time() if now is None else now
    conn.execute("UPDATE sources SET cursor_ts = ? WHERE kind = 'window'", (now - days * 86400,))


class Syncer:
    """Runs sync_once periodically on a background thread."""

    def __init__(self, db: Database, client: AWClient, backfill_days: int = DEFAULT_BACKFILL_DAYS,
                 interval_s: float = SYNC_INTERVAL_S):
        self.db = db
        self.client = client
        self.backfill_days = backfill_days
        self.interval_s = interval_s
        self.last_result: Optional[SyncResult] = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def run_now(self) -> SyncResult:
        with self._lock:
            try:
                self.last_result = sync_once(self.db, self.client, backfill_days=self.backfill_days)
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
            result = self.run_now()
            # Retry sooner while ActivityWatch is down (e.g. still starting at login).
            self._wake.wait(self.interval_s if result.ok else min(self.interval_s, RETRY_INTERVAL_S))
            self._wake.clear()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="aw-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
