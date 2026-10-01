"""Persisted work sessions: regrouping and user edits.

Rules that keep user edits safe:
- Unedited, unlocked sessions are regenerated freely.
- An edited (title/project/note/excluded) but unlocked session follows its
  regrouped counterpart, matched by start time, so a session that is still
  going on keeps growing after you rename it.
- If no counterpart exists any more, the edited session is locked at its
  current range instead of being dropped.
- Locked sessions (from merge/split) keep their range; regrouping only
  happens outside them.
"""

import sqlite3
import threading
import time
from dataclasses import replace
from datetime import date
from typing import Any, Optional

from . import store
from .normalize import Activity, merge_intervals, subtract
from .sessions import auto_title, dominant_project, group_activities, offwork_seconds
from .store import Invalid, NotFound

_refresh_lock = threading.Lock()


def _is_edited(row: sqlite3.Row) -> bool:
    return bool(row["title_edited"] or row["project_manual"] or row["note"] or row["excluded"])


def activities_within(activities: list[Activity], start: float, end: float) -> list[Activity]:
    out = []
    for a in activities:
        s, e = max(a.start, start), min(a.end, end)
        if s < e:
            out.append(a if (s, e) == (a.start, a.end) else replace(a, start=s, end=e))
    return out


def refresh_day(conn: sqlite3.Connection, day: date, now: Optional[float] = None,
                activities: Optional[list[Activity]] = None) -> None:
    with _refresh_lock:
        if activities is None:
            activities, _ = store.day_activities(conn, day, now)
        _refresh(conn, day, activities)
        conn.execute("INSERT INTO session_days (day, version) VALUES (?, ?) "
                     "ON CONFLICT (day) DO UPDATE SET version = excluded.version",
                     (day.isoformat(), store.session_version(conn)))


