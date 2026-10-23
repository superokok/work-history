"""AI features wired to the database: settings, consent, run log, reports."""

import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from . import ai_claude, ai_context, ai_local, ai_report
from .ai_claude import ClaudeCodeModel, is_claude_model
from .ai_client import AIError, Model, Usage
from .claude_link import ClaudeLink, ClaudeLinkError, find_cli
from .store import Invalid

# model name -> Model
ModelFactory = Callable[[str], Model]
MODELS = ai_claude.MODELS
DEFAULT_MODEL = "claude-code:sonnet"  # suggested in the UI, and used for the demo database


def all_models() -> list[str]:
    """Claude Code models, then the local models whose files are in the llm folder."""
    return list(MODELS) + ai_local.available_models()


def default_factory(name: str) -> Model:
    if ai_local.is_local_model(name):
        return ai_local.LocalModel(name)  # llama-server on this PC; nothing leaves it
    return ClaudeCodeModel(name)  # the Claude Code signed in on this PC; no API key


# ---------------------------------------------------------------- settings

def _meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                 "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))


def chosen_model(conn: sqlite3.Connection) -> Optional[str]:
    """The model the user turned on, or None: AI is off until the user picks one (that is the consent)."""
    name = _meta(conn, "ai_model")
    return name if name in all_models() else None  # e.g. a retired provider's or removed local model counts as off


def _model_name(conn: sqlite3.Connection, demo: bool) -> str:
    return chosen_model(conn) or DEFAULT_MODEL


def _model(conn: sqlite3.Connection, demo: bool, factory: ModelFactory) -> Model:
    return factory(_model_name(conn, demo))


def get_settings(conn: sqlite3.Connection, demo: bool) -> dict:
    return {
        "model": chosen_model(conn) or "",
        "models": all_models(),
        "local_models": ai_local.available_models(),
        "default_model": DEFAULT_MODEL,
        "enabled": chosen_model(conn) is not None,
        # Cheap PATH lookup only; login is checked by '연결 확인' (check_connection).
        "claude_installed": find_cli() is not None,
        "demo": demo,
    }


def get_mcp(conn: sqlite3.Connection, demo: bool, link: "ClaudeLink") -> dict:
    """Claude Code connection: the user's consent plus Claude Code's actual registration state."""
    try:
        claude = link.status()
    except ClaudeLinkError as e:
        claude = {"installed": bool(link.cli), "name": link.name, "registered": False, "ours": False,
                  "error": str(e)}
    return {
        "enabled": demo or _meta(conn, "mcp_enabled") == "1",
        "demo": demo,
        "claude": claude,
        "manual_command": ["claude", "mcp", "add", "--scope", "user", "--transport", "stdio", link.name, "--",
                           *link.server_command()],
    }


def set_mcp(conn: sqlite3.Connection, data: dict, demo: bool, link: "ClaudeLink") -> dict:
    """One switch in the app: consent + registration with Claude Code, kept in step."""
    enable = data.get("enabled") is True
    if enable:
        previous = _meta(conn, "mcp_enabled")
        _set_meta(conn, "mcp_enabled", "1")
        try:
            link.register()
        except ClaudeLinkError as e:
            _set_meta(conn, "mcp_enabled", previous or "0")  # stay consistent: no consent without a working link
            raise Invalid(str(e)) from None
    else:
        # Withdraw consent first: the server stops answering even if unregistering fails.
        _set_meta(conn, "mcp_enabled", "0")
        conn.commit()
        try:
            link.unregister()
        except ClaudeLinkError as e:
            raise Invalid("연결 허용은 껐지만 Claude Code 등록 해제에 실패했습니다: " + str(e)) from None
    return get_mcp(conn, demo, link)


def check_connection(conn: sqlite3.Connection, demo: bool, factory: ModelFactory = default_factory) -> dict:
    """Is Claude Code installed and signed in? Sends no work data."""
    model = _model(conn, demo, factory)
    model.check()
    return {"ok": True, "model": model.name}


def update_settings(conn: sqlite3.Connection, data: dict, demo: bool) -> dict:
    if "model" in data:
        if data["model"] not in all_models() + [""]:
            raise Invalid("지원하지 않는 모델입니다")
        # Choosing a model is the consent to give it the records: through Claude Code to Anthropic, or to
        # the local model on this PC (the UI asks first); "" turns AI off again.
        _set_meta(conn, "ai_model", data["model"])
    return get_settings(conn, demo)


