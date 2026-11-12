"""지난 계획 이어 보기: the plans of the previous saved report, and this period's records about them.

The previous report is the latest saved report of the same kind that ends before this
period starts (usually last week's 주간보고, or the last 업무일지 before this day). Its
plan lines (차주 계획 / 다음 할 일) are read from the saved text, and each is looked up in
this period's records by the clues it names: issue and MR numbers, file and class names,
other specific words, and work folders. No AI is involved: a match only says that a
record names the same thing, never that the plan was done.
"""

import re
import sqlite3
from datetime import date, datetime
from typing import Optional

from . import store

PLAN_LABELS = {"차주 계획", "다음 할 일", "다음 주 계획", "향후 계획"}
SHOWN = 3  # records listed per plan
_STATUS = re.compile(r"\s*\((진행 중|완료|제안)\)\s*$")
_DURATION = re.compile(r"\s*\((\d+시간)?\s*(\d+분)?\)\s*$")
# ASCII word edges: Korean particles follow names directly ("poc-dev에").
_A, _Z = r"(?<![A-Za-z0-9_])", r"(?![A-Za-z0-9_])"
_CLUES = [re.compile(_A + p + _Z) for p in (
    r"[A-Z][A-Z0-9]+-\d+",                       # issue keys: GOV-1418
    r"(?<![!#])[!#]\d+",                         # MR / PR: !312, #42
    r"[A-Za-z0-9_\-]+\.[A-Za-z][A-Za-z0-9]{0,5}",  # file names: V42__index.sql, values.yaml
    r"[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+",        # class names: JwtTokenProvider
    r"[A-Z][A-Z0-9]*_[A-Z0-9_]+",                # constants, tables: MINWON_REQ
    r"[a-z][a-z0-9]*(?:-[a-z0-9]+)+",            # names with hyphens: poc-dev
    r"[A-Za-z]{5,}",                             # other specific words: Ingress, searchAfter
)]
# Words too common in window titles to point at one piece of work.
_COMMON = {"chrome", "edge", "visual", "studio", "google", "microsoft", "excel", "word", "outlook",
           "teams", "slack", "notepad", "explorer", "files", "window", "untitled", "document",
           "review", "update", "check", "fix", "test", "tests", "error", "issue", "issues", "merge",
           "request", "branch", "commit", "build", "deploy", "server", "client", "local", "index",
           "query", "page", "pages", "table", "values", "config", "setting", "settings"}


def _clues(text: str) -> list[str]:
    found: list[str] = []
    for pattern in _CLUES:
        for m in pattern.findall(text):
            word = m if isinstance(m, str) else m[0]
            if word.lower() in _COMMON or any(word.lower() in f.lower() for f in found):
                continue
            found.append(word)
    return found


def parse_plans(body: str) -> list[dict]:
    """The plan lines of a saved report: {group, text, suggested}."""
    plans, group, in_plan = [], "", False
    for line in body.splitlines():
        t = line.strip()
        if t.startswith("## "):
            group, in_plan = _DURATION.sub("", t[3:]).strip(), False
        elif t.startswith("# "):
            group, in_plan = "", False
        elif re.fullmatch(r"\[[^\]]+\]", t):
            in_plan = t[1:-1].strip() in PLAN_LABELS
        elif in_plan and re.match(r"[-*•]\s", t):
            m = _STATUS.search(t)
            text = (t[:m.start()] if m else t)[1:].strip()
            if text:
                plans.append({"group": group, "text": text, "suggested": bool(m and m.group(1) == "제안")})
    return plans


def _previous(conn: sqlite3.Connection, kind: str, start: date) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM reports WHERE kind = ? AND end_day < ? ORDER BY end_day DESC LIMIT 1",
                        (kind, start.isoformat())).fetchone()


def _windows(conn, lo: float, hi: float, clue: str) -> list[dict]:
    rows = conn.execute(
        "SELECT title, MIN(start_ts) AS ts, SUM(end_ts - start_ts) AS sec FROM window_events "
        "WHERE start_ts < ? AND end_ts > ? AND title LIKE ? ESCAPE '\\' GROUP BY title ORDER BY ts",
        (hi, lo, "%" + clue.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")).fetchall()
    return [{"type": "window", "day": datetime.fromtimestamp(r["ts"]).date().isoformat(), "start": r["ts"],
             "label": r["title"], "seconds": r["sec"]} for r in rows]


def _changes(conn, lo: float, hi: float, clue: str) -> list[dict]:
    rows = conn.execute(
        "SELECT c.id, c.ts, f.label AS folder, cf.path FROM change_files cf JOIN changes c ON c.id = cf.change_id "
        "JOIN watched_folders f ON f.id = c.folder_id WHERE c.baseline = 0 AND c.ts >= ? AND c.ts < ? "
        "AND cf.path LIKE ? ESCAPE '\\' ORDER BY c.ts",
        (lo, hi, "%" + clue.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")).fetchall()
    return [{"type": "change", "id": r["id"], "day": datetime.fromtimestamp(r["ts"]).date().isoformat(),
             "start": r["ts"], "label": f"{r['folder']}: {r['path']}"} for r in rows]


def _folder_changes(conn, lo: float, hi: float, folder_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM changes WHERE folder_id = ? AND baseline = 0 AND ts >= ? AND ts < ?",
                        (folder_id, lo, hi)).fetchone()[0]


def previous_plans(conn: sqlite3.Connection, kind: str, start: date, end: date,
                   now: Optional[float] = None) -> dict:
    """{"from": the previous report's period or None, "plans": [...]}. Each plan: its group and text,
    whether it was still an unconfirmed AI suggestion, the clues looked for, and `status`:
    'related' (a record of this period names a clue), 'folder' (only its work folder changed),
    'none' (nothing found) or 'no_clue' (nothing specific to look for)."""
    prev = _previous(conn, kind, start)
    if prev is None:
        return {"from": None, "plans": []}
    lo, hi = store.day_bounds(start)[0], store.day_bounds(end)[1]
    if now is not None:
        hi = min(hi, now)
    folders = [(r["id"], r["label"]) for r in conn.execute("SELECT id, label FROM watched_folders WHERE enabled = 1")]
    out = []
    for plan in parse_plans(prev["body"]):
        clues = _clues(plan["text"]) or _clues(plan["group"])
        evidence: list[dict] = []
        for clue in clues:
            evidence += _windows(conn, lo, hi, clue) + _changes(conn, lo, hi, clue)
        seen, unique = set(), []
        for e in sorted(evidence, key=lambda e: e["start"]):
            key = (e["type"], e.get("id"), e["label"])
            if key not in seen:
                seen.add(key)
                unique.append(e)
        named = [(fid, label) for fid, label in folders
                 if label.lower() in f"{plan['group']} {plan['text']}".lower()]
        folder_changes = sum(_folder_changes(conn, lo, hi, fid) for fid, _ in named)
        status = ("related" if unique else "folder" if folder_changes
                  else "none" if clues or named else "no_clue")
        out.append({**plan, "clues": clues, "folders": [label for _, label in named], "status": status,
                    "count": len(unique), "evidence": unique[:SHOWN], "folder_changes": folder_changes})
    return {"from": {"start": prev["start_day"], "end": prev["end_day"]}, "plans": out}
