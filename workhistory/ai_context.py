"""The exact work data the AI may see for a period, with citable references.

References: S<id> = session (inferred from activity), C<id> = a recorded
file change (results), N<id> = the user's note, P<id> = a project with the
user's own next action. Nothing outside this payload is sent to the model,
every free-text field passes the secret redaction, and every reference the
model returns is checked against `refs`.
"""

import difflib
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from . import sessionstore, store
from .classify import OFFWORK_APP
from .redact import redact
from .sessions import app_label, clean_title

MAX_DAYS = 31
MIN_SESSION_S = 60          # shorter sessions are noise for a report
WINDOWS_PER_SESSION = 5
MIN_WINDOW_S = 60
# Diff excerpts: enough to see what changed, bounded so a big refactor cannot flood the prompt.
DIFF_LINES_PER_FILE = 25
DIFF_LINES_PER_CHANGE = 50
DIFF_LINES_TOTAL = 600
DIFF_LINE_CHARS = 200
FILES_PER_CHANGE = 20


@dataclass
class Context:
    start: date
    end: date
    payload: dict
    refs: set[str] = field(default_factory=set)
    # ref -> where it points, for links in the UI: {type, day, label, start?, end?}
    sources: dict[str, dict] = field(default_factory=dict)
    project_seconds: dict[str, float] = field(default_factory=dict)  # name -> work seconds
    project_changes: dict[str, int] = field(default_factory=dict)  # name -> recorded file changes
    # ref -> what the user wrote themselves (a note; a session's note and a title they edited).
    # Only these may back a claim that something is finished.
    user_words: dict[str, str] = field(default_factory=dict)
    offwork_s: float = 0.0

    @property
    def empty(self) -> bool:
        p = self.payload
        return not p["sessions"] and not p["notes"] and not p["changes"]


def _hm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _excerpt(repo, path: str, old_blob: Optional[str], new_blob: Optional[str], budget: int) -> list[str]:
    """Changed lines only (+/-), redacted and truncated."""
    from .extract import extract_text  # local import keeps this module light for callers without files
    old = extract_text(path, repo[old_blob.encode()].data) if old_blob else ""
    new = extract_text(path, repo[new_blob.encode()].data) if new_blob else ""
    if old is None or new is None:
        return []
    # Whole texts first, so that multi-line secrets (PEM blocks) are masked before the diff splits them.
    old, new = redact(old), redact(new)
    lines = []
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(), n=0, lineterm=""):
        if line.startswith(("+++", "---", "@@")):
            continue
        lines.append(redact(line)[:DIFF_LINE_CHARS])
        if len(lines) >= min(DIFF_LINES_PER_FILE, budget):
            break
    return lines


def _changes(conn: sqlite3.Connection, ctx: Context, start: date, end: date, repos_dir: Optional[Path],
             name_of, projects: dict, used_projects: set) -> list[dict]:
    lo, hi = store.day_bounds(start)[0], store.day_bounds(end)[1]
    rows = conn.execute(
        "SELECT c.*, f.label AS folder, f.project_id FROM changes c JOIN watched_folders f ON f.id = c.folder_id "
        "WHERE c.baseline = 0 AND c.ts >= ? AND c.ts < ? ORDER BY c.ts", (lo, hi)).fetchall()
    repos: dict[int, object] = {}
    out, budget = [], DIFF_LINES_TOTAL
    try:
        for c in rows:
            files = conn.execute("SELECT * FROM change_files WHERE change_id = ? ORDER BY path LIMIT ?",
                                 (c["id"], FILES_PER_CHANGE)).fetchall()
            project = name_of(c["project_id"])
            if c["project_id"] in projects:
                used_projects.add(c["project_id"])
            ctx.project_changes[project] = ctx.project_changes.get(project, 0) + 1
            item = {
                "ref": f"C{c['id']}", "date": datetime.fromtimestamp(c["ts"]).date().isoformat(),
                "time": _hm(c["ts"]), "folder": c["folder"], "project": project,
                "files": [{"path": f["path"], "change": {"A": "추가", "M": "수정", "D": "삭제"}[f["kind"]],
                           **({"lines": f"+{f['lines_added']} -{f['lines_removed']}"}
                              if f["lines_added"] is not None else {})} for f in files],
            }
            if repos_dir is not None and budget > 0:
                diff: list[str] = []
                repo = repos.get(c["folder_id"])
                if repo is None:
                    from dulwich.repo import Repo
                    from .snapshots import repo_path
                    path = repo_path(repos_dir, c["folder_id"])
                    repo = repos[c["folder_id"]] = Repo(str(path)) if path.exists() else False
                for f in files if repo else []:
                    if not f["is_text"] or len(diff) >= DIFF_LINES_PER_CHANGE:
                        continue
                    lines = _excerpt(repo, f["path"], f["old_blob"], f["new_blob"],
                                     min(DIFF_LINES_PER_CHANGE - len(diff), budget))
                    if lines:
                        diff += [f"# {f['path']}"] + lines
                        budget -= len(lines)
                if diff:
                    item["diff_excerpt"] = diff
            out.append(item)
            ctx.refs.add(item["ref"])
            ctx.sources[item["ref"]] = {"type": "change", "day": item["date"], "start": c["ts"],
                                        "label": f"{c['folder']}: " + ", ".join(
                                            f["path"].rsplit("/", 1)[-1] for f in files[:3])}
    finally:
        for repo in repos.values():
            if repo:
                repo.close()
    return out


