"""The user's names and kinds for apps (설정 → 앱 이름과 분류).

Windows names a window by its executable, and some executables are not the program the
user sees: Eclipse runs as javaw, many tools as python or node, store apps under
ApplicationFrameHost. The user gives an app a name and a kind (개발, 문서, ...), for every
window of it or only for windows whose title contains some text ("javaw" + "Eclipse IDE"
-> Eclipse IDE, 개발). Nothing is guessed: the settings list each app with a few of its
window titles, and the user decides.

The names apply when activity is read (`apply`, from store.day_activities), so the
dashboard, the timeline, search results built from activity and the AI's data all use
them, for past days too; the recorded window events keep the executable's name, and
removing a name brings the original back.
"""

import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Optional

from . import categories
from .apps import app_key, app_label
from .classify import OFFWORK_APP
from .store import Invalid, NotFound

# Executables that run other programs: their name alone does not say what the user used.
LAUNCHER_APPS = {"javaw", "java", "javaws", "python", "pythonw", "py", "node", "electron", "msedgewebview2",
                 "applicationframehost", "rundll32", "dllhost", "mshta", "wscript", "cscript", "wwahost"}
KINDS = [key for key in categories.KEYS if key != "offwork"]
TITLES_SHOWN = 5
MAX_LABEL = 60
MAX_CONTAINS = 100


@dataclass(frozen=True)
class NameRule:
    id: int
    app: str        # app_key of the executable ("javaw")
    contains: str   # only windows whose title contains this ('' = every window)
    label: str      # the name shown ('' = keep the usual name)
    category: Optional[str]  # a categories key, or None = keep the usual kind


class Naming:
    def __init__(self, rules: list[NameRule]):
        by_app: dict[str, list[NameRule]] = defaultdict(list)
        for r in rules:
            by_app[r.app].append(r)
        for lst in by_app.values():
            lst.sort(key=lambda r: (-len(r.contains), r.id))  # a title condition before "every window"
        self.by_app = dict(by_app)

    def match(self, app: str, title: str) -> Optional[NameRule]:
        lowered = title.casefold()
        return next((r for r in self.by_app.get(app_key(app), ())
                     if not r.contains or r.contains.casefold() in lowered), None)


def _rules(conn: sqlite3.Connection) -> list[NameRule]:
    return [NameRule(r["id"], r["app"], r["contains"], r["label"], r["category"])
            for r in conn.execute("SELECT * FROM app_names ORDER BY id")]


def load(conn: sqlite3.Connection) -> Naming:
    """The rules, and the kind of every renamed app for `categories.category`."""
    rules = _rules(conn)
    categories.set_renamed({app_key(r.label or app_label(r.app)): r.category or categories.default_category(r.app)
                            for r in rules})
    return Naming(rules)


def apply(conn: sqlite3.Connection, activities: list) -> list:
    """Activities with the user's app names (the kind follows from the name, see `load`)."""
    naming = load(conn)
    if not naming.by_app:
        return activities
    out = []
    for a in activities:
        rule = naming.match(a.app, a.title) if a.app != OFFWORK_APP else None
        out.append(replace(a, app=rule.label or app_label(a.app)) if rule else a)
    return out


# ---------------------------------------------------------------- settings

def _rule_dict(r: NameRule) -> dict:
    return {"id": r.id, "app": r.app, "contains": r.contains, "label": r.label, "category": r.category}


def list_names(conn: sqlite3.Connection, days: int = 7, now: Optional[float] = None) -> dict:
    """The rules, and the apps of the last `days` days (from the recorded windows) with their usual
    name and kind and a few window titles."""
    now = time.time() if now is None else now
    rules = _rules(conn)
    per_app: dict[str, dict] = {}
    for r in conn.execute(
            "SELECT app, title, SUM(MIN(end_ts, ?) - MAX(start_ts, ?)) AS sec FROM window_events "
            "WHERE end_ts > ? AND start_ts < ? AND app != ? GROUP BY app, title",
            (now, now - days * 86400, now - days * 86400, now, OFFWORK_APP)):
        key = app_key(r["app"])
        if not key:
            continue
        entry = per_app.setdefault(key, {"app": r["app"], "key": key, "seconds": 0.0, "titles": defaultdict(float)})
        entry["seconds"] += r["sec"] or 0.0
        entry["titles"][r["title"]] += r["sec"] or 0.0
    apps = []
    for e in sorted(per_app.values(), key=lambda e: -e["seconds"]):
        titles = e.pop("titles")
        own = [_rule_dict(r) for r in rules if r.app == e["key"]]
        launcher = e["key"] in LAUNCHER_APPS
        apps.append({**e, "seconds": round(e["seconds"]), "label": app_label(e["app"]),
                     "category": categories.default_category(e["app"]),
                     "launcher": launcher, "rules": own,
                     "titles": [{"title": t, "seconds": round(s)} for t, s in
                                sorted(titles.items(), key=lambda kv: -kv[1])[:TITLES_SHOWN]]})
    labels = dict(categories.CATEGORIES_LABELS)
    return {"rules": [_rule_dict(r) for r in rules], "apps": apps,
            "kinds": [{"key": k, "label": labels[k]} for k in KINDS]}


def save_name(conn: sqlite3.Connection, data: dict) -> dict:
    app = app_key(str(data.get("app") or ""))
    contains = str(data.get("contains") or "").strip()
    label = str(data.get("label") or "").strip()
    category = data.get("category") or None
    if not app:
        raise Invalid("앱(실행 파일 이름)을 입력하세요")
    if len(contains) > MAX_CONTAINS or len(label) > MAX_LABEL:
        raise Invalid("이름이 너무 깁니다")
    if category is not None and category not in KINDS:
        raise Invalid("분류가 올바르지 않습니다")
    if not label and category is None:
        raise Invalid("보일 이름이나 분류 중 하나는 정하세요")
    conn.execute(
        "INSERT INTO app_names (app, contains, label, category, created_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (app, contains) DO UPDATE SET label = excluded.label, category = excluded.category",
        (app, contains, label, category, time.time()))
    row = conn.execute("SELECT * FROM app_names WHERE app = ? AND contains = ?", (app, contains)).fetchone()
    return _rule_dict(NameRule(row["id"], row["app"], row["contains"], row["label"], row["category"]))


def delete_name(conn: sqlite3.Connection, rule_id: int) -> None:
    if conn.execute("DELETE FROM app_names WHERE id = ?", (rule_id,)).rowcount == 0:
        raise NotFound("규칙을 찾을 수 없습니다")
