"""The dashboard: where the time of a day or a week went, and what came out of it.

Built from the same sessions as the timeline (excluded sessions do not count) and the
recorded file changes of the work folders. Kinds of activity come from categories.py
(rules, no AI). Projects are not used: the app no longer asks the user to classify.
"""

import sqlite3
import time
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from . import categories, sessionstore, store
from .classify import OFFWORK_APP
from .sessions import app_label

STRIP_MERGE_GAP_S = 120  # same-kind activities this close together form one block of the day strip
STRIP_MIN_BLOCK_S = 180  # shorter blocks are folded into the one before (the strip only)
TOP_WORK = 8             # longest sessions (by title) shown under "오래 한 일"


def _empty(day: date) -> dict:
    return {"day": day, "by_kind": {}, "apps": {}, "work": {}, "blocks": [], "folders": []}


def _day(conn: sqlite3.Connection, day: date, now: Optional[float]) -> dict:
    """Time by kind, by app and by session title, the day strip and the file changes, for one day."""
    if day > store.local_day(time.time() if now is None else now):
        return _empty(day)  # not yet
    activities, _ = store.day_activities(conn, day, now)
    sessionstore.ensure_day(conn, day, now, activities)
    by_kind: dict[str, float] = defaultdict(float)
    apps: dict[str, float] = defaultdict(float)
    work: dict[str, float] = defaultdict(float)  # session title -> work seconds
    blocks: list[list] = []
    for s in conn.execute("SELECT * FROM sessions WHERE day = ? AND excluded = 0 ORDER BY start_ts",
                          (day.isoformat(),)):
        worked = 0.0
        for a in sessionstore.activities_within(activities, s["start_ts"], s["end_ts"]):
            kind = categories.category(a.app, a.title)
            by_kind[kind] += a.duration
            if a.app != OFFWORK_APP:
                apps[a.app] += a.duration
                worked += a.duration
            if blocks and blocks[-1][2] == kind and a.start - blocks[-1][1] <= STRIP_MERGE_GAP_S:
                blocks[-1][1] = max(blocks[-1][1], a.end)
            else:
                blocks.append([a.start, a.end, kind])
        if worked > 0 and s["title"]:
            work[s["title"]] += worked
    start, end = store.day_bounds(day)
    folders = conn.execute(
        "SELECT c.folder_id, f.label, COUNT(*) AS n, "
        "SUM(c.files_added + c.files_modified + c.files_deleted) AS files, "
        "SUM(c.lines_added) AS added, SUM(c.lines_removed) AS removed "
        "FROM changes c JOIN watched_folders f ON f.id = c.folder_id "
        "WHERE c.baseline = 0 AND c.ts >= ? AND c.ts < ? GROUP BY c.folder_id", (start, end)).fetchall()
    return {"day": day, "by_kind": by_kind, "apps": apps, "work": work, "blocks": _smooth(blocks),
            "folders": folders}


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


def _kinds(by_kind: dict[str, float]) -> dict[str, int]:
    return {k: round(by_kind.get(k, 0.0)) for k in categories.KEYS}


def _totals(by_kind: dict[str, float]) -> dict:
    total = sum(by_kind.values())
    work = sum(v for k, v in by_kind.items() if k in categories.WORK)
    return {"active_s": round(total), "work_s": round(work), "offwork_s": round(by_kind.get("offwork", 0.0))}


def _sum_kinds(days: list[dict]) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for d in days:
        for k, v in d["by_kind"].items():
            out[k] += v
    return out


def _folders(days: list[dict]) -> list[dict]:
    """File changes per work folder over several days, most files first; `day` is the day with the
    most files changed (to open)."""
    acc: dict[int, dict] = {}
    best: dict[int, int] = {}
    for d in days:
        for r in d["folders"]:
            f = acc.setdefault(r["folder_id"], {"folder_id": r["folder_id"], "label": r["label"], "changes": 0,
                                                "files": 0, "lines_added": 0, "lines_removed": 0})
            if r["folder_id"] not in best or (r["files"] or 0) > best[r["folder_id"]]:
                best[r["folder_id"]] = r["files"] or 0
                f["day"] = d["day"].isoformat()
            f["changes"] += r["n"]
            f["files"] += r["files"] or 0
            f["lines_added"] += r["added"] or 0
            f["lines_removed"] += r["removed"] or 0
    return sorted(acc.values(), key=lambda f: (-f["files"], -f["lines_added"] - f["lines_removed"], f["label"]))


def _work(days: list[dict]) -> list[dict]:
    """The session titles that took the most time; `day` is the day with the most of it (to open)."""
    total: dict[str, float] = defaultdict(float)
    best: dict[str, tuple[float, date]] = {}
    for d in days:
        for title, sec in d["work"].items():
            total[title] += sec
            if title not in best or sec > best[title][0]:
                best[title] = (sec, d["day"])
    top = sorted(total.items(), key=lambda kv: -kv[1])[:TOP_WORK]
    return [{"title": t, "seconds": round(sec), "day": best[t][1].isoformat(),
             "days": sum(1 for d in days if t in d["work"])} for t, sec in top]


def _top_app(days: list[dict]) -> Optional[dict]:
    apps: dict[str, float] = defaultdict(float)
    for d in days:
        for app, sec in d["apps"].items():
            apps[app] += sec
    if not apps:
        return None
    app, sec = max(apps.items(), key=lambda kv: kv[1])
    return {"app": app, "label": app_label(app), "seconds": round(sec)}


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
    by_kind = _sum_kinds(chosen)
    folders = _folders(chosen)
    out = {
        "span": span,
        "day": day.isoformat(),
        "start": chosen[0]["day"].isoformat(),
        "end": chosen[-1]["day"].isoformat(),
        "categories": [{"key": k, "label": label, "color": color} for k, label, color in categories.CATEGORIES],
        "work_keys": sorted(categories.WORK),
        "totals": _totals(by_kind),
        "previous": _totals(_sum_kinds(previous)),
        "by_kind": _kinds(by_kind),
        "files": sum(f["files"] for f in folders),
        "lines_added": sum(f["lines_added"] for f in folders),
        "lines_removed": sum(f["lines_removed"] for f in folders),
        "folders": folders,
        "work": _work(chosen),
        "top_app": _top_app(chosen),
        "week": [{"day": d["day"].isoformat(), "by_kind": _kinds(d["by_kind"])} for d in week],
    }
    if span == "day":
        out["blocks"] = [{"start": b[0], "end": b[1], "kind": b[2]} for b in chosen[0]["blocks"]]
    return out