def _refresh(conn: sqlite3.Connection, day: date, activities: list[Activity]) -> None:
    ds, ts = day.isoformat(), time.time()
    while True:
        rows = conn.execute("SELECT * FROM sessions WHERE day = ? ORDER BY start_ts", (ds,)).fetchall()
        locked = [r for r in rows if r["locked"]]
        unlocked = [r for r in rows if not r["locked"]]
        locked_ranges = merge_intervals((r["start_ts"], r["end_ts"]) for r in locked)
        free = [replace(a, start=s, end=e) for a in activities
                for s, e in subtract([(a.start, a.end)], locked_ranges)]
        groups = group_activities(free)

        matches: dict[int, sqlite3.Row] = {}
        used: set[int] = set()
        for gi, group in enumerate(groups):
            for r in unlocked:
                if r["id"] not in used and abs(r["start_ts"] - group[0].start) < 1.0:
                    matches[gi] = r
                    used.add(r["id"])
                    break
        orphans = [r for r in unlocked if r["id"] not in used and _is_edited(r)]
        if not orphans:
            break
        # Never lose user edits: freeze them where they are and regroup around them.
        conn.executemany("UPDATE sessions SET locked = 1 WHERE id = ?", [(r["id"],) for r in orphans])

    for r in unlocked:
        if r["id"] not in used:
            conn.execute("DELETE FROM sessions WHERE id = ?", (r["id"],))

    for gi, group in enumerate(groups):
        start, end = group[0].start, group[-1].end
        active, offwork = sum(a.duration for a in group), offwork_seconds(group)
        title, project = auto_title(group), dominant_project(group)
        r = matches.get(gi)
        if r is None:
            conn.execute(
                "INSERT INTO sessions (day, start_ts, end_ts, active_s, offwork_s, title, project_id, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ds, start, end, active, offwork, title, project, ts, ts))
        else:
            _update_auto(conn, r, start, end, active, offwork, title, project)

    for r in locked:
        inside = activities_within(activities, r["start_ts"], r["end_ts"])
        _update_auto(conn, r, r["start_ts"], r["end_ts"], sum(a.duration for a in inside),
                     offwork_seconds(inside), auto_title(inside) or r["title"], dominant_project(inside))


def _update_auto(conn, row, start, end, active, offwork, title, project) -> None:
    conn.execute(
        "UPDATE sessions SET start_ts = ?, end_ts = ?, active_s = ?, offwork_s = ?, "
        "title = CASE WHEN title_edited THEN title ELSE ? END, "
        "project_id = CASE WHEN project_manual THEN project_id ELSE ? END WHERE id = ?",
        (start, end, active, offwork, title, project, row["id"]))


def ensure_day(conn: sqlite3.Connection, day: date, now: Optional[float] = None) -> None:
    row = conn.execute("SELECT version FROM session_days WHERE day = ?", (day.isoformat(),)).fetchone()
    if row is None or row["version"] != store.session_version(conn):
        refresh_day(conn, day, now)


def ensure_all(conn: sqlite3.Connection, now: Optional[float] = None) -> int:
    """Regroup every stale day; returns how many days were refreshed."""
    stale = [r["day"] for r in conn.execute(
        "SELECT day FROM session_days WHERE version != ? ORDER BY day", (store.session_version(conn),))]
    for d in stale:
        refresh_day(conn, date.fromisoformat(d), now)
        # Sessions are derived data: commit per day so a long regroup (e.g. after
        # a rule change over a year of history) never holds the write lock for long.
        conn.commit()
    return len(stale)


# ---------------------------------------------------------------- user edits

def get_session(conn: sqlite3.Connection, session_id: int) -> dict:
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        raise NotFound("세션을 찾을 수 없습니다. 화면을 새로 고쳐 주세요.")
    return dict(row)


def update_session(conn: sqlite3.Connection, session_id: int, data: dict) -> dict:
    s = get_session(conn, session_id)
    fields: dict[str, Any] = {}
    if "title" in data:
        if not isinstance(data["title"], str) or len(data["title"]) > 300:
            raise Invalid("제목이 올바르지 않습니다")
        title = data["title"].strip()
        # Blank title = go back to the automatic title.
        fields.update(title=title or s["title"], title_edited=1 if title else 0)
    if "project_id" in data:
        value = data["project_id"]
        if value == "auto":
            fields.update(project_manual=0)
        elif value in (None, ""):
            fields.update(project_id=None, project_manual=1)
        else:
            try:
                pid = int(value)
            except (TypeError, ValueError):
                raise Invalid("project_id가 올바르지 않습니다") from None
            if conn.execute("SELECT 1 FROM projects WHERE id = ?", (pid,)).fetchone() is None:
                raise NotFound("프로젝트를 찾을 수 없습니다")
            fields.update(project_id=pid, project_manual=1)
    if "note" in data:
        if not isinstance(data["note"], str) or len(data["note"]) > 10000:
            raise Invalid("메모가 올바르지 않습니다")
        fields["note"] = data["note"].strip()
    if "excluded" in data:
        fields["excluded"] = 1 if data["excluded"] else 0
    if not fields:
        raise Invalid("변경할 내용이 없습니다")
    fields["updated_at"] = time.time()
    conn.execute(f"UPDATE sessions SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                 [*fields.values(), session_id])
    refresh_day(conn, date.fromisoformat(s["day"]))
    return get_session(conn, session_id)


def merge_sessions(conn: sqlite3.Connection, ids: Any) -> dict:
    """Merge the given sessions and everything between them into one locked session."""
    if not isinstance(ids, list) or len(ids) < 2:
        raise Invalid("병합할 세션을 두 개 이상 선택하세요")
    try:
        picked = [get_session(conn, int(i)) for i in ids]
    except (TypeError, ValueError):
        raise Invalid("세션 id가 올바르지 않습니다") from None
    days = {s["day"] for s in picked}
    if len(days) != 1:
        raise Invalid("같은 날짜의 세션만 병합할 수 있습니다")
    day = days.pop()
    lo, hi = min(s["start_ts"] for s in picked), max(s["end_ts"] for s in picked)
    members = [dict(r) for r in conn.execute(
        "SELECT * FROM sessions WHERE day = ? AND end_ts > ? AND start_ts < ? ORDER BY start_ts",
        (day, lo, hi))]
    first_title = next((m["title"] for m in members if m["title_edited"]), None)
    first_project = next((m for m in members if m["project_manual"]), None)
    note = "\n".join(m["note"] for m in members if m["note"])
    now = time.time()
    conn.executemany("DELETE FROM sessions WHERE id = ?", [(m["id"],) for m in members])
    new_id = conn.execute(
        "INSERT INTO sessions (day, start_ts, end_ts, title, title_edited, project_id, project_manual, "
        "note, excluded, locked, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (day, lo, hi, first_title or "", 1 if first_title else 0,
         first_project["project_id"] if first_project else None, 1 if first_project else 0,
         note, 1 if all(m["excluded"] for m in members) else 0, now, now)).lastrowid
    refresh_day(conn, date.fromisoformat(day))
    return get_session(conn, new_id)


def split_session(conn: sqlite3.Connection, session_id: int, at: Any) -> list[dict]:
    s = get_session(conn, session_id)
    try:
        at = float(at)
    except (TypeError, ValueError):
        raise Invalid("분할 시각이 올바르지 않습니다") from None
    if not s["start_ts"] < at < s["end_ts"]:
        raise Invalid("분할 시각은 세션 안에 있어야 합니다")
    now = time.time()
    conn.execute("UPDATE sessions SET end_ts = ?, locked = 1, updated_at = ? WHERE id = ?",
                 (at, now, session_id))
    second = conn.execute(
        "INSERT INTO sessions (day, start_ts, end_ts, project_id, project_manual, excluded, locked, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (s["day"], at, s["end_ts"], s["project_id"] if s["project_manual"] else None,
         s["project_manual"], s["excluded"], now, now)).lastrowid
    refresh_day(conn, date.fromisoformat(s["day"]))
    return [get_session(conn, session_id), get_session(conn, second)]
