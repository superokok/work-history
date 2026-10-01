"""Keyword matching for project rules, tracking rules and exclusions (pure functions)."""

from dataclasses import dataclass
from typing import Iterable, Optional

FIELDS = ("app", "title")

# Window events outside the tracked work apps are stored with no app name and
# no title: only their time is kept, shown as "업무 외".
OFFWORK_APP = ""


@dataclass(frozen=True)
class Keyword:
    field: str  # 'app' | 'title'
    pattern: str


@dataclass(frozen=True)
class Rule:
    id: int
    project_id: int
    field: str
    pattern: str


@dataclass(frozen=True)
class TrackRule:
    """Record windows whose app contains `app` and title contains `title`.

    Either part may be empty (= any); not both.
    """
    app: str
    title: str = ""


def is_tracked(rules: Iterable[TrackRule], app: str, title: str) -> bool:
    for r in rules:
        a, t = r.app.strip().casefold(), r.title.strip().casefold()
        if not (a or t):
            continue
        if (not a or a in (app or "").casefold()) and (not t or t in (title or "").casefold()):
            return True
    return False


def keyword_matches(field: str, pattern: str, app: str, title: str) -> bool:
    """Case-insensitive substring match on the app name or window title."""
    needle = pattern.strip().casefold()
    if not needle:
        return False
    value = app if field == "app" else title
    return needle in (value or "").casefold()


def matches_any(keywords: Iterable[Keyword], app: str, title: str) -> bool:
    return any(keyword_matches(k.field, k.pattern, app, title) for k in keywords)


def match_rule(rules: Iterable[Rule], app: str, title: str) -> Optional[Rule]:
    """The most specific (longest pattern) matching rule; ties go to the oldest."""
    best = None
    for rule in rules:
        if not keyword_matches(rule.field, rule.pattern, app, title):
            continue
        key = (len(rule.pattern.strip()), -rule.id)
        if best is None or key > (len(best.pattern.strip()), -best.id):
            best = rule
    return best


def resolve_project(
    manual_project_id: Optional[int], rules: Iterable[Rule], app: str, title: str
) -> tuple[Optional[int], Optional[str]]:
    """Return (project_id, source) where source is 'manual', 'rule' or None."""
    if app == OFFWORK_APP:
        return None, None
    if manual_project_id is not None:
        return manual_project_id, "manual"
    rule = match_rule(rules, app, title)
    if rule is not None:
        return rule.project_id, "rule"
    return None, None