def build(conn: sqlite3.Connection, start: date, end: date, repos_dir: Optional[Path] = None) -> Context:
    if end < start:
        raise store.Invalid("종료일이 시작일보다 빠릅니다")
    if (end - start).days + 1 > MAX_DAYS:
        raise store.Invalid(f"한 번에 {MAX_DAYS}일까지만 보낼 수 있습니다")

    projects = {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, name, next_action FROM projects WHERE archived = 0")}
    name_of = lambda pid: projects[pid]["name"] if pid in projects else "미분류"  # noqa: E731
    ctx = Context(start, end, {})
    sessions_out, used_projects = [], set()
    project_seconds: dict[str, float] = defaultdict(float)

    day = start
    while day <= end:
        activities, _ = store.day_activities(conn, day)
        sessionstore.refresh_day(conn, day, activities=activities)
        for s in conn.execute(
                "SELECT * FROM sessions WHERE day = ? AND excluded = 0 ORDER BY start_ts", (day.isoformat(),)):
            inside = sessionstore.activities_within(activities, s["start_ts"], s["end_ts"])
            work = [a for a in inside if a.app != OFFWORK_APP]
            work_s = sum(a.duration for a in work)
            ctx.offwork_s += sum(a.duration for a in inside) - work_s
            if work_s < MIN_SESSION_S:
                continue
            windows: dict[tuple[str, str], float] = defaultdict(float)
            for a in work:
                windows[(app_label(a.app), clean_title(a.title, a.app))] += a.duration
            top = sorted(windows.items(), key=lambda kv: -kv[1])[:WINDOWS_PER_SESSION]
            ref = f"S{s['id']}"
            project = name_of(s["project_id"])
            project_seconds[project] += work_s
            if s["project_id"] in projects:
                used_projects.add(s["project_id"])
            item = {
                "ref": ref, "date": day.isoformat(),
                "time": f"{_hm(s['start_ts'])}-{_hm(s['end_ts'])}",
                "minutes": round(work_s / 60),
                "project": project,
                "title": redact(s["title"]),
                "title_by": "user" if s["title_edited"] else "auto",
                "windows": [{"app": app, "title": redact(title), "minutes": round(sec / 60)}
                            for (app, title), sec in top if sec >= MIN_WINDOW_S],
            }
            if s["note"]:
                item["user_note"] = redact(s["note"])
            words = " ".join(filter(None, [s["note"], s["title"] if s["title_edited"] else ""]))
            if words:
                ctx.user_words[ref] = words
            sessions_out.append(item)
            ctx.refs.add(ref)
            ctx.sources[ref] = {"type": "session", "day": day.isoformat(), "label": s["title"],
                                "start": s["start_ts"], "end": s["end_ts"]}
        day += timedelta(days=1)
    conn.commit()  # regrouped sessions are derived data; keep them

    notes_out = []
    for n in conn.execute("SELECT * FROM notes WHERE day >= ? AND day <= ? ORDER BY day, created_at",
                          (start.isoformat(), end.isoformat())):
        notes_out.append({"ref": f"N{n['id']}", "date": n["day"], "project": name_of(n["project_id"]),
                          "text": redact(n["body"])})
        ctx.refs.add(f"N{n['id']}")
        ctx.sources[f"N{n['id']}"] = {"type": "note", "day": n["day"], "label": n["body"][:60]}
        ctx.user_words[f"N{n['id']}"] = n["body"]
        if n["project_id"] in projects:
            used_projects.add(n["project_id"])

    changes_out = _changes(conn, ctx, start, end, repos_dir, name_of, projects, used_projects)

    projects_out = []
    for pid in sorted(used_projects, key=lambda p: projects[p]["name"]):
        p = projects[pid]
        item = {"ref": f"P{pid}", "name": p["name"]}
        if p["next_action"].strip():
            item["user_next_action"] = redact(p["next_action"].strip())
        projects_out.append(item)
        ctx.refs.add(f"P{pid}")
        ctx.sources[f"P{pid}"] = {"type": "project", "project_id": pid, "label": p["name"]}

    ctx.project_seconds = dict(project_seconds)
    ctx.payload = {
        "period": {"from": start.isoformat(), "to": end.isoformat()},
        "projects": projects_out,
        "project_minutes": {k: round(v / 60) for k, v in sorted(project_seconds.items(), key=lambda kv: -kv[1])},
        "offwork_minutes": round(ctx.offwork_s / 60),
        "sessions": sessions_out,
        "changes": changes_out,
        "notes": notes_out,
    }
    return ctx
