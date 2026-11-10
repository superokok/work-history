"""Read/write operations used by the HTTP API. Every function takes an open connection."""

import re
import shutil
import sqlite3
import time
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Optional

from . import locate, snapshots, store
from .classify import FIELDS, OFFWORK_APP, Keyword, matches_any, resolve_project
from .apps import app_label
from .normalize import merge_rows
from .store import Invalid, NotFound, day_bounds

__all__ = ["Invalid", "NotFound"]  # re-exported for the HTTP layer


# ---------------------------------------------------------------- helpers

def parse_day(value: Any) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise Invalid(f"날짜 형식이 올바르지 않습니다: {value!r}") from None


def _text(value: Any, name: str, max_len: int = 10000, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise Invalid(f"{name}: 문자열이 필요합니다")
    value = value.strip()
    if not value and not allow_empty:
        raise Invalid(f"{name}: 비어 있을 수 없습니다")
    if len(value) > max_len:
        raise Invalid(f"{name}: 너무 깁니다")
    return value


def _field(value: Any) -> str:
    if value not in FIELDS:
        raise Invalid("field는 'app' 또는 'title'이어야 합니다")
    return value


def _optional_project(conn: sqlite3.Connection, value: Any) -> Optional[int]:
    if value in (None, "", "none"):
        return None
    try:
        project_id = int(value)
    except (TypeError, ValueError):
        raise Invalid("project_id가 올바르지 않습니다") from None
    if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
        raise NotFound("프로젝트를 찾을 수 없습니다")
    return project_id


def _project_filter(project: Any) -> tuple[bool, Optional[int]]:
    """(filter active?, wanted project id or None for 'unassigned')."""
    if project in (None, ""):
        return False, None
    return True, None if project == "none" else int(project)


def _like(q: str) -> str:
    return "%" + re.sub(r"([\\%_])", r"\\\1", q) + "%"


# ---------------------------------------------------------------- timeline

def timeline(conn: sqlite3.Connection, day: date, project: Any = None, app: Optional[str] = None,
             now: Optional[float] = None) -> dict:
    activities, warnings = store.day_activities(conn, day, now)
    filtering, wanted = _project_filter(project)
    by_project: dict[Optional[int], float] = defaultdict(float)
    by_app: dict[str, float] = defaultdict(float)
    offwork_total = 0.0
    shown = []  # the per-app view
    for a in activities:
        if a.app == OFFWORK_APP:
            offwork_total += a.duration
        else:
            by_app[a.app] += a.duration
            by_project[a.project_id] += a.duration
        if (app and a.app != app) or (filtering and a.project_id != wanted):
            continue
        shown.append(a)

    return {
        "day": day.isoformat(),
        "total_active_s": sum(by_project.values()) + offwork_total,
        "offwork_s": offwork_total,
        "by_project": [{"project_id": k, "seconds": v}
                       for k, v in sorted(by_project.items(), key=lambda kv: -kv[1])],
        "by_app": [{"app": k, "seconds": v}
                   for k, v in sorted(by_app.items(), key=lambda kv: -kv[1])],
        "rows": [r.__dict__ for r in merge_rows(shown)],
        "apps": app_summary(shown, locate.file_paths(conn, list({a.title for a in shown}))),
        "warnings": warnings,
        "notes": list_notes(conn, day),
        "changes": list_changes(conn, day),
    }


def app_summary(activities: list, file_paths: Optional[dict[str, str]] = None) -> list[dict]:
    """One entry per app with its time added up over the whole day, however often it came back.

    Most recently used app first; inside an app, window titles by total time. Each window
    has a `path`: the work-folder file its title names (`file_paths`, title → path), else "".
    """
    file_paths = file_paths or {}
    apps: dict[str, dict] = {}
    for a in activities:
        entry = apps.setdefault(a.app, {"app": a.app, "label": app_label(a.app), "seconds": 0.0,
                                        "first": a.start, "last": a.end,
                                        "windows": {}})
        entry["seconds"] += a.duration
        entry["first"], entry["last"] = min(entry["first"], a.start), max(entry["last"], a.end)
        w = entry["windows"].setdefault(a.title, {"title": a.title, "seconds": 0.0, "last": a.end,
                                                  "projects": defaultdict(float)})
        w["seconds"] += a.duration
        w["last"] = max(w["last"], a.end)
        w["projects"][a.project_id] += a.duration
    out = []
    for entry in sorted(apps.values(), key=lambda e: -e["last"]):
        windows = []
        for w in sorted(entry["windows"].values(), key=lambda w: -w["seconds"]):
            projects = w.pop("projects")
            path = file_paths.get(w["title"], "")
            windows.append({**w, "path": path, "path_kind": "file" if path else None,
                            "project_id": max(projects, key=projects.get)})
        out.append({**entry, "windows": windows})
    return out


_CHANGE_SQL = ("SELECT c.*, f.label AS folder FROM changes c "
               "JOIN watched_folders f ON f.id = c.folder_id ")


def _change_out(conn: sqlite3.Connection, c: sqlite3.Row, limit_files: int) -> dict:
    files = [dict(r) for r in conn.execute(
        "SELECT id, path, kind, is_text, lines_added, lines_removed, project_id FROM change_files "
        "WHERE change_id = ? ORDER BY path LIMIT ?", (c["id"], limit_files))]
    return {"id": c["id"], "ts": c["ts"], "folder": c["folder"], "project_id": c["project_id"],
            "files_added": c["files_added"], "files_modified": c["files_modified"],
            "files_deleted": c["files_deleted"], "lines_added": c["lines_added"],
            "lines_removed": c["lines_removed"], "files": files}


def list_changes(conn: sqlite3.Connection, day: date, limit_files: int = 50) -> list[dict]:
    """File changes (results) recorded on a day, newest last. Baseline snapshots are not work."""
    start, end = day_bounds(day)
    return [_change_out(conn, c, limit_files) for c in conn.execute(
        _CHANGE_SQL + "WHERE c.baseline = 0 AND c.ts >= ? AND c.ts < ? ORDER BY c.ts", (start, end))]


def get_change(conn: sqlite3.Connection, change_id: int, limit_files: int = 50) -> dict:
    """One recorded file change (a C reference), for opening its diffs straight from a citation."""
    row = conn.execute(_CHANGE_SQL + "WHERE c.id = ? AND c.baseline = 0", (change_id,)).fetchone()
    if row is None:
        raise NotFound("파일 변경 기록을 찾을 수 없습니다")
    return _change_out(conn, row, limit_files)


def assign_project(conn: sqlite3.Connection, event_ids: Any, project_id: Any) -> int:
    """Manually classify window events (overrides keyword rules)."""
    if not isinstance(event_ids, list) or not event_ids or len(event_ids) > 10000:
        raise Invalid("event_ids 목록이 필요합니다")
    try:
        ids = [int(i) for i in event_ids]
    except (TypeError, ValueError):
        raise Invalid("event_ids가 올바르지 않습니다") from None
    pid = _optional_project(conn, project_id)
    changed = 0
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        changed += conn.execute(f"UPDATE window_events SET project_id = ? WHERE id IN ({marks})",
                                [pid, *chunk]).rowcount
    return changed


# ---------------------------------------------------------------- projects

def list_projects(conn: sqlite3.Connection) -> list[dict]:
    rules = defaultdict(list)
    for r in conn.execute("SELECT id, project_id, field, pattern FROM project_rules ORDER BY id"):
        rules[r["project_id"]].append(dict(r))
    return [{**dict(p), "rules": rules[p["id"]]} for p in conn.execute(
        "SELECT id, name, color, next_action, archived, created_at FROM projects "
        "ORDER BY archived, name COLLATE NOCASE")]


def create_project(conn: sqlite3.Connection, data: dict) -> dict:
    name = _text(data.get("name"), "이름", 100)
    color = data.get("color") or "#4f7cff"
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise Invalid("색상은 #RRGGBB 형식이어야 합니다")
    try:
        cur = conn.execute("INSERT INTO projects (name, color, created_at) VALUES (?, ?, ?)",
                           (name, color, time.time()))
    except sqlite3.IntegrityError:
        raise Invalid("같은 이름의 프로젝트가 이미 있습니다") from None
    return get_project(conn, cur.lastrowid)


def get_project(conn: sqlite3.Connection, project_id: int) -> dict:
    for p in list_projects(conn):
        if p["id"] == project_id:
            return p
    raise NotFound("프로젝트를 찾을 수 없습니다")


def update_project(conn: sqlite3.Connection, project_id: int, data: dict) -> dict:
    get_project(conn, project_id)
    updates: dict[str, Any] = {}
    if "name" in data:
        updates["name"] = _text(data["name"], "이름", 100)
    if "color" in data:
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(data["color"])):
            raise Invalid("색상은 #RRGGBB 형식이어야 합니다")
        updates["color"] = data["color"]
    if "next_action" in data:
        updates["next_action"] = _text(data["next_action"], "다음 할 일", 2000, allow_empty=True)
    if "archived" in data:
        updates["archived"] = 1 if data["archived"] else 0
    if updates:
        try:
            conn.execute(f"UPDATE projects SET {', '.join(f'{k} = ?' for k in updates)} WHERE id = ?",
                         [*updates.values(), project_id])
        except sqlite3.IntegrityError:
            raise Invalid("같은 이름의 프로젝트가 이미 있습니다") from None
    return get_project(conn, project_id)


