"""The dashboard: where the time of a day or a week went, by kind of activity and by project.

Built from the same sessions as the timeline (excluded sessions do not count) and the
recorded file changes. Kinds of activity come from categories.py (rules, no AI).
"""

import sqlite3
import time
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from . import categories, sessionstore, store
from .classify import OFFWORK_APP

STRIP_MERGE_GAP_S = 120  # same-kind activities this close together form one block of the day strip
STRIP_MIN_BLOCK_S = 180  # shorter blocks are folded into the one before (the strip only)
TOP_TITLES = 3


def _day(conn: sqlite3.Connection, day: date, now: Optional[float]) -> dict:
    """Time by kind and by project, the day strip and the sessions behind them, for one day."""
    if day > store.local_day(time.time() if now is None else now):
        return {"day": day, "by_kind": {}, "projects": {}, "blocks": [], "changes": []}  # not yet
    activities, _ = store.day_activities(conn, day, now)
    sessionstore.ensure_day(conn, day, now, activities)
    by_kind: dict[str, float] = defaultdict(float)
    projects: dict[Optional[int], dict] = {}
    blocks: list[list] = []
    for s in conn.execute("SELECT * FROM sessions WHERE day = ? AND excluded = 0 ORDER BY start_ts",
                          (day.isoformat(),)):
        inside = sessionstore.activities_within(activities, s["start_ts"], s["end_ts"])
        work = 0.0
        for a in inside:
            kind = categories.category(a.app, a.title)
            by_kind[kind] += a.duration
            if a.app != OFFWORK_APP:
                work += a.duration
            if blocks and blocks[-1][2] == kind and a.start - blocks[-1][1] <= STRIP_MERGE_GAP_S:
                blocks[-1][1] = max(blocks[-1][1], a.end)
            else:
                blocks.append([a.start, a.end, kind])
        if work > 0:
            p = projects.setdefault(s["project_id"], {"seconds": 0.0, "titles": defaultdict(float)})
            p["seconds"] += work
            p["titles"][s["title"]] += work
    blocks = _smooth(blocks)
    start, end = store.day_bounds(day)
    changes = conn.execute(
        "SELECT project_id, COUNT(*) AS n, SUM(files_added + files_modified + files_deleted) AS files, "
        "SUM(lines_added) AS added, SUM(lines_removed) AS removed FROM changes "
        "WHERE baseline = 0 AND ts >= ? AND ts < ? GROUP BY project_id", (start, end)).fetchall()
    return {"day": day, "by_kind": by_kind, "projects": projects, "blocks": blocks, "changes": changes}


def _smooth(blocks: list[list]) -> list[list]:
    """Fold blocks shorter than STRIP_MIN_BLOCK_S into the block before them, so the strip shows
    the shape of the day rather than every window switch (totals are counted before this)."""
    out: list[list] = []
    for b in blocks:
        if out and b[1] - b[0] < STRIP_MIN_BLOCK_S and b[0] - out[-1][1] <= STRIP_MERGE_GAP_S:
            out[-1][1] = max(out[-1][1], b[1])
        elif out and out[-1][2] == b[2] and b[0] - out[-1][1] <= STRIP_MERGE_GAP_S:
            out[-1][1] = max(out[-1][1], b[1])
        else:
            out.append(list(b))
    return out


def _kinds(by_kind: dict[str, float]) -> dict[str, float]:
    return {k: round(by_kind.get(k, 0.0)) for k in categories.KEYS}


def _totals(by_kind: dict[str, float]) -> dict:
    total = sum(by_kind.values())
    work = sum(v for k, v in by_kind.items() if k in categories.WORK)
    return {"active_s": round(total), "work_s": round(work), "surf_s": round(by_kind.get("surf", 0.0)),
            "offwork_s": round(by_kind.get("offwork", 0.0))}


def _merge(days: list[dict]) -> tuple[dict, dict, dict]:
    """Sum several days: time by kind, projects (seconds, session titles) and file changes by project."""
    by_kind: dict[str, float] = defaultdict(float)
    projects: dict[Optional[int], dict] = {}
    changes: dict[Optional[int], dict] = defaultdict(lambda: {"files": 0, "added": 0, "removed": 0})
    for d in days:
        for k, v in d["by_kind"].items():
            by_kind[k] += v
        for pid, p in d["projects"].items():
            q = projects.setdefault(pid, {"seconds": 0.0, "titles": defaultdict(float)})
            q["seconds"] += p["seconds"]
            for title, sec in p["titles"].items():
                q["titles"][title] += sec
        for c in d["changes"]:
            ch = changes[c["project_id"]]
            ch["files"] += c["files"] or 0
            ch["added"] += c["added"] or 0
            ch["removed"] += c["removed"] or 0
    return by_kind, projects, changes


def dashboard(conn: sqlite3.Connection, day: date, span: str = "day", now: Optional[float] = None) -> dict:
    """`span` "day": that day; "week": its Monday-to-Sunday week. The week's days are always included
    (for the trend chart). For comparison: the day before, or last week up to the same weekday
    (a week in progress is compared with as many days of the week before)."""
    if span not in ("day", "week"):
        raise ValueError(span)
    monday = day - timedelta(days=day.weekday())
    week = [_day(conn, monday + timedelta(days=i), now) for i in range(7)]
    if span == "day":
        chosen = [week[day.weekday()]]
        previous = [_day(conn, day - timedelta(days=1), now)]
    else:
        chosen = week
        today = store.local_day(time.time() if now is None else now)
        days = min(7, max(1, (today - monday).days + 1))
        previous = [_day(conn, monday - timedelta(days=7 - i), now) for i in range(days)]
    by_kind, projects, changes = _merge(chosen)
    prev_kind, _, _ = _merge(previous)
    names = {r["id"]: (r["name"], r["color"]) for r in conn.execute("SELECT id, name, color FROM projects")}

    rows = []
    for pid in set(projects) | set(changes):
        p = projects.get(pid, {"seconds": 0.0, "titles": {}})
        ch = changes.get(pid, {"files": 0, "added": 0, "removed": 0})
        name, color = names.get(pid, ("미분류", "#9aa3b5"))
        titles = sorted(p["titles"].items(), key=lambda kv: -kv[1])[:TOP_TITLES]
        rows.append({"project_id": pid, "name": name, "color": color, "seconds": round(p["seconds"]),
                     "files": ch["files"], "lines_added": ch["added"], "lines_removed": ch["removed"],
                     "titles": [t for t, _ in titles if t]})
    rows.sort(key=lambda r: (-r["seconds"], -r["files"]))

    out = {
        "span": span,
        "day": day.isoformat(),
        "start": chosen[0]["day"].isoformat(),
        "end": chosen[-1]["day"].isoformat(),
        "categories": [{"key": k, "label": label, "color": color} for k, label, color in categories.CATEGORIES],
        "work_keys": sorted(categories.WORK),
        "totals": _totals(by_kind),
        "previous": _totals(prev_kind),
        "by_kind": _kinds(by_kind),
        "files": sum(c["files"] for c in changes.values()),
        "lines_added": sum(c["added"] for c in changes.values()),
        "lines_removed": sum(c["removed"] for c in changes.values()),
        "projects": rows,
        "week": [{"day": d["day"].isoformat(), "by_kind": _kinds(d["by_kind"])} for d in week],
    }
    if span == "day":
        out["blocks"] = [{"start": b[0], "end": b[1], "kind": b[2]} for b in chosen[0]["blocks"]]
    return out
