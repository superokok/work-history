"""MCP server: lets Claude Code (or any MCP client) read the user's work history.

Run over stdio by the MCP client:  python -m workhistory.mcp_server [--demo] [--db PATH]

All tools are read-only. Because results leave the PC through the MCP client
(Claude Code sends them to its model provider), the server answers only after
the user enabled "Claude Code 연결 허용" in the app (the demo database is
always allowed). Every text passes the same secret redaction as the app's own
AI features, and the "업무 앱만 기록" / exclusion settings apply unchanged.
"""

import argparse
import json
import logging
import sys
from datetime import date as Date, timedelta
from pathlib import Path
from typing import Optional

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from . import __version__, ai_context, ai_report, demo
from .ai_agent import HistoryTools
from .ai_claude import is_claude_model
from .config import DB_FILENAME, DEMO_DB_FILENAME, default_data_dir
from .db import Database
from .server import snapshots_dir
from .store import Invalid

META_KEY = "mcp_enabled"
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                            open_world_hint=False)

INSTRUCTIONS = """이 서버는 사용자 본인의 업무 기록(작업 세션 S, 파일 변경 C, 메모 N, 프로젝트 P, 하루 D, 리뷰 R)을 읽기 전용으로 제공합니다.
- 기록에서 확인한 사실만 말하고, 근거 ref를 [C12]처럼 함께 적으세요.
- 파일 변경(C)은 실제 결과물이고, 세션(S)과 창 제목은 무엇을 보고 있었는지일 뿐입니다. '완료/해결'은 본인의 메모(N)에 근거가 있을 때만 쓰세요.
- '다음 할 일'은 본인이 적은 next_action이나 메모에 있을 때만 말하세요.
- 보고서를 쓸 때는 get_report_data로 자료를 받아 그 규칙(rules)을 따르세요."""

DISABLED_MESSAGE = ("작업 기록 앱에서 Claude Code 연결이 꺼져 있습니다. "
                    "앱의 개인정보 탭 → 'Claude Code 연결 허용'을 켠 뒤 다시 시도하세요.")


def _meta(conn, key: str) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else ""


def is_enabled(conn, app_session: bool = False) -> bool:
    if _meta(conn, META_KEY) == "1":
        return True
    # Started by the app itself for its own AI features, which the user pointed at Claude Code.
    return app_session and is_claude_model(_meta(conn, "ai_model"))


def build(db: Database, repos_dir: Optional[Path], demo_mode: bool = False,
          trace: Optional[Path] = None) -> MCPServer:
    """`trace`: append one JSON line per tool call (name, args, the references it returned),
    so the app can check an answer's citations against what the tools really showed."""
    server = MCPServer("workhistory", title="작업 기록", instructions=INSTRUCTIONS, version=__version__)

    def run(fn, tool: str = "", args: Optional[dict] = None):
        """Open the database, enforce consent, run one read-only lookup."""
        with db.connect() as conn:
            if not demo_mode and not is_enabled(conn, app_session=trace is not None):
                return {"error": DISABLED_MESSAGE}
            tools = HistoryTools(conn, repos_dir)
            try:
                out = fn(tools, conn)
            except (ValueError, Invalid) as e:
                out = {"error": str(e)}
        if trace is not None:
            line = {"tool": tool, "args": args or {}, "ok": "error" not in out, "sources": tools.sources}
            with open(trace, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        return out

    @server.tool(annotations=READ_ONLY)
    def search_history(query: str, date_from: Optional[str] = None, date_to: Optional[str] = None) -> dict:
        """업무 기록을 키워드로 검색합니다 (세션 제목·메모, 바뀐 파일 경로, 메모, 일일 리뷰, 창 제목).
        이슈 번호(GOV-1418), 클래스·파일 이름, 한국어 낱말이 잘 맞습니다. 낱말은 공백으로 구분합니다.
        date_from/date_to: YYYY-MM-DD (선택)."""
        return run(lambda t, c: t.search_history(query, date_from, date_to), "search_history", {"query": query, "date_from": date_from, "date_to": date_to})

    @server.tool(annotations=READ_ONLY)
    def get_day(date: str) -> dict:
        """하루(YYYY-MM-DD)의 업무 기록: 작업 세션, 파일 변경, 메모."""
        return run(lambda t, c: t.get_day(date), "get_day", {"date": date})

    @server.tool(annotations=READ_ONLY)
    def list_projects() -> dict:
        """프로젝트 목록과 마지막 작업 시각, 최근 7일 작업 시간, 본인이 적은 다음 할 일."""
        return run(lambda t, c: t.list_projects(), "list_projects", {})

    @server.tool(annotations=READ_ONLY)
    def get_project(name: str) -> dict:
        """프로젝트의 최근 상황: 마지막 세션과 마지막으로 본 창, 최근 세션, 최근 파일 변경, 메모, 다음 할 일."""
        return run(lambda t, c: t.get_project(name), "get_project", {"name": name})

    @server.tool(annotations=READ_ONLY)
    def get_change(ref: str) -> dict:
        """파일 변경 하나(C12 같은 ref)의 바뀐 파일 목록과 바뀐 줄 일부(비밀값은 가려짐)."""
        return run(lambda t, c: t.get_change(ref), "get_change", {"ref": ref})

    @server.tool(annotations=READ_ONLY)
    def get_report_data(kind: str, date: str) -> dict:
        """업무일지(kind=daily: 그 날) 또는 주간보고(kind=weekly: 그 날이 속한 월~일) 작성용 자료와 작성 규칙.
        반환되는 rules를 따르고, 각 항목에 근거 ref를 붙여 보고서를 쓰세요."""
        def report(tools, conn):
            if kind not in ("daily", "weekly"):
                raise ValueError("kind는 daily 또는 weekly입니다")
            try:
                day = Date.fromisoformat(date)
            except ValueError:
                raise ValueError("date는 YYYY-MM-DD 형식입니다") from None
            start, end = (day, day) if kind == "daily" else (
                day - timedelta(days=day.weekday()), day - timedelta(days=day.weekday()) + timedelta(days=6))
            ctx = ai_context.build(conn, start, end, repos_dir)
            labels = ai_report.SECTION_LABELS[kind]
            return {"kind": ai_report.KINDS[kind], "period": ctx.payload["period"],
                    "sections": {"done": labels[0], "next": labels[1], "issues": labels[2]},
                    "rules": ai_report.SYSTEM, "data": ctx.payload}
        return run(report, "get_report_data", {"kind": kind, "date": date})

    return server



def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="workhistory.mcp_server", description="작업 기록 MCP 서버 (stdio)")
    parser.add_argument("--db", type=Path, help="데이터베이스 파일 경로")
    parser.add_argument("--demo", action="store_true", help="데모 DB 사용 (가상 데이터)")
    parser.add_argument("--trace", type=Path, help=argparse.SUPPRESS)  # set by the app for its own AI runs
    args = parser.parse_args(argv)
    # stdout carries the protocol; anything human-readable goes to stderr.
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    db_path = args.db or default_data_dir() / (DEMO_DB_FILENAME if args.demo else DB_FILENAME)
    db = Database(db_path)
    db.migrate()
    if args.demo and args.trace is None:
        demo.seed(db, snapshots_dir(db))
    build(db, snapshots_dir(db), demo_mode=args.demo, trace=args.trace).run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
