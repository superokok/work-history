"""Turn raw window/AFK events into active activity segments (pure functions).

All times are UTC epoch seconds. Intervals are half-open (start, end).
"""

from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from .classify import Keyword, Rule, matches_any, resolve_project
from .config import IDLE_APPS, ROW_MERGE_GAP_S

Interval = tuple[float, float]


@dataclass(frozen=True)
class WindowEvent:
    id: int
    start: float
    end: float
    app: str
    title: str
    project_id: Optional[int] = None  # manual override


@dataclass(frozen=True)
class AfkEvent:
    start: float
    end: float
    status: str  # 'afk' | 'not-afk'


@dataclass
class Activity:
    event_id: int
    start: float
    end: float
    app: str
    title: str
    project_id: Optional[int] = None
    project_source: Optional[str] = None

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class Row:
    """One timeline row: consecutive activities with the same app/title/project."""

    start: float
    end: float
    app: str
    title: str
    project_id: Optional[int]
    project_source: Optional[str]
    active_s: float
    event_ids: list[int] = field(default_factory=list)


def merge_intervals(intervals: Iterable[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for start, end in sorted(i for i in intervals if i[1] > i[0]):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def subtract(base: Sequence[Interval], remove: Sequence[Interval]) -> list[Interval]:
    """base − remove; both must be merged and sorted."""
    out: list[Interval] = []
    for start, end in base:
        cur = start
        for r_start, r_end in remove:
            if r_end <= cur:
                continue
            if r_start >= end:
                break
            if r_start > cur:
                out.append((cur, r_start))
            cur = max(cur, r_end)
            if cur >= end:
                break
        if cur < end:
            out.append((cur, end))
    return out


def clip(start: float, end: float, allowed: Sequence[Interval]) -> list[Interval]:
    """Parts of [start, end) inside `allowed` (merged, sorted)."""
    out: list[Interval] = []
    i = max(bisect_right(allowed, (start, float("inf"))) - 1, 0)
    while i < len(allowed) and allowed[i][0] < end:
        s, e = max(start, allowed[i][0]), min(end, allowed[i][1])
        if s < e:
            out.append((s, e))
        i += 1
    return out


def idle_periods(afk_events: Iterable[AfkEvent]) -> list[Interval]:
    """AFK periods; 'not-afk' wins where the watcher reported both."""
    afk_events = list(afk_events)
    afk = merge_intervals((e.start, e.end) for e in afk_events if e.status == "afk")
    active = merge_intervals((e.start, e.end) for e in afk_events if e.status == "not-afk")
    return subtract(afk, active)


def build_activities(
    window_events: Iterable[WindowEvent],
    afk_events: Iterable[AfkEvent],
    blocked: Iterable[Interval],
    range_start: float,
    range_end: float,
    exclusions: Iterable[Keyword] = (),
    rules: Iterable[Rule] = (),
) -> tuple[list[Activity], list[str]]:
    """Active, non-excluded, classified activity inside [range_start, range_end).

    Time with no AFK data counts as active; if the range has window events but
    no AFK data at all, the 'afk_missing' warning is returned.
    """
    window_events = sorted(window_events, key=lambda e: (e.start, e.end, e.id))
    afk_events = list(afk_events)
    exclusions = list(exclusions)
    rules = list(rules)
    warnings: list[str] = []

    remove = merge_intervals(list(blocked) + idle_periods(afk_events))
    allowed = subtract([(range_start, range_end)], remove)
    if window_events and not afk_events:
        warnings.append("afk_missing")

    activities: list[Activity] = []
    covered_until = range_start
    for ev in window_events:
        if ev.app.casefold() in IDLE_APPS or matches_any(exclusions, ev.app, ev.title):
            continue
        # Never count the same moment twice if the watcher reported overlaps.
        start = max(ev.start, covered_until)
        if ev.end <= start:
            continue
        project_id, source = resolve_project(ev.project_id, rules, ev.app, ev.title)
        for s, e in clip(start, ev.end, allowed):
            activities.append(Activity(ev.id, s, e, ev.app, ev.title, project_id, source))
        covered_until = max(covered_until, ev.end)
    return activities, warnings


def merge_rows(activities: Iterable[Activity], max_gap: float = ROW_MERGE_GAP_S) -> list[Row]:
    rows: list[Row] = []
    for a in activities:
        last = rows[-1] if rows else None
        if (
            last is not None
            and last.app == a.app
            and last.title == a.title
            and last.project_id == a.project_id
            and a.start - last.end <= max_gap
        ):
            last.end = a.end
            last.active_s += a.duration
            if a.event_id not in last.event_ids:
                last.event_ids.append(a.event_id)
            if a.project_source == "manual":
                last.project_source = "manual"
        else:
            rows.append(
                Row(a.start, a.end, a.app, a.title, a.project_id, a.project_source,
                    a.duration, [a.event_id])
            )
    return rows
