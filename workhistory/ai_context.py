"""The exact work data the AI may see for a period, with citable references.

References: W<n> = a work record (one work folder's time on one day, or the
day's 웹 / 소통 / 기타 time; work.py), C<id> = recorded file changes of one work
folder (results). There are no projects and no notes: the model groups the work
itself (issue keys, work folders, topics). Nothing outside this payload is sent
to the model, every free-text field passes the secret redaction, and every
reference the model returns is checked against `refs`.

The payload is kept within a size budget so a busy week fits the model's
context: snapshots of one folder taken close together go as one piece of work,
and when that is not enough, fewer windows per record and the shortest records
are left out (`FIT_LEVELS`). Diff excerpts share what room is left.
"""

import difflib
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from . import store, work
from .redact import redact

MAX_DAYS = 31
MIN_WINDOW_S = 60
# The prompt must leave room for the system rules and the answer in the model's context
# (32k tokens, 8k of them for the answer): about 18k tokens of data, counted in characters of `dumps`.
PAYLOAD_CHARS = 40_000
# Ways to make a period fit, tried in order until the payload without diffs takes at most
# FIT_SHARE of the budget: (windows per record, shortest record sent in seconds,
# snapshots of one folder closer than this many seconds go as one change, files listed per change).
FIT_LEVELS = [
    (10, 60, 30 * 60, 20),
    (6, 60, 30 * 60, 20),
    (6, 60, 3 * 3600, 10),
    (4, 60, 24 * 3600, 10),
    (3, 60, 24 * 3600, 5),
    (2, 60, 24 * 3600, 5),
    (1, 300, 24 * 3600, 5),
    (1, 900, 24 * 3600, 5),
]
SPANS_SHOWN = 8  # times of day listed per record
FIT_SHARE = 0.7
# Diff excerpts: enough to see what changed, shared fairly between the changes of the period.
DIFF_LINES_PER_FILE = 25
DIFF_LINES_PER_CHANGE = 50
DIFF_LINES_TOTAL = 600
DIFF_LINE_CHARS = 200
DIFF_LINE_COST = 70  # average characters a diff line takes in the payload
KIND_WORDS = {"A": "추가", "M": "수정", "D": "삭제", "AD": "추가 후 삭제"}


@dataclass
class Context:
    start: date
    end: date
    payload: dict
    refs: set[str] = field(default_factory=set)
    # ref -> where it points, for links in the UI: {type, day, label, start?, end?}
    sources: dict[str, dict] = field(default_factory=dict)
    # W ref of a work folder -> its seconds (records of 웹/소통/기타 add no time to a group of work)
    work_seconds: dict[str, float] = field(default_factory=dict)
    # W ref of a work folder -> {iso day: seconds}; ref (W or C) -> its work folder. They let a week's
    # folder time be shared between two pieces of work by the days each one changed files.
    work_days: dict[str, dict[str, float]] = field(default_factory=dict)
    ref_folder: dict[str, str] = field(default_factory=dict)
    # work folder -> its W and C refs, to notice a folder the draft leaves out
    folder_refs: dict[str, list[str]] = field(default_factory=dict)
    folder_seconds: dict[str, float] = field(default_factory=dict)
    outside_s: dict[str, float] = field(default_factory=dict)  # 웹 / 소통 / 기타 -> seconds
    offwork_s: float = 0.0
    # What was cut to fit the budget, in words for the user ([] = everything was sent as is).
    trimmed: list[str] = field(default_factory=list)
    next_ref: int = 1  # the W number after this context's last record (to number the next day on)

    @property
    def empty(self) -> bool:
        p = self.payload
        return not p["work"] and not p["changes"]


def _hm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def dumps(payload: dict) -> str:
    """The payload as sent: valid JSON with one record or change per line (indenting every key
    would spend a large part of the budget on whitespace)."""
    def value(v) -> str:
        if isinstance(v, list) and v:
            return "[\n" + ",\n".join(json.dumps(x, ensure_ascii=False) for x in v) + "\n]"
        return json.dumps(v, ensure_ascii=False)
    return "{\n" + ",\n".join(f"{json.dumps(k)}: {value(v)}" for k, v in payload.items()) + "\n}"


def _size(payload: dict) -> int:
    return len(dumps(payload))


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
        if len(lines) >= min(DIFF_LINES_PER_FILE, budget):
            break
        lines.append(redact(line)[:DIFF_LINE_CHARS])
    return lines


