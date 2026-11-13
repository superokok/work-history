"""The dashboard: where the time of a day or a week went, and what came out of it.

Built from the same activity as the timeline, the work records (time per work folder,
work.py) and the recorded file changes of the work folders. Kinds of activity come from
categories.py (rules, no AI). Projects are not used: the app no longer asks the user to classify.
"""

import sqlite3
import time
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from . import categories, store, work
from .classify import OFFWORK_APP
from .apps import app_label, clean_title

STRIP_MERGE_GAP_S = 120  # same-kind activities this close together form one block of the day strip
STRIP_MIN_BLOCK_S = 180  # shorter blocks are folded into the one before (the strip only)
TOP_WORK = 8             # work folders with the most time shown under "한 일" (then 웹·소통·기타)


def _empty(day: date) -> dict:
    return {"day": day, "by_kind": {}, "apps": {}, "work": {}, "blocks": [], "folders": []}


def _day(conn: sqlite3.Connection, day: date, now: Optional[float]) -> dict:
    """Time by kind, by app and by work folder, the day strip and the file changes, for one day."""
    if day > store.local_day(time.time() if now is None else now):
        return _empty(day)  # not yet
    activities, _ = store.day_activities(conn, day, now)
    by_kind: dict[str, float] = defaultdict(float)
    apps: dict[str, float] = defaultdict(float)
    blocks: list[list] = []
    for a in sorted(activities, key=lambda a: a.start):
        kind = categories.category(a.app, a.title)
        by_kind[kind] += a.duration
        if a.app != OFFWORK_APP:
            apps[a.app] += a.duration
        if blocks and blocks[-1][2] == kind and a.start - blocks[-1][1] <= STRIP_MERGE_GAP_S:
            blocks[-1][1] = max(blocks[-1][1], a.end)
        else:
            blocks.append([a.start, a.end, kind])
    records, _ = work.day(conn, day, now, activities)
    work_s = {(r.name, r.folder_id is None): r.seconds for r in records}  # (name, outside) -> seconds
    start, end = store.day_bounds(day)
    folders = conn.execute(
        "SELECT c.folder_id, f.label, COUNT(*) AS n, "
        "SUM(c.files_added + c.files_modified + c.files_deleted) AS files, "
        "SUM(c.lines_added) AS added, SUM(c.lines_removed) AS removed "
        "FROM changes c JOIN watched_folders f ON f.id = c.folder_id "
        "WHERE c.baseline = 0 AND c.ts >= ? AND c.ts < ? GROUP BY c.folder_id", (start, end)).fetchall()
    return {"day": day, "by_kind": by_kind, "apps": apps, "work": work_s, "blocks": _smooth(blocks),
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
    """Time per work folder, most first, then 웹·소통·기타 (`outside`); `day` is the day with the most
    of it (to open)."""
    total: dict[tuple[str, bool], float] = defaultdict(float)
    best: dict[tuple[str, bool], tuple[float, date]] = {}
    for d in days:
        for key, sec in d["work"].items():
            total[key] += sec
            if key not in best or sec > best[key][0]:
                best[key] = (sec, d["day"])
    folders = sorted((k for k in total if not k[1]), key=lambda k: -total[k])[:TOP_WORK]
    outside = [(n, True) for n in work.OUTSIDE if (n, True) in total]
    return [{"title": k[0], "outside": k[1], "seconds": round(total[k]), "day": best[k][1].isoformat(),
             "days": sum(1 for d in days if k in d["work"])} for k in folders + outside]


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


# ---------------------------------------------------------------- details (a click on the dashboard)

DETAIL_WINDOWS = 40  # window titles listed per app or per piece of work


def _days(day: date, span: str, now: Optional[float]) -> list[date]:
    """The days a dashboard view covers, up to today."""
    if span not in ("day", "week"):
        raise ValueError(span)
    if span == "day":
        return [day]
    today = store.local_day(time.time() if now is None else now)
    monday = day - timedelta(days=day.weekday())
    return [d for d in (monday + timedelta(days=i) for i in range(7)) if d <= today] or [monday]


def _top(windows: dict, n: int = DETAIL_WINDOWS) -> list[dict]:
    return [{"app": app, "title": title, "seconds": round(sec)}
            for (app, title), sec in sorted(windows.items(), key=lambda kv: -kv[1])[:n] if sec >= 30]


def detail_files(conn: sqlite3.Connection, day: date, span: str = "day", folder_id: Optional[int] = None,
                 now: Optional[float] = None) -> dict:
    """The files changed in the view, per work folder: each path once (its latest change opens the diff),
    how often it changed and the lines added and removed."""
    days = _days(day, span, now)
    lo, hi = store.day_bounds(days[0])[0], store.day_bounds(days[-1])[1]
    sql = ("SELECT cf.id, cf.path, cf.kind, cf.lines_added, cf.lines_removed, c.ts, c.folder_id, f.label "
           "FROM change_files cf JOIN changes c ON c.id = cf.change_id JOIN watched_folders f ON f.id = c.folder_id "
           "WHERE c.baseline = 0 AND c.ts >= ? AND c.ts < ?")
    args: list = [lo, hi]
    if folder_id is not None:
        sql += " AND c.folder_id = ?"
        args.append(folder_id)
    folders: dict[int, dict] = {}
    changes = 0
    for r in conn.execute(sql + " ORDER BY c.ts DESC, cf.id DESC", args):
        changes += 1
        f = folders.setdefault(r["folder_id"], {"folder_id": r["folder_id"], "label": r["label"], "files": {}})
        e = f["files"].get(r["path"])
        if e is None:  # the latest change of this path
            e = f["files"][r["path"]] = {"id": r["id"], "path": r["path"], "kind": r["kind"], "last": r["ts"],
                                         "times": 0, "added": 0, "removed": 0}
        e["times"] += 1
        e["added"] += r["lines_added"] or 0
        e["removed"] += r["lines_removed"] or 0
    out = [{**f, "files": sorted(f["files"].values(), key=lambda e: -e["last"])} for f in folders.values()]
    out.sort(key=lambda f: -len(f["files"]))
    return {"start": days[0].isoformat(), "end": days[-1].isoformat(), "changes": changes,
            "files": sum(len(f["files"]) for f in out), "folders": out}


def detail_kind(conn: sqlite3.Connection, day: date, span: str, kind: str, now: Optional[float] = None) -> dict:
    """The activity of one kind (개발, 문서, ...) in the view: per app, its window titles by time, and per day."""
    if kind not in categories.KEYS:
        raise store.Invalid("분류가 올바르지 않습니다")
    apps: dict[str, dict] = {}
    per_day = []
    for d in _days(day, span, now):
        activities, _ = store.day_activities(conn, d, now)
        sec_day = 0.0
        for a in activities:
            if categories.category(a.app, a.title) != kind:
                continue
            sec_day += a.duration
            label = "업무 외" if a.app == OFFWORK_APP else app_label(a.app)
            e = apps.setdefault(label, {"label": label, "seconds": 0.0, "windows": defaultdict(float)})
            e["seconds"] += a.duration
            if a.app != OFFWORK_APP:  # 업무 외: time only, no names
                e["windows"][(label, clean_title(a.title, a.app))] += a.duration
        per_day.append({"day": d.isoformat(), "seconds": round(sec_day)})
    out = [{"label": e["label"], "seconds": round(e["seconds"]), "windows": _top(e["windows"])}
           for e in sorted(apps.values(), key=lambda e: -e["seconds"])]
    labels = dict(categories.CATEGORIES_LABELS)
    return {"kind": kind, "label": labels[kind], "seconds": sum(d["seconds"] for d in per_day),
            "days": per_day, "apps": out}


def detail_work(conn: sqlite3.Connection, day: date, span: str, name: str, outside: bool,
                now: Optional[float] = None) -> dict:
    """One line of "한 일" (a work folder, or 웹 / 소통 / 기타) in the view: its time and spans per day,
    the windows it was, and (a work folder) the files changed in it."""
    days = _days(day, span, now)
    per_day, windows, folder_id = [], defaultdict(float), None
    for d, recs, _ in work.period(conn, days[0], days[-1], now):
        r = next((r for r in recs if r.name == name and (r.folder_id is None) == outside), None)
        if r is None:
            continue
        folder_id = folder_id or r.folder_id
        per_day.append({"day": d.isoformat(), "seconds": round(r.seconds), "spans": [[s, e] for s, e in r.spans]})
        for w, sec in r.windows.items():
            windows[w] += sec
    files = detail_files(conn, day, span, folder_id, now) if folder_id else None
    return {"name": name, "outside": outside, "seconds": sum(d["seconds"] for d in per_day),
            "days": per_day, "windows": _top(windows), "files": files}