def delete_project(conn: sqlite3.Connection, project_id: int) -> None:
    if conn.execute("DELETE FROM projects WHERE id = ?", (project_id,)).rowcount == 0:
        raise NotFound("프로젝트를 찾을 수 없습니다")
    snapshots.attribute_all(conn)  # its sub-folders are asked about again; changes may now lean elsewhere


def add_rule(conn: sqlite3.Connection, project_id: int, data: dict) -> dict:
    get_project(conn, project_id)
    field = _field(data.get("field"))
    pattern = _text(data.get("pattern"), "키워드", 200)
    cur = conn.execute(
        "INSERT INTO project_rules (project_id, field, pattern, created_at) VALUES (?, ?, ?, ?)",
        (project_id, field, pattern, time.time()))
    return {"id": cur.lastrowid, "project_id": project_id, "field": field, "pattern": pattern}


def delete_rule(conn: sqlite3.Connection, rule_id: int) -> None:
    if conn.execute("DELETE FROM project_rules WHERE id = ?", (rule_id,)).rowcount == 0:
        raise NotFound("규칙을 찾을 수 없습니다")


# ---------------------------------------------------------------- notes

def list_notes(conn: sqlite3.Connection, day: date) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, day, project_id, body, created_at, updated_at FROM notes "
        "WHERE day = ? ORDER BY created_at", (day.isoformat(),))]