# ---------------------------------------------------------------- work records

def _records(conn: sqlite3.Connection, ctx: Context, start: date, end: date,
             first_ref: int = 1) -> list[tuple[str, work.Record]]:
    """(W ref, record), numbered from `first_ref`; trimmed later. One day: a record per work
    folder (and 웹/소통/기타). Several days (a week): one per work folder over the whole period,
    with its minutes per day, so a folder worked on every day is sent once, not five times."""
    days = []
    for _day, recs, offwork in work.period(conn, start, end):
        ctx.offwork_s += offwork
        days.append(recs)
    records = days[0] if len(days) == 1 else work.over_period(days)
    out = [(f"W{first_ref + i}", r) for i, r in enumerate(records)]
    ctx.next_ref = first_ref + len(out)
    return out


def _spans(r: work.Record) -> str:
    shown = [f"{_hm(a)}-{_hm(b)}" for a, b in r.spans[:SPANS_SHOWN]]
    more = len(r.spans) - len(shown)
    return ", ".join(shown) + (f" 외 {more}번" if more > 0 else "")


_WEEKDAYS = "월화수목금토일"


def _record_item(ref: str, r: work.Record, windows: int) -> dict:
    when = ({"days": ", ".join(f"{_WEEKDAYS[d.weekday()]} {d.month}/{d.day} {round(sec / 60)}분"
                               for d, sec in sorted(r.per_day.items()) if sec >= 60)}
            if r.per_day else {"date": r.day.isoformat(), "times": _spans(r)})
    return {
        "ref": ref, **({"folder": r.name} if r.folder_id else {"kind": r.name}),
        "minutes": round(r.seconds / 60), **when,
        "windows": [{"app": app, "title": redact(title), "minutes": round(sec / 60)}
                    for (app, title), sec in r.top_windows(windows) if sec >= MIN_WINDOW_S],
    }


# ---------------------------------------------------------------- file changes

def _change_rows(conn: sqlite3.Connection, start: date, end: date) -> tuple[list, dict[int, list]]:
    lo, hi = store.day_bounds(start)[0], store.day_bounds(end)[1]
    rows = conn.execute(
        "SELECT c.*, f.label AS folder FROM changes c JOIN watched_folders f ON f.id = c.folder_id "
        "WHERE c.baseline = 0 AND c.ts >= ? AND c.ts < ? ORDER BY c.ts, c.id", (lo, hi)).fetchall()
    files: dict[int, list] = defaultdict(list)
    for f in conn.execute(
            "SELECT cf.* FROM change_files cf JOIN changes c ON c.id = cf.change_id "
            "WHERE c.baseline = 0 AND c.ts >= ? AND c.ts < ? ORDER BY cf.change_id, cf.path", (lo, hi)):
        files[f["change_id"]].append(f)
    return rows, files


def _bursts(rows: list, gap: float) -> list[list]:
    """Snapshots of one folder on one day less than `gap` seconds apart, as one piece of work."""
    out: list[list] = []
    last: dict[tuple, list] = {}
    for c in rows:
        key = (c["folder_id"], datetime.fromtimestamp(c["ts"]).date())
        burst = last.get(key)
        if burst and c["ts"] - burst[-1]["ts"] < gap:
            burst.append(c)
        else:
            last[key] = burst = [c]
            out.append(burst)
    return out


def _burst_files(burst: list, files: dict[int, list]) -> list[dict]:
    """Each path once, from its first old version to its last new version; most changed first."""
    merged: dict[str, dict] = {}
    for c in burst:
        for f in files[c["id"]]:
            m = merged.get(f["path"])
            if m is None:
                merged[f["path"]] = {"path": f["path"], "first": f["kind"], "last": f["kind"],
                                     "old_blob": f["old_blob"], "new_blob": f["new_blob"], "is_text": f["is_text"],
                                     "added": f["lines_added"], "removed": f["lines_removed"]}
                continue
            m.update(last=f["kind"], new_blob=f["new_blob"], is_text=f["is_text"])
            if m["added"] is None or f["lines_added"] is None:
                m["added"] = m["removed"] = None
            else:
                m["added"] += f["lines_added"]
                m["removed"] += f["lines_removed"]
    for m in merged.values():
        first, last = m["first"], m["last"]
        m["kind"] = ("AD" if first == "A" and last == "D" else "A" if first == "A"
                     else "D" if last == "D" else "M")
    return sorted(merged.values(), key=lambda m: (-((m["added"] or 0) + (m["removed"] or 0)), m["path"]))


