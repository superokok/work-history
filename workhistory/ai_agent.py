"""'지난주 그거 어디까지 했지?' — an agent that answers questions about the user's own history.

The model gets read-only tools over the local database. Every result item
carries a reference; the final answer may only cite references that a tool
actually returned, and anything else is removed before the user sees it.
All text passes the secret redaction before it reaches the model.
"""

import json
import re
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from . import ai_service, service, sessionstore, store
from .ai_client import AIError, ToolSpec, ToolTrace, Usage
from .redact import redact
from .store import Invalid

MAX_QUESTION_CHARS = 500
MAX_RESULTS = 15
_REF = r"[SCNPDR][0-9A-Za-z-]+"
REF_IN_TEXT = re.compile(rf"\[({_REF}(?:\s*,\s*{_REF})*)\]")
WEEKDAYS = "월화수목금토일"

SYSTEM = """당신은 한 개발자의 업무 기록을 찾아보고 질문에 답하는 도우미입니다.
도구로 이 사람의 기록(작업 세션 S, 파일 변경 C, 메모 N, 프로젝트 P, 하루 D, 일일 리뷰 R)을 찾아보고, 확인한 사실만으로 답합니다.

반드시 지킬 것:
- 오늘은 {today}({weekday}요일)입니다. '어제', '지난주' 같은 말은 이 날짜를 기준으로 날짜 범위를 계산해 도구에 넘깁니다.
- 답을 쓰기 전에 필요한 만큼 도구로 확인합니다. 키워드 검색이 비면 다른 표현(영문 클래스 이름, 이슈 번호, 한국어 낱말)으로 다시 찾거나 해당 날짜를 직접 봅니다.
- 답의 근거가 된 기록마다 문장 끝에 [S12], [C5], [N3], [P1], [D2026-09-22] 형식으로 ref를 붙입니다. 도구 결과에 나온 ref만 씁니다.
- 기록에서 찾지 못한 것은 찾지 못했다고 말합니다. 추측하거나 지어내지 않습니다.
- 파일 변경(C)은 실제로 바꾼 결과물이고, 세션(S)과 창 제목은 무엇을 보고 있었는지일 뿐입니다. '완료', '해결'은 본인의 메모(N)나 직접 쓴 세션 제목에 근거가 있을 때만 씁니다.
- '다음에 할 일'은 본인이 적은 다음 할 일(P의 next_action)이나 메모에 있을 때만 말합니다. 없으면 기록상 마지막으로 한 일을 알려 주고, 다음 할 일은 기록에 없다고 말합니다.
- 한국어로 간결하게, 5문장 이내로 답합니다. 필요하면 짧은 목록을 씁니다."""

TOOLS = [
    ToolSpec("search_history",
             "업무 기록을 키워드로 검색합니다 (세션 제목·메모, 바뀐 파일 경로, 메모, 일일 리뷰, 창 제목). "
             "이슈 번호(GOV-1418), 클래스·파일 이름, 한국어 낱말이 잘 맞습니다. 여러 낱말은 공백으로 구분합니다.",
             {"type": "object", "properties": {
                 "query": {"type": "string"},
                 "date_from": {"type": "string", "description": "YYYY-MM-DD (선택)"},
                 "date_to": {"type": "string", "description": "YYYY-MM-DD (선택)"}},
              "required": ["query"]}),
    ToolSpec("get_day", "하루의 업무 기록(작업 세션, 파일 변경, 메모)을 봅니다.",
             {"type": "object", "properties": {"date": {"type": "string", "description": "YYYY-MM-DD"}},
              "required": ["date"]}),
    ToolSpec("list_projects", "프로젝트 목록과 각 프로젝트의 마지막 작업 시각, 본인이 적은 다음 할 일을 봅니다.",
             {"type": "object", "properties": {}}),
    ToolSpec("get_project",
             "프로젝트의 최근 상황(마지막 세션과 마지막으로 본 창, 최근 세션, 최근 파일 변경, 메모, 다음 할 일)을 봅니다.",
             {"type": "object", "properties": {"name": {"type": "string", "description": "프로젝트 이름"}},
              "required": ["name"]}),
    ToolSpec("get_change", "파일 변경 하나의 상세(바뀐 파일 목록과 바뀐 줄 일부)를 봅니다.",
             {"type": "object", "properties": {"ref": {"type": "string", "description": "C로 시작하는 ref (예: C12)"}},
              "required": ["ref"]}),
]