def create_note(conn: sqlite3.Connection, data: dict) -> dict:
    day = parse_day(data.get("day"))
    body = _text(data.get("body"), "메모")
    pid = _optional_project(conn, data.get("project_id"))
    now = time.time()
    cur = conn.execute(
        "INSERT INTO notes (day, project_id, body, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (day.isoformat(), pid, body, now, now))
    return dict(conn.execute("SELECT * FROM notes WHERE id = ?", (cur.lastrowid,)).fetchone())


def update_note(conn: sqlite3.Connection, note_id: int, data: dict) -> dict:
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    if row is None:
        raise NotFound("메모를 찾을 수 없습니다")
    body = _text(data["body"], "메모") if "body" in data else row["body"]
    pid = _optional_project(conn, data["project_id"]) if "project_id" in data else row["project_id"]
    conn.execute("UPDATE notes SET body = ?, project_id = ?, updated_at = ? WHERE id = ?",
                 (body, pid, time.time(), note_id))
    return dict(conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone())


def delete_note(conn: sqlite3.Connection, note_id: int) -> None:
    if conn.execute("DELETE FROM notes WHERE id = ?", (note_id,)).rowcount == 0:
        raise NotFound("메모를 찾을 수 없습니다")


# ---------------------------------------------------------------- search

def search(conn: sqlite3.Connection, q: str, date_from: Optional[date] = None,
           date_to: Optional[date] = None, project: Any = None, limit: int = 200) -> dict:
    q = (q or "").strip()
    if not q:
        raise Invalid("검색어를 입력하세요")
    like = _like(q)
    filtering, wanted = _project_filter(project)

    def day_filter(column: str) -> tuple[str, list]:
        sql, args = "", []
        if date_from:
            sql += f" AND {column} >= ?"
            args.append(date_from.isoformat())
        if date_to:
            sql += f" AND {column} <= ?"
            args.append(date_to.isoformat())
        return sql, args

    time_sql, time_args = "", []
    if date_from:
        time_sql += " AND end_ts > ?"
        time_args.append(day_bounds(date_from)[0])
    if date_to:
        time_sql += " AND start_ts < ?"
        time_args.append(day_bounds(date_to)[1])

    rules = store.load_rules(conn)
    exclusions = store.load_exclusions(conn)
    events = []
    for r in conn.execute(
        "SELECT date(start_ts, 'unixepoch', 'localtime') AS day, app, title, project_id, "
        "MIN(start_ts) AS first_ts, MAX(end_ts) AS last_ts "
        "FROM window_events WHERE (app LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\')"
        + time_sql + " GROUP BY day, app, title, project_id ORDER BY last_ts DESC LIMIT ?",
        [like, like, *time_args, limit * 5],
    ):
        if matches_any(exclusions, r["app"], r["title"]):
            continue
        pid, source = resolve_project(r["project_id"], rules, r["app"], r["title"])
        if filtering and pid != wanted:
            continue
        events.append({**dict(r), "project_id": pid, "project_source": source})
        if len(events) >= limit:
            break

    sql, args = day_filter("day")
    if filtering:
        sql += " AND project_id IS ?"
        args.append(wanted)
    notes = [dict(r) for r in conn.execute(
        "SELECT * FROM notes WHERE body LIKE ? ESCAPE '\\'" + sql
        + " ORDER BY day DESC, created_at DESC LIMIT ?", [like, *args, limit])]

    projects = [dict(r) for r in conn.execute(
        "SELECT id, name, color, next_action FROM projects WHERE name LIKE ? ESCAPE '\\' "
        "OR next_action LIKE ? ESCAPE '\\' ORDER BY name", (like, like))]

    files_sql = ("SELECT cf.id, cf.change_id, cf.path, cf.kind, cf.lines_added, cf.lines_removed, c.ts, "
                 "f.label AS folder, "
                 "cf.project_id, date(c.ts, 'unixepoch', 'localtime') AS day FROM change_files cf "
                 "JOIN changes c ON c.id = cf.change_id JOIN watched_folders f ON f.id = c.folder_id "
                 "WHERE c.baseline = 0 AND cf.path LIKE ? ESCAPE '\\'")
    files_args: list[Any] = [like]
    sql, args = day_filter("date(c.ts, 'unixepoch', 'localtime')")
    if filtering:
        sql += " AND cf.project_id IS ?"
        args.append(wanted)
    changed_files = [dict(r) for r in conn.execute(files_sql + sql + " ORDER BY c.ts DESC LIMIT ?",
                                                   [*files_args, *args, limit])]

    return {"query": q, "events": events, "notes": notes,
            "projects": projects, "files": changed_files}


# ---------------------------------------------------------------- what to record

def get_tracking(conn: sqlite3.Connection) -> dict:
    return {"mode": store.track_mode(conn),
            "rules": [dict(r) for r in conn.execute(
                "SELECT id, app_pattern, title_pattern FROM track_rules "
                "ORDER BY app_pattern COLLATE NOCASE, title_pattern COLLATE NOCASE")]}


def set_track_mode(conn: sqlite3.Connection, data: dict) -> dict:
    mode = data.get("mode")
    if mode not in ("all", "selected"):
        raise Invalid("mode는 'all' 또는 'selected'여야 합니다")
    if mode == "selected" and not conn.execute("SELECT 1 FROM track_rules").fetchone():
        raise Invalid("기록할 앱을 먼저 하나 이상 추가하세요")
    conn.execute("INSERT INTO meta (key, value) VALUES ('track_mode', ?) "
                 "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (mode,))
    return get_tracking(conn)


def add_track_rule(conn: sqlite3.Connection, data: dict) -> dict:
    app = _text(data.get("app", ""), "앱 이름", 200, allow_empty=True)
    title = _text(data.get("title", ""), "창 제목 키워드", 200, allow_empty=True)
    if not (app or title):
        raise Invalid("앱 이름이나 창 제목 키워드 중 하나는 입력하세요")
    exists = conn.execute(
        "SELECT 1 FROM track_rules WHERE app_pattern = ? COLLATE NOCASE AND title_pattern = ? COLLATE NOCASE",
        (app, title)).fetchone()
    if not exists:
        conn.execute("INSERT INTO track_rules (app_pattern, title_pattern, created_at) VALUES (?, ?, ?)",
                     (app, title, time.time()))
    return get_tracking(conn)


def delete_track_rule(conn: sqlite3.Connection, rule_id: int) -> dict:
    if conn.execute("SELECT 1 FROM track_rules WHERE id = ?", (rule_id,)).fetchone() is None:
        raise NotFound("규칙을 찾을 수 없습니다")
    if store.track_mode(conn) == "selected" and conn.execute("SELECT COUNT(*) FROM track_rules").fetchone()[0] == 1:
        raise Invalid("마지막 규칙은 지울 수 없습니다. 먼저 '전부 기록'으로 바꾸세요")
    conn.execute("DELETE FROM track_rules WHERE id = ?", (rule_id,))
    return get_tracking(conn)


# ---------------------------------------------------------------- privacy

def list_exclusions(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM exclusions ORDER BY field, pattern")]


def add_exclusion(conn: sqlite3.Connection, data: dict) -> dict:
    field = _field(data.get("field"))
    pattern = _text(data.get("pattern"), "키워드", 200)
    conn.execute("INSERT OR IGNORE INTO exclusions (field, pattern, created_at) VALUES (?, ?, ?)",
                 (field, pattern, time.time()))
    # Purge already-imported matches so excluded titles do not linger locally.
    keyword = [Keyword(field, pattern)]
    doomed = [r["id"] for r in conn.execute(
        f"SELECT id, app, title FROM window_events WHERE {field} LIKE ? ESCAPE '\\'",
        (_like(pattern),)) if matches_any(keyword, r["app"], r["title"])]
    for i in range(0, len(doomed), 500):
        chunk = doomed[i:i + 500]
        conn.execute(f"DELETE FROM window_events WHERE id IN ({','.join('?' * len(chunk))})", chunk)
    # And from the collector's raw events.
    raw = [r["id"] for r in conn.execute(
        f"SELECT id, app, title FROM collector_events WHERE kind = 'window' AND {field} LIKE ? ESCAPE '\\'",
        (_like(pattern),)) if matches_any(keyword, r["app"], r["title"])]
    for i in range(0, len(raw), 500):
        chunk = raw[i:i + 500]
        conn.execute(f"DELETE FROM collector_events WHERE id IN ({','.join('?' * len(chunk))})", chunk)
    return {"field": field, "pattern": pattern, "purged": len(doomed)}


def delete_exclusion(conn: sqlite3.Connection, exclusion_id: int) -> None:
    if conn.execute("DELETE FROM exclusions WHERE id = ?", (exclusion_id,)).rowcount == 0:
        raise NotFound("제외 항목을 찾을 수 없습니다")


def is_paused(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM blocked_ranges WHERE kind = 'pause' AND end_ts IS NULL").fetchone() is not None


def pause(conn: sqlite3.Connection, now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    if not is_paused(conn):
        conn.execute("INSERT INTO blocked_ranges (kind, start_ts) VALUES ('pause', ?)", (now,))


def resume(conn: sqlite3.Connection, now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    conn.execute("UPDATE blocked_ranges SET end_ts = ? WHERE kind = 'pause' AND end_ts IS NULL", (now,))


def delete_data(conn: sqlite3.Connection, data: dict, now: Optional[float] = None,
                repos_dir: Optional[Path] = None) -> dict:
    """Delete imported activity and notes for a day range, or everything.

    A 'deleted' blocked range is recorded so later syncs do not re-import it.
    Projects, rules and exclusions are kept.
    """
    now = time.time() if now is None else now
    if data.get("all") is True:
        lo, hi, day_lo, day_hi = 0.0, now, "0000-00-00", "9999-99-99"
    else:
        d_from, d_to = parse_day(data.get("from")), parse_day(data.get("to"))
        if d_to < d_from:
            raise Invalid("종료일이 시작일보다 빠릅니다")
        lo, hi = day_bounds(d_from)[0], min(day_bounds(d_to)[1], now)
        day_lo, day_hi = d_from.isoformat(), d_to.isoformat()
    counts = {
        "window_events": conn.execute(
            "DELETE FROM window_events WHERE start_ts >= ? AND end_ts <= ?", (lo, hi)).rowcount,
        "afk_events": conn.execute(
            "DELETE FROM afk_events WHERE start_ts >= ? AND end_ts <= ?", (lo, hi)).rowcount,
        "notes": conn.execute(
            "DELETE FROM notes WHERE day >= ? AND day <= ?", (day_lo, day_hi)).rowcount,
    }
    # Rows of the session tables earlier versions kept (titles, notes): gone with the rest.
    conn.execute("DELETE FROM sessions WHERE day >= ? AND day <= ?", (day_lo, day_hi))
    # The collector's raw events too, so they are really gone.
    conn.execute("DELETE FROM collector_events WHERE start_ts >= ? AND end_ts <= ?", (lo, hi))
    if data.get("all") is True:
        conn.execute("DELETE FROM session_days")
        # Results history goes too: change records and snapshot repositories.
        counts["changes"] = conn.execute("DELETE FROM changes").rowcount
        conn.execute("DELETE FROM file_state")  # the next scan starts a fresh baseline
        if repos_dir is not None:
            shutil.rmtree(repos_dir, ignore_errors=True)  # the snapshot repositories themselves
    else:
        # Change records go; the snapshot repository itself keeps those commits (documented limitation).
        counts["changes"] = conn.execute("DELETE FROM changes WHERE ts >= ? AND ts < ?", (lo, hi)).rowcount
    if hi > lo:
        conn.execute("INSERT INTO blocked_ranges (kind, start_ts, end_ts) VALUES ('deleted', ?, ?)",
                     (lo, hi))
    return counts