def check_allowed(conn: sqlite3.Connection, demo: bool) -> None:
    """Real work data goes to the AI only after the user turned it on; the demo data always may."""
    if demo or chosen_model(conn) is not None:
        return
    raise AIError("AI가 꺼져 있습니다. 개인정보 탭의 AI 설정에서 모델을 고르면 사용할 수 있습니다.",
                  "not_allowed")


# ---------------------------------------------------------------- run log

def log_run(conn: sqlite3.Connection, kind: str, model: str, started: float, usage: Usage,
            tool_calls: int, ok: bool, error_kind: Optional[str]) -> None:
    conn.execute(
        "INSERT INTO ai_runs (kind, model, started_at, duration_ms, requests, input_tokens, output_tokens, "
        "thinking_tokens, tool_calls, ok, error_kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (kind, model, started, int((time.time() - started) * 1000), usage.requests, usage.input_tokens,
         usage.output_tokens, usage.thinking_tokens, tool_calls, 1 if ok else 0, error_kind))
    conn.commit()  # keep the log even when the caller rolls back after an error


def run_stats(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS runs, SUM(ok) AS ok, SUM(input_tokens) AS input_tokens, "
        "SUM(output_tokens) AS output_tokens, SUM(thinking_tokens) AS thinking_tokens, "
        "AVG(duration_ms) AS avg_ms FROM ai_runs").fetchone()
    return {k: row[k] or 0 for k in row.keys()}


# ---------------------------------------------------------------- reports

def period(kind: str, day: date) -> tuple[date, date]:
    if kind == "daily":
        return day, day
    if kind == "weekly":
        monday = day - timedelta(days=day.weekday())
        return monday, monday + timedelta(days=6)
    raise Invalid("kind는 'daily' 또는 'weekly'여야 합니다")


def preview(conn: sqlite3.Connection, kind: str, day: date, repos_dir: Optional[Path] = None) -> dict:
    """Exactly what would be sent to the AI (nothing is sent)."""
    start, end = period(kind, day)
    ctx = ai_context.build(conn, start, end, repos_dir)
    return {"start": start.isoformat(), "end": end.isoformat(),
            "prompt": ai_report.prompt(kind, ctx), "system": ai_report.SYSTEM}


def generate_report(conn: sqlite3.Connection, kind: str, day: date, demo: bool,
                    factory: ModelFactory = default_factory, repos_dir: Optional[Path] = None) -> dict:
    check_allowed(conn, demo)
    start, end = period(kind, day)
    ctx = ai_context.build(conn, start, end, repos_dir)
    if ctx.empty:
        raise Invalid("이 기간에는 보고서로 만들 업무 기록이 없습니다")
    model_name = _model_name(conn, demo)
    started, usage = time.time(), Usage()
    try:
        model = factory(model_name)
        raw, usage = model.generate_json(ai_report.SYSTEM, ai_report.prompt(kind, ctx), ai_report.SCHEMA)
    except AIError as e:
        log_run(conn, f"report_{kind}", model_name, started, usage, 0, False, e.kind)
        raise
    report = ai_report.validate(kind, ctx, raw)
    log_run(conn, f"report_{kind}", model_name, started, usage, 0, True, None)
    out = ai_report.to_dict(report, ctx)
    out["usage"] = usage.__dict__
    out["saved"] = get_report(conn, kind, day)
    return out


def get_report(conn: sqlite3.Connection, kind: str, day: date) -> Optional[dict]:
    start, end = period(kind, day)
    row = conn.execute("SELECT * FROM reports WHERE kind = ? AND start_day = ? AND end_day = ?",
                       (kind, start.isoformat(), end.isoformat())).fetchone()
    return dict(row) if row else None


def save_report(conn: sqlite3.Connection, data: dict) -> Optional[dict]:
    kind = data.get("kind")
    day = _day(data.get("day"))
    start, end = period(kind, day)
    body = data.get("body")
    if not isinstance(body, str) or len(body) > 200_000:
        raise Invalid("보고서 내용이 올바르지 않습니다")
    if not body.strip():
        conn.execute("DELETE FROM reports WHERE kind = ? AND start_day = ? AND end_day = ?",
                     (kind, start.isoformat(), end.isoformat()))
        return None
    now = time.time()
    conn.execute(
        "INSERT INTO reports (kind, start_day, end_day, body, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (kind, start_day, end_day) DO UPDATE SET body = excluded.body, updated_at = excluded.updated_at",
        (kind, start.isoformat(), end.isoformat(), body, now, now))
    return get_report(conn, kind, day)


def _day(value: Any) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise Invalid(f"날짜 형식이 올바르지 않습니다: {value!r}") from None