def _file_line(f: dict) -> str:
    """'src/Auth.java (수정 +12 -3)': one short string per file."""
    lines = f" +{f['added']} -{f['removed']}" if f["added"] is not None else ""
    return f"{f['path']} ({KIND_WORDS[f['kind']]}{lines})"


def _change_item(burst: list, files: list[dict], limit: int) -> dict:
    first, last = burst[0], burst[-1]
    item = {
        "ref": f"C{first['id']}", "date": datetime.fromtimestamp(first["ts"]).date().isoformat(),
        "time": _hm(first["ts"]) if first is last else f"{_hm(first['ts'])}-{_hm(last['ts'])}",
        "folder": first["folder"],
        "files": [_file_line(f) for f in files[:limit]],
    }
    if len(files) > limit:
        item["more_files"] = len(files) - limit
    return item


def _add_diffs(changes: list[tuple[dict, list, list[dict]]], total: int, limit: int, repos_dir: Path) -> None:
    """Diff excerpts within `total` lines, shared fairly: each change gets an equal share of what is
    left, so a big early change cannot take the room of the later ones."""
    from dulwich.repo import Repo
    from .snapshots import repo_path
    repos: dict[int, object] = {}
    try:
        for i, (item, burst, files) in enumerate(changes):
            share = min(DIFF_LINES_PER_CHANGE, total // (len(changes) - i))
            if share <= 0:
                continue
            folder_id = burst[0]["folder_id"]
            repo = repos.get(folder_id)
            if repo is None:
                path = repo_path(repos_dir, folder_id)
                repo = repos[folder_id] = Repo(str(path)) if path.exists() else False
            if not repo:
                continue
            diff: list[str] = []
            used = 0
            for f in files[:limit]:
                if not f["is_text"] or used >= share:
                    continue
                lines = _excerpt(repo, f["path"], f["old_blob"], f["new_blob"], share - used)
                if lines:
                    diff += [f"# {f['path']}"] + lines
                    used += len(lines)
            if diff:
                item["diff_excerpt"] = diff
                total -= used
    finally:
        for repo in repos.values():
            if repo:
                repo.close()


# ---------------------------------------------------------------- build

def build(conn: sqlite3.Connection, start: date, end: date, repos_dir: Optional[Path] = None,
          first_ref: int = 1) -> Context:
    """The payload for one request. `first_ref`: the first W number, so that the days of a week
    split into several requests (ai_service) never share a number."""
    if end < start:
        raise store.Invalid("종료일이 시작일보다 빠릅니다")
    if (end - start).days + 1 > MAX_DAYS:
        raise store.Invalid(f"한 번에 {MAX_DAYS}일까지만 보낼 수 있습니다")

    ctx = Context(start, end, {})
    records = _records(conn, ctx, start, end, first_ref)
    rows, files = _change_rows(conn, start, end)

    def payload(level) -> tuple[dict, list]:
        windows, min_s, gap, limit = level
        chosen = [(ref, r) for ref, r in records if r.seconds >= max(min_s, 60)]
        changes = []
        for burst in _bursts(rows, gap):
            merged = _burst_files(burst, files)
            changes.append((_change_item(burst, merged, limit), burst, merged))
        return {
            "period": {"from": start.isoformat(), "to": end.isoformat()},
            "work_minutes": round(sum(r.seconds for _, r in records) / 60),
            "offwork_minutes": round(ctx.offwork_s / 60),
            "work": [_record_item(ref, r, windows) for ref, r in chosen],
            "changes": [c[0] for c in changes],
        }, changes

    fits = lambda d: _size(d) <= PAYLOAD_CHARS * FIT_SHARE  # noqa: E731
    for level in FIT_LEVELS:
        data, changes = payload(level)
        if fits(data):
            break
    else:
        # Still too big (a long period): keep the longest records that fit.
        windows, _, gap, limit = FIT_LEVELS[-1]
        lengths = sorted({r.seconds for _, r in records})
        lo, hi = 0, len(lengths)  # threshold index; hi = no records at all
        while lo < hi:
            mid = (lo + hi) // 2
            if fits(payload((windows, lengths[mid], gap, limit))[0]):
                hi = mid
            else:
                lo = mid + 1
        level = (windows, lengths[lo] if lo < len(lengths) else float("inf"), gap, limit)
        data, changes = payload(level)
    windows, min_s, gap, limit = level
    if level != FIT_LEVELS[0]:
        ctx.trimmed.append(f"창 제목은 기록마다 {windows}개까지")
        if gap > FIT_LEVELS[0][2]:
            ctx.trimmed.append("같은 폴더의 파일 변경은 " + ("하루" if gap >= 24 * 3600 else f"{gap // 3600}시간")
                               + " 단위로 묶음")
        if limit < FIT_LEVELS[0][3]:
            ctx.trimmed.append(f"파일 이름은 변경마다 {limit}개까지")
        left_out = sum(1 for _, r in records if 60 <= r.seconds < min_s)
        if left_out:
            ctx.trimmed.append(f"작업 기록 {left_out}건 모두 제외" if min_s == float("inf")
                               else f"{round(min_s / 60)}분 미만 기록 {left_out}건 제외")
    for ref, r in records:
        if r.folder_id:
            ctx.folder_seconds[r.name] = ctx.folder_seconds.get(r.name, 0.0) + r.seconds
        else:
            ctx.outside_s[r.name] = ctx.outside_s.get(r.name, 0.0) + r.seconds
        if r.seconds < max(min_s, 60):
            continue
        ctx.refs.add(ref)
        ctx.sources[ref] = {"type": "work", "day": r.day.isoformat(), "label": r.name,
                            "start": r.spans[0][0], "end": r.spans[-1][1]}
        if r.folder_id:
            ctx.work_seconds[ref] = r.seconds
            ctx.work_days[ref] = {d.isoformat(): sec for d, sec in (r.per_day or {r.day: r.seconds}).items()}
            ctx.ref_folder[ref] = r.name
            ctx.folder_refs.setdefault(r.name, []).append(ref)

    if repos_dir is not None and changes:
        room = (PAYLOAD_CHARS - _size(data)) // DIFF_LINE_COST
        _add_diffs(changes, max(0, min(DIFF_LINES_TOTAL, room)), limit, repos_dir)
        # Long lines can take more room than estimated: drop the biggest excerpts until it fits.
        for item, _, _ in sorted(changes, key=lambda c: -len(json.dumps(c[0].get("diff_excerpt", [])))):
            if _size(data) <= PAYLOAD_CHARS or "diff_excerpt" not in item:
                break
            del item["diff_excerpt"]
            if "바뀐 줄 일부 생략" not in ctx.trimmed:
                ctx.trimmed.append("바뀐 줄 일부 생략")
    for item, burst, merged in changes:
        ref, first = item["ref"], burst[0]
        ctx.refs.add(ref)
        ctx.folder_refs.setdefault(item["folder"], []).append(ref)
        ctx.ref_folder[ref] = item["folder"]
        ctx.sources[ref] = {"type": "change", "day": item["date"], "start": first["ts"],
                            "label": f"{item['folder']}: " + ", ".join(
                                f["path"].rsplit("/", 1)[-1] for f in merged[:3])}
    ctx.payload = data
    return ctx


def merge(parts: list[Context], start: date, end: date) -> Context:
    """One context over several (the days of a week sent one by one): every reference any of
    them sent stays valid, with its time and its place."""
    out = Context(start, end, {"work": [], "changes": []})
    for c in parts:
        out.payload["work"] += c.payload["work"]
        out.payload["changes"] += c.payload["changes"]
        out.refs |= c.refs
        out.sources.update(c.sources)
        out.work_seconds.update(c.work_seconds)
        out.work_days.update(c.work_days)
        out.ref_folder.update(c.ref_folder)
        for folder, refs in c.folder_refs.items():
            out.folder_refs.setdefault(folder, []).extend(refs)
        for folder, sec in c.folder_seconds.items():
            out.folder_seconds[folder] = out.folder_seconds.get(folder, 0.0) + sec
        for name, sec in c.outside_s.items():
            out.outside_s[name] = out.outside_s.get(name, 0.0) + sec
        out.offwork_s += c.offwork_s
        out.trimmed += [f"{c.start.month}/{c.start.day}: {t}" for t in c.trimmed]
        out.next_ref = max(out.next_ref, c.next_ref)
    return out
