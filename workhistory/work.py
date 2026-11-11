"""Work records: where the time of a day went, per work folder (pure rules, no AI).

Each window of the day is put with a work folder only when its path is under that
folder (see `FolderMatcher`); every other window goes by the kind of app: 웹 (browsers),
소통 (messengers, mail, meetings) or 기타. Time is not cut into sessions: a folder's
windows are added up however often the user switched back and forth, and the
times of day it was worked on are kept as spans. "업무 외" time (apps that are not
recorded) is a total only.

A record is one (day, work folder) or (day, 웹/소통/기타): the unit the dashboard,
the AI report and "AI에게 묻기" use.
"""

import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Optional

from . import categories, locate, store
from .classify import OFFWORK_APP
from .config import WORK_SPAN_GAP_S
from .normalize import Activity
from .apps import app_label, clean_title

WEB, COMM, OTHER = "웹", "소통", "기타"
OUTSIDE = (WEB, COMM, OTHER)  # in this order after the work folders
_KIND_NAMES = {"web": WEB, "comm": COMM}


def _norm_path(path: str) -> str:
    return path.replace("/", "\\").rstrip("\\").casefold()


class FolderMatcher:
    """Which work folder a window is about: only where the window has a path under a work folder.

    The path is the one the timeline shows for the window: the work-folder file its title
    names (`locate.file_paths`, the way IDE and editor titles name the open file), or a
    work folder's path written in the title itself (terminals: "PS C:/app/project-a>").
    Names alone (a folder called "api" in some title) never count. With nested work
    folders the deepest one wins. Browsers and messengers are never matched: they are
    웹 and 소통.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.folders = [(r["id"], r["label"], r["path"]) for r in conn.execute(
            "SELECT id, label, path FROM watched_folders WHERE enabled = 1 ORDER BY id")]
        # Deepest first, so that a path inside a nested work folder goes to that folder.
        self._roots = sorted(((fid, _norm_path(path)) for fid, _, path in self.folders if path),
                             key=lambda r: -len(r[1]))

    def match(self, windows: set[tuple[str, str]]) -> dict[tuple[str, str], int]:
        """(app, title) -> folder id, for the windows whose path is under a work folder."""
        if not self._roots:
            return {}
        candidates = {w for w in windows if categories.category(w[0]) not in ("web", "comm", "offwork")}
        by_file = locate.file_paths(self.conn, list({title for _, title in candidates}))
        out = {}
        for app, title in candidates:
            fid = self._under(by_file.get(title, "")) or self._in_title(title)
            if fid:
                out[(app, title)] = fid
        return out

    def _under(self, path: str) -> Optional[int]:
        p = _norm_path(path) if path else ""
        return next((fid for fid, root in self._roots if p and (p == root or p.startswith(root + "\\"))), None)

    def _in_title(self, title: str) -> Optional[int]:
        t = title.replace("/", "\\").casefold()
        return next((fid for fid, root in self._roots
                     if re.search(re.escape(root) + r"(?![\w.\-])", t)), None)


@dataclass
class Record:
    """One work folder's (or 웹/소통/기타's) time on one day."""
    day: date
    name: str                     # the folder's label, or 웹 / 소통 / 기타
    folder_id: Optional[int] = None
    seconds: float = 0.0
    spans: list[list[float]] = field(default_factory=list)  # [start, end] times of day it was worked on
    windows: dict[tuple[str, str], float] = field(default_factory=lambda: defaultdict(float))  # (app, title)
    per_day: dict[date, float] = field(default_factory=dict)  # a record over several days: seconds per day

    @property
    def key(self) -> str:
        return f"folder:{self.folder_id}" if self.folder_id else self.name

    def top_windows(self, n: int) -> list[tuple[tuple[str, str], float]]:
        return sorted(self.windows.items(), key=lambda kv: -kv[1])[:n]


def over_period(days: list[list[Record]]) -> list[Record]:
    """The records of several days as one per work folder (and 웹 / 소통 / 기타): time added up,
    seconds per day kept, window titles added up over the days. `day` is the day with the most of it."""
    by_key: dict[str, Record] = {}
    for recs in days:
        for r in recs:
            c = by_key.get(r.key)
            if c is None:
                c = by_key[r.key] = Record(r.day, r.name, r.folder_id)
            c.seconds += r.seconds
            c.spans += r.spans
            c.per_day[r.day] = c.per_day.get(r.day, 0.0) + r.seconds
            for w, sec in r.windows.items():
                c.windows[w] += sec
    for c in by_key.values():
        c.day = max(c.per_day, key=c.per_day.get)
    folders = sorted((c for c in by_key.values() if c.folder_id), key=lambda c: -c.seconds)
    return folders + [by_key[n] for n in OUTSIDE if n in by_key]


def outside_name(app: str) -> str:
    return _KIND_NAMES.get(categories.category(app), OTHER)


def records(day: date, activities: list[Activity], folder_of: dict[tuple[str, str], int],
            labels: dict[int, str]) -> tuple[list[Record], float]:
    """The day's records (work folders by time, then 웹, 소통, 기타) and its 업무 외 seconds."""
    by_key: dict[object, Record] = {}
    offwork = 0.0
    for a in sorted(activities, key=lambda a: a.start):
        if a.app == OFFWORK_APP:
            offwork += a.duration
            continue
        fid = folder_of.get((a.app, a.title))
        key = fid if fid else outside_name(a.app)
        r = by_key.get(key)
        if r is None:
            r = by_key[key] = Record(day, labels[fid] if fid else key, fid)
        r.seconds += a.duration
        if r.spans and a.start - r.spans[-1][1] <= WORK_SPAN_GAP_S:
            r.spans[-1][1] = max(r.spans[-1][1], a.end)
        else:
            r.spans.append([a.start, a.end])
        r.windows[(app_label(a.app), clean_title(a.title, a.app))] += a.duration
    folders = sorted((r for r in by_key.values() if r.folder_id), key=lambda r: -r.seconds)
    return folders + [by_key[n] for n in OUTSIDE if n in by_key], offwork


def period(conn: sqlite3.Connection, start: date, end: date, now: Optional[float] = None,
           activities_of: Optional[Callable[[date], list[Activity]]] = None
           ) -> list[tuple[date, list[Record], float]]:
    """(day, records, 업무 외 seconds) for each day from `start` to `end`."""
    days, acts = [], {}
    d = start
    while d <= end:
        days.append(d)
        acts[d] = activities_of(d) if activities_of else store.day_activities(conn, d, now)[0]
        d += timedelta(days=1)
    matcher = FolderMatcher(conn)
    folder_of = matcher.match({(a.app, a.title) for d in days for a in acts[d] if a.app != OFFWORK_APP})
    labels = {fid: label for fid, label, _ in matcher.folders}
    return [(d, *records(d, acts[d], folder_of, labels)) for d in days]


def day(conn: sqlite3.Connection, d: date, now: Optional[float] = None,
        activities: Optional[list[Activity]] = None) -> tuple[list[Record], float]:
    [(_, recs, offwork)] = period(conn, d, d, now, (lambda _d: activities) if activities is not None else None)
    return recs, offwork
