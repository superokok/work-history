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
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from . import ai_service, service, store, work
from .ai_client import AIError, ToolSpec, ToolTrace, Usage
from .redact import redact
from .store import Invalid

MAX_QUESTION_CHARS = 500
MAX_RESULTS = 15
_REF = r"[WCNDR][0-9A-Za-z-]+"
REF_IN_TEXT = re.compile(rf"\[({_REF}(?:\s*,\s*{_REF})*)\]")
WEEKDAYS = "월화수목금토일"

SYSTEM = """당신은 한 사람의 업무 기록을 찾아보고 질문에 답하는 도우미입니다.
도구로 이 사람의 기록(작업 기록 W: 하루 동안 작업 폴더별 시간과 웹·소통·기타 시간, 파일 변경 C, 메모 N, 하루 D)을 찾아보고, 확인한 사실만으로 답합니다.

반드시 지킬 것:
- 오늘은 {today}({weekday}요일)입니다. '어제', '지난주' 같은 말은 이 날짜를 기준으로 날짜 범위를 계산해 도구에 넘깁니다.
- 답을 쓰기 전에 필요한 만큼 도구로 확인합니다. 키워드 검색이 비면 다른 표현(영문·한글 이름, 번호, 비슷한 낱말)으로 다시 찾거나 해당 날짜를 직접 봅니다.
- 답의 근거가 된 기록마다 문장 끝에 [W2026-09-22-1], [C5], [N3], [D2026-09-22] 형식으로 ref를 붙입니다. 도구 결과에 나온 ref만 씁니다.
- 기록에서 찾지 못한 것은 찾지 못했다고 말합니다. 추측하거나 지어내지 않습니다.
- 파일 변경(C)은 실제로 바꾼 결과물이고, 작업 기록(W)과 창 제목은 무엇을 보고 있었는지일 뿐입니다. '완료', '해결'은 본인의 메모(N)에 근거가 있을 때만 씁니다.
- '다음에 할 일'은 본인이 적은 메모에 있을 때만 말합니다. 없으면 기록상 마지막으로 한 일을 알려 주고, 다음 할 일은 기록에 없다고 말합니다.
- 한국어로 간결하게, 5문장 이내로 답합니다. 필요하면 짧은 목록을 씁니다."""

TOOLS = [
    ToolSpec("search_history",
             "업무 기록을 키워드로 검색합니다 (바뀐 파일 경로, 메모, 창 제목). "
             "번호, 파일·문서 이름, 낱말이 잘 맞습니다. 여러 낱말은 공백으로 구분합니다.",
             {"type": "object", "properties": {
                 "query": {"type": "string"},
                 "date_from": {"type": "string", "description": "YYYY-MM-DD (선택)"},
                 "date_to": {"type": "string", "description": "YYYY-MM-DD (선택)"}},
              "required": ["query"]}),
    ToolSpec("get_day", "하루의 업무 기록(작업 폴더별 시간과 시간대, 웹·소통·기타 시간, 파일 변경, 메모)을 봅니다.",
             {"type": "object", "properties": {"date": {"type": "string", "description": "YYYY-MM-DD"}},
              "required": ["date"]}),
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

    def _cite(self, ref: str, source: dict) -> str:
        self.sources.setdefault(ref, source)
        return ref

    # ------------------------------------------------------------ tools

    def execute(self, name: str, args: dict) -> Any:
        handler = {"search_history": self.search_history, "get_day": self.get_day,
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
            for f in r["files"]:
                ref = f"C{f['change_id']}"
                add(ref, {"ref": ref, "type": "파일 변경", "date": f["day"], "time": _hm(f["ts"]),
                          "folder": f["folder"], "path": f["path"],
                          "change": {"A": "추가", "M": "수정", "D": "삭제"}[f["kind"]]},
                    {"type": "change", "day": f["day"], "start": f["ts"], "label": f"{f['folder']}: {f['path']}"})
            for n in r["notes"]:
                add(f"N{n['id']}", {"ref": f"N{n['id']}", "type": "메모", "date": n["day"],
                                    "text": redact(n["body"])},
                    {"type": "note", "day": n["day"], "label": n["body"][:60]})
            for e in r["events"]:
                ref = f"D{e['day']}"
                add(ref, {"ref": ref, "type": "창 기록", "date": e["day"],
                          "time": f"{_hm(e['first_ts'])}-{_hm(e['last_ts'])}", "app": e["app"],
                          "title": redact(e["title"])},
                    {"type": "day", "day": e["day"], "label": f"{e['day']} 타임라인"})
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
        records, offwork = work.day(self.conn, day)
        work_out = []
        for i, r in enumerate(records, 1):
            if r.seconds < 60:
                continue
            ref = self._cite(f"W{day.isoformat()}-{i}", {"type": "work", "day": day.isoformat(),
                                                         "start": r.spans[0][0], "end": r.spans[-1][1],
                                                         "label": r.name})
            work_out.append({"ref": ref, **({"folder": r.name} if r.folder_id else {"kind": r.name}),
                             "minutes": round(r.seconds / 60),
                             "times": ", ".join(f"{_hm(a)}-{_hm(b)}" for a, b in r.spans[:8]),
                             "windows": [{"app": app, "title": redact(title), "minutes": round(sec / 60)}
                                         for (app, title), sec in r.top_windows(5) if sec >= 60]})
        changes = []
        for c in t["changes"]:
            ref = self._cite(f"C{c['id']}", {"type": "change", "day": day.isoformat(), "start": c["ts"],
                                             "label": f"{c['folder']}: " + ", ".join(
                                                 f["path"].rsplit("/", 1)[-1] for f in c["files"][:3])})
            changes.append({"ref": ref, "time": _hm(c["ts"]), "folder": c["folder"],
                            "files": [{"path": f["path"], "change": {"A": "추가", "M": "수정", "D": "삭제"}[f["kind"]]}
                                      for f in c["files"][:15]]})
        notes = [{"ref": self._cite(f"N{n['id']}", {"type": "note", "day": n["day"], "label": n["body"][:60]}),
                  "text": redact(n["body"])} for n in t["notes"]]
        return {"ref": day_ref, "date": day.isoformat(), "weekday": WEEKDAYS[day.weekday()],
                "active_minutes": round(t["total_active_s"] / 60), "offwork_minutes": round(offwork / 60),
                "work": work_out, "changes": changes, "notes": notes}

    def get_change(self, ref: str) -> dict:
        m = re.fullmatch(r"C(\d+)", str(ref).strip())
        if not m:
            raise ValueError("ref는 C12 같은 형식입니다")
        row = self.conn.execute(
            "SELECT c.*, f.label AS folder FROM changes c JOIN watched_folders f "
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
    from .config import console_python
    db_file = conn.execute("PRAGMA database_list").fetchone()["file"]
    with tempfile.TemporaryDirectory(prefix="workhistory-ask-") as d:
        trace = Path(d) / "trace.jsonl"
        server = [console_python(), "-m", "workhistory.mcp_server", "--db", db_file, "--trace", str(trace)] \
            + (["--demo"] if demo else [])
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
    today = today or date.today()
    tools = HistoryTools(conn, repos_dir)
    model_name = ai_service._model_name(conn, demo)
    started, usage = time.time(), Usage()
    system = SYSTEM.format(today=today.isoformat(), weekday=WEEKDAYS[today.weekday()])
    try:
        model = ai_service._model(conn, demo, factory or ai_service.default_factory)
        if hasattr(model, "run_mcp"):
            # Claude Code runs the tool loop itself, against this app's MCP server (another
            # process): commit first so it is not blocked by our lock.
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