def _hm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts).date().isoformat()


def _parse_day(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ValueError(f"날짜 형식은 YYYY-MM-DD 입니다: {value!r}") from None


class HistoryTools:
    """Read-only views of the database for the model; remembers every reference it handed out."""

    def __init__(self, conn: sqlite3.Connection, repos_dir: Optional[Path] = None):
        self.conn = conn
        self.repos_dir = repos_dir
        self.sources: dict[str, dict] = {}
        self._projects = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM projects")}

    def project_name(self, pid: Optional[int]) -> str:
        return self._projects.get(pid, "미분류")

    def _cite(self, ref: str, source: dict) -> str:
        self.sources.setdefault(ref, source)
        return ref

    # ------------------------------------------------------------ tools

    def execute(self, name: str, args: dict) -> Any:
        handler = {"search_history": self.search_history, "get_day": self.get_day,
                   "list_projects": self.list_projects, "get_project": self.get_project,
                   "get_change": self.get_change}.get(name)
        if handler is None:
            raise ValueError(f"알 수 없는 도구: {name}")
        return handler(**{k: v for k, v in args.items() if v not in (None, "")})

    def search_history(self, query: str, date_from: Any = None, date_to: Any = None) -> dict:
        words = [w for w in re.findall(r"[\w가-힣.!#-]+", str(query)) if len(w) >= 2][:6]
        if not words:
            return {"results": [], "note": "검색할 낱말이 없습니다"}
        lo, hi = _parse_day(date_from), _parse_day(date_to)
        hits: dict[str, dict] = {}

        def add(ref: str, item: dict, source: dict) -> None:
            entry = hits.setdefault(ref, {"item": item, "score": 0})
            entry["score"] += 1
            self._cite(ref, source)

        for word in words:
            try:
                r = service.search(self.conn, word, lo, hi, None, limit=20)
            except Invalid:
                continue
            for s in r["sessions"]:
                add(f"S{s['id']}", {"ref": f"S{s['id']}", "type": "세션", "date": s["day"],
                                    "time": f"{_hm(s['start'])}-{_hm(s['end'])}",
                                    "project": self.project_name(s["project_id"]), "title": redact(s["title"]),
                                    **({"note": redact(s["note"])} if s["note"] else {})},
                    {"type": "session", "day": s["day"], "start": s["start"], "label": s["title"]})
            for f in r["files"]:
                ref = f"C{f['change_id']}"
                add(ref, {"ref": ref, "type": "파일 변경", "date": f["day"], "time": _hm(f["ts"]),
                          "folder": f["folder"], "path": f["path"],
                          "change": {"A": "추가", "M": "수정", "D": "삭제"}[f["kind"]]},
                    {"type": "change", "day": f["day"], "start": f["ts"], "label": f"{f['folder']}: {f['path']}"})
            for n in r["notes"]:
                add(f"N{n['id']}", {"ref": f"N{n['id']}", "type": "메모", "date": n["day"],
                                    "project": self.project_name(n["project_id"]), "text": redact(n["body"])},
                    {"type": "note", "day": n["day"], "label": n["body"][:60]})
            for v in r["reviews"]:
                ref = f"R{v['day']}"
                add(ref, {"ref": ref, "type": "일일 리뷰", "date": v["day"], "excerpt": redact(v["excerpt"])},
                    {"type": "review", "day": v["day"], "label": f"{v['day']} 리뷰"})
            for e in r["events"]:
                ref = f"D{e['day']}"
                add(ref, {"ref": ref, "type": "창 기록", "date": e["day"],
                          "time": f"{_hm(e['first_ts'])}-{_hm(e['last_ts'])}", "app": e["app"],
                          "title": redact(e["title"])},
                    {"type": "day", "day": e["day"], "label": f"{e['day']} 타임라인"})
            for p in r["projects"]:
                ref = f"P{p['id']}"
                add(ref, {"ref": ref, "type": "프로젝트", "name": p["name"],
                          "next_action": redact(p["next_action"] or ""),
                          "last_activity": _day_of(p["last_activity_ts"]) if p["last_activity_ts"] else None},
                    {"type": "project", "project_id": p["id"], "label": p["name"]})
        # Most query words matched first, then the most recent (ISO dates sort as text).
        ranked = sorted(hits.values(), key=lambda h: (h["score"], str(h["item"].get("date", ""))), reverse=True)
        results = [h["item"] for h in ranked[:MAX_RESULTS]]
        return {"words": words, "results": results, "total": len(hits)}

    def get_day(self, date: Any) -> dict:
        day = _parse_day(date)
        if day is None:
            raise ValueError("date가 필요합니다")
        t = service.timeline(self.conn, day)
        day_ref = self._cite(f"D{day.isoformat()}", {"type": "day", "day": day.isoformat(),
                                                     "label": f"{day.isoformat()} 타임라인"})
        sessions = []
        for s in t["sessions"]:
            if s["excluded"] or s["offwork"] or s["active_s"] < 60:
                continue
            ref = self._cite(f"S{s['id']}", {"type": "session", "day": s["day"], "start": s["start"],
                                             "label": s["title"]})
            sessions.append({"ref": ref, "time": f"{_hm(s['start'])}-{_hm(s['end'])}",
                             "minutes": round(s["active_s"] / 60), "project": self.project_name(s["project_id"]),
                             "title": redact(s["title"]), **({"note": redact(s["note"])} if s["note"] else {})})
        changes = []
        for c in t["changes"]:
            ref = self._cite(f"C{c['id']}", {"type": "change", "day": day.isoformat(), "start": c["ts"],
                                             "label": f"{c['folder']}: " + ", ".join(
                                                 f["path"].rsplit("/", 1)[-1] for f in c["files"][:3])})
            changes.append({"ref": ref, "time": _hm(c["ts"]), "folder": c["folder"],
                            "project": self.project_name(c["project_id"]),
                            "files": [{"path": f["path"], "change": {"A": "추가", "M": "수정", "D": "삭제"}[f["kind"]]}
                                      for f in c["files"][:15]]})
        notes = [{"ref": self._cite(f"N{n['id']}", {"type": "note", "day": n["day"], "label": n["body"][:60]}),
                  "project": self.project_name(n["project_id"]), "text": redact(n["body"])} for n in t["notes"]]
        return {"ref": day_ref, "date": day.isoformat(), "weekday": WEEKDAYS[day.weekday()],
                "active_minutes": round(t["total_active_s"] / 60), "sessions": sessions, "changes": changes,
                "notes": notes}

    def list_projects(self) -> dict:
        out = []
        for p in service.resume_overview(self.conn):
            ref = self._cite(f"P{p['id']}", {"type": "project", "project_id": p["id"], "label": p["name"]})
            last = p["last_session"]
            out.append({"ref": ref, "name": p["name"], "next_action": redact(p["next_action"] or ""),
                        "last_work": f"{last['day']} {_hm(last['end'])}" if last else None,
                        "minutes_last_7_days": round(p["week_active_s"] / 60)})
        return {"projects": out}

    def get_project(self, name: str) -> dict:
        wanted = str(name).strip().casefold()
        match = next((pid for pid, n in self._projects.items() if n.casefold() == wanted), None) or next(
            (pid for pid, n in self._projects.items() if wanted and wanted in n.casefold()), None)
        if match is None:
            return {"error": "그런 프로젝트가 없습니다", "projects": list(self._projects.values())}
        r = service.resume_project(self.conn, match)
        p_ref = self._cite(f"P{match}", {"type": "project", "project_id": match, "label": r["project"]["name"]})

        def session(s: dict) -> dict:
            ref = self._cite(f"S{s['id']}", {"type": "session", "day": s["day"], "start": s["start"],
                                             "label": s["title"]})
            return {"ref": ref, "date": s["day"], "time": f"{_hm(s['start'])}-{_hm(s['end'])}",
                    "title": redact(s["title"]), **({"note": redact(s["note"])} if s["note"] else {})}

        changes = []
        for c in self.conn.execute(
                "SELECT c.id, c.ts, f.label AS folder FROM changes c JOIN watched_folders f ON f.id = c.folder_id "
                "WHERE c.baseline = 0 AND f.project_id = ? ORDER BY c.ts DESC LIMIT 10", (match,)):
            files = [row["path"] for row in self.conn.execute(
                "SELECT path FROM change_files WHERE change_id = ? ORDER BY path LIMIT 8", (c["id"],))]
            ref = self._cite(f"C{c['id']}", {"type": "change", "day": _day_of(c["ts"]), "start": c["ts"],
                                             "label": f"{c['folder']}: " + ", ".join(
                                                 f.rsplit("/", 1)[-1] for f in files[:3])})
            changes.append({"ref": ref, "date": _day_of(c["ts"]), "time": _hm(c["ts"]), "files": files})
        return {
            "ref": p_ref, "name": r["project"]["name"],
            "next_action": redact(r["project"]["next_action"] or ""),
            "last_session": session(r["last_session"]) if r["last_session"] else None,
            "last_window": ({"app": r["last_activity"]["app"], "title": redact(r["last_activity"]["title"])}
                            if r["last_activity"] else None),
            "recent_sessions": [session(s) for s in r["recent_sessions"][:8]],
            "recent_changes": changes,
            "notes": [{"ref": self._cite(f"N{n['id']}", {"type": "note", "day": n["day"], "label": n["body"][:60]}),
                       "date": n["day"], "text": redact(n["body"])} for n in r["notes"][:8]],
        }

    def get_change(self, ref: str) -> dict:
        m = re.fullmatch(r"C(\d+)", str(ref).strip())
        if not m:
            raise ValueError("ref는 C12 같은 형식입니다")
        row = self.conn.execute(
            "SELECT c.*, f.label AS folder, f.project_id FROM changes c JOIN watched_folders f "
            "ON f.id = c.folder_id WHERE c.id = ? AND c.baseline = 0", (int(m.group(1)),)).fetchone()
        if row is None:
            return {"error": "그런 파일 변경이 없습니다"}
        files = self.conn.execute("SELECT * FROM change_files WHERE change_id = ? ORDER BY path LIMIT 20",
                                  (row["id"],)).fetchall()
        excerpt: list[str] = []
        if self.repos_dir is not None:
            from dulwich.repo import Repo
            from .ai_context import DIFF_LINES_PER_CHANGE, _excerpt
            from .snapshots import repo_path
            path = repo_path(self.repos_dir, row["folder_id"])
            if path.exists():
                repo = Repo(str(path))
                try:
                    for f in files:
                        if f["is_text"] and len(excerpt) < DIFF_LINES_PER_CHANGE:
                            lines = _excerpt(repo, f["path"], f["old_blob"], f["new_blob"],
                                             DIFF_LINES_PER_CHANGE - len(excerpt))
                            if lines:
                                excerpt += [f"# {f['path']}"] + lines
                finally:
                    repo.close()
        ref = self._cite(f"C{row['id']}", {"type": "change", "day": _day_of(row["ts"]), "start": row["ts"],
                                           "label": f"{row['folder']}: " + ", ".join(
                                               f["path"].rsplit("/", 1)[-1] for f in files[:3])})
        return {"ref": ref, "date": _day_of(row["ts"]), "time": _hm(row["ts"]), "folder": row["folder"],
                "project": self.project_name(row["project_id"]),
                "files": [{"path": f["path"], "change": {"A": "추가", "M": "수정", "D": "삭제"}[f["kind"]],
                           **({"lines": f"+{f['lines_added']} -{f['lines_removed']}"}
                              if f["lines_added"] is not None else {})} for f in files],
                "diff_excerpt": excerpt}


def verify_answer(text: str, sources: dict[str, dict]) -> tuple[str, list[str], int]:
    """Keep only references a tool really returned; returns (text, cited refs in order, dropped count)."""
    cited: list[str] = []
    dropped = 0

    def keep(m: re.Match) -> str:
        # "[S2, S14]" is split into "[S2][S14]" so each reference is checked and shown on its own.
        nonlocal dropped
        out = []
        for ref in re.split(r"\s*,\s*", m.group(1)):
            if ref in sources:
                if ref not in cited:
                    cited.append(ref)
                out.append(f"[{ref}]")
            else:
                dropped += 1
        return "".join(out)

    clean = REF_IN_TEXT.sub(keep, text)
    clean = re.sub(r"[ \t]+([.,)])", r"\1", clean)
    return clean.strip(), cited, dropped


@dataclass
class MCPRun:
    text: str
    usage: Usage
    calls: list[ToolTrace]
    sources: dict[str, dict]


def _run_via_mcp(conn: sqlite3.Connection, model: Any, system: str, prompt: str, demo: bool) -> MCPRun:
    """Run the question through Claude Code with the app's MCP server, which logs every tool
    call and the references it returned to a trace file; citations are checked against that."""
    from .claude_link import MODULE, console_python
    db_file = conn.execute("PRAGMA database_list").fetchone()["file"]
    with tempfile.TemporaryDirectory(prefix="workhistory-ask-") as d:
        trace = Path(d) / "trace.jsonl"
        server = [console_python(), "-m", MODULE, "--db", db_file, "--trace", str(trace)] + (["--demo"] if demo else [])
        text, usage = model.run_mcp(system, prompt, server, [t.name for t in TOOLS])
        calls, sources = [], {}
        if trace.exists():
            for line in trace.read_text(encoding="utf-8").splitlines():
                entry = json.loads(line)
                calls.append(ToolTrace(entry["tool"], entry["args"], entry["ok"]))
                for ref, source in entry["sources"].items():
                    sources.setdefault(ref, source)
    return MCPRun(text, usage, calls, sources)


def ask(conn: sqlite3.Connection, question: Any, demo: bool, factory: "ai_service.ModelFactory" = None,
        repos_dir: Optional[Path] = None, today: Optional[date] = None) -> dict:
    if not isinstance(question, str) or not question.strip():
        raise Invalid("질문을 입력하세요")
    question = question.strip()[:MAX_QUESTION_CHARS]
    ai_service.check_allowed(conn, demo)
    sessionstore.ensure_all(conn)
    today = today or date.today()
    tools = HistoryTools(conn, repos_dir)
    model_name = ai_service._model_name(conn, demo)
    started, usage = time.time(), Usage()
    system = SYSTEM.format(today=today.isoformat(), weekday=WEEKDAYS[today.weekday()])
    try:
        model = ai_service._model(conn, demo, factory or ai_service.default_factory)
        if hasattr(model, "run_mcp"):
            # Claude Code runs the tool loop itself, against this app's MCP server (another
            # process): commit first so it sees the rebuilt sessions and is not blocked by our lock.
            conn.commit()
            result = _run_via_mcp(conn, model, system, redact(question), demo)
            tools.sources = result.sources
        else:
            result = model.run_tools(system, redact(question), TOOLS, tools.execute)
        usage = result.usage
    except AIError as e:
        ai_service.log_run(conn, "ask", model_name, started, usage, 0, False, e.kind)
        raise
    answer, cited, dropped = verify_answer(result.text, tools.sources)
    ai_service.log_run(conn, "ask", model_name, started, usage, len(result.calls), True, None)
    return {
        "question": question,
        "answer": answer or "기록에서 답을 찾지 못했습니다.",
        "citations": [{"ref": ref, **tools.sources[ref]} for ref in cited],
        "dropped_refs": dropped,
        "tool_calls": [{"name": c.name, "args": c.args, "ok": c.ok} for c in result.calls],
        "usage": usage.__dict__,
    }
