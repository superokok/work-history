"""Shared database loaders and day/staleness bookkeeping."""

import sqlite3
import time
from datetime import date, datetime, timedelta
from typing import Optional

from .classify import Keyword, Rule, TrackRule
from .normalize import Activity, AfkEvent, WindowEvent, build_activities


class Invalid(ValueError):
    """Bad input from the client."""


class NotFound(LookupError):
    pass


def day_bounds(day: date) -> tuple[float, float]:
    """Local-midnight boundaries of `day` as UTC epoch seconds."""
    start = datetime(day.year, day.month, day.day)
    return start.astimezone().timestamp(), (start + timedelta(days=1)).astimezone().timestamp()


def local_day(ts: float) -> date:
    return datetime.fromtimestamp(ts).date()


def days_between(start_ts: float, end_ts: float) -> list[date]:
    first, last = local_day(start_ts), local_day(max(start_ts, end_ts))
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def load_exclusions(conn: sqlite3.Connection) -> list[Keyword]:
    return [Keyword(r["field"], r["pattern"])
            for r in conn.execute("SELECT field, pattern FROM exclusions")]


def load_blocked(conn: sqlite3.Connection, now: float) -> list[tuple[float, float]]:
    return [(r["start_ts"], r["end_ts"] if r["end_ts"] is not None else now)
            for r in conn.execute("SELECT start_ts, end_ts FROM blocked_ranges")]


def track_mode(conn: sqlite3.Connection) -> str:
    """'all' (record every app) or 'selected' (only apps matching track_rules)."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'track_mode'").fetchone()
    return row["value"] if row and row["value"] in ("all", "selected") else "all"


def load_tracking(conn: sqlite3.Connection) -> Optional[list[TrackRule]]:
    """None when every app is recorded; else the rules a window must match."""
    if track_mode(conn) == "all":
        return None
    return [TrackRule(r["app_pattern"], r["title_pattern"])
            for r in conn.execute("SELECT app_pattern, title_pattern FROM track_rules")]


def load_rules(conn: sqlite3.Connection) -> list[Rule]:
    return [Rule(r["id"], r["project_id"], r["field"], r["pattern"]) for r in conn.execute(
        "SELECT r.id, r.project_id, r.field, r.pattern FROM project_rules r "
        "JOIN projects p ON p.id = r.project_id WHERE p.archived = 0")]


def load_events(conn: sqlite3.Connection, start: float, end: float):
    window = [WindowEvent(r["id"], r["start_ts"], r["end_ts"], r["app"], r["title"], r["project_id"])
              for r in conn.execute(
                  "SELECT id, start_ts, end_ts, app, title, project_id FROM window_events "
                  "WHERE end_ts > ? AND start_ts < ?", (start, end))]
    afk = [AfkEvent(r["start_ts"], r["end_ts"], r["status"]) for r in conn.execute(
        "SELECT start_ts, end_ts, status FROM afk_events WHERE end_ts > ? AND start_ts < ?",
        (start, end))]
    return window, afk


def day_activities(conn: sqlite3.Connection, day: date,
                   now: Optional[float] = None) -> tuple[list[Activity], list[str]]:
    """Active, classified activity for one local day."""
    now = time.time() if now is None else now
    start, end = day_bounds(day)
    window, afk = load_events(conn, start, end)
    return build_activities(window, afk, load_blocked(conn, now), start, end,
                            load_exclusions(conn), load_rules(conn))


# Sessions are derived data. `session_days` remembers, per day, which
# classification version its sessions were computed with; -1 means "new
# activity arrived". Anything that changes classification bumps the version.

def session_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'session_version'").fetchone()
    return int(row["value"]) if row else 0


def bump_session_version(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES ('session_version', '1') "
                 "ON CONFLICT (key) DO UPDATE SET value = CAST(value AS INTEGER) + 1")


def mark_days_dirty(conn: sqlite3.Connection, start_ts: float, end_ts: float) -> None:
    conn.executemany(
        "INSERT INTO session_days (day, version) VALUES (?, -1) "
        "ON CONFLICT (day) DO UPDATE SET version = -1",
        [(d.isoformat(),) for d in days_between(start_ts, end_ts)])
