"""AI features wired to the database: settings, consent, run log, reports."""

import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from . import ai_claude, ai_context, ai_local, ai_report
from .ai_claude import ClaudeCodeModel, find_cli
from .ai_client import AIError, Model, Usage
from .store import Invalid

# model name -> Model
ModelFactory = Callable[[str], Model]
MODELS = ai_claude.MODELS
DEFAULT_MODEL = "claude-code:sonnet"  # the suggestion when no local model is installed


def default_model() -> str:
    """Suggested in the UI and used for the demo database: the largest local model if any is installed
    (records stay on the PC; the larger model followed the report rules best), else Claude Code."""
    files = ai_local.model_files()
    if files:
        return max(files, key=lambda name: files[name].stat().st_size)
    return DEFAULT_MODEL


def all_models(conn: Optional[sqlite3.Connection] = None) -> list[str]:
    """The local models in the llm folder, plus Claude Code models where Claude Code is part of the install."""
    claude = list(MODELS) if ai_claude.bundled() else []
    return claude + ai_local.available_models()


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
    return name if name in all_models(conn) else None  # a removed local model counts as off


def _model_name(conn: sqlite3.Connection, demo: bool) -> str:
    return chosen_model(conn) or default_model()


def _model(conn: sqlite3.Connection, demo: bool, factory: ModelFactory) -> Model:
    return factory(_model_name(conn, demo))


def get_settings(conn: sqlite3.Connection, demo: bool) -> dict:
    return {
        "model": chosen_model(conn) or "",
        "models": all_models(conn),
        "local_models": ai_local.available_models(),
        "default_model": default_model(),
        "enabled": chosen_model(conn) is not None,
        # False in the released (offline) zip: the UI then says nothing about Claude Code at all.
        "claude_bundled": ai_claude.bundled(),
        # Cheap PATH lookup only; login is checked by '연결 확인' (check_connection).
        "claude_installed": ai_claude.bundled() and find_cli() is not None,
        "demo": demo,
    }


def check_connection(conn: sqlite3.Connection, demo: bool, factory: ModelFactory = default_factory) -> dict:
    """Is Claude Code installed and signed in? Sends no work data."""
    model = _model(conn, demo, factory)
    model.check()
    return {"ok": True, "model": model.name}


def update_settings(conn: sqlite3.Connection, data: dict, demo: bool) -> dict:
    if "model" in data:
        if data["model"] not in all_models(conn) + [""]:
            raise Invalid("지원하지 않는 모델입니다")
        # Choosing a model is the consent to give it the records: through Claude Code to Anthropic, or to
        # the local model on this PC (the UI asks first); "" turns AI off again.
        _set_meta(conn, "ai_model", data["model"])
    return get_settings(conn, demo)


def check_allowed(conn: sqlite3.Connection, demo: bool) -> None:
    """Real work data goes to the AI only after the user turned it on; the demo data always may."""
    if demo or chosen_model(conn) is not None:
        return
    raise AIError("AI가 꺼져 있습니다. 설정 탭의 AI 설정에서 모델을 고르면 사용할 수 있습니다.",
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


def _plan(conn: sqlite3.Connection, kind: str, start: date, end: date,
          repos_dir: Optional[Path]) -> tuple[ai_context.Context, list[ai_context.Context]]:
    """(context of the whole period, contexts of its days when the week is sent day by day).

    A week whose records fit the budget as they are goes in one request. A bigger one would lose
    detail (fewer window titles, shorter diff excerpts, short records left out), so each day is
    written from its own records first and the drafts are merged: nothing is left out."""
    whole = ai_context.build(conn, start, end, repos_dir)
    if kind != "weekly" or not whole.trimmed:
        return whole, []
    days, ref = [], 1
    d = start
    while d <= end:
        c = ai_context.build(conn, d, d, repos_dir, first_ref=ref)
        ref = c.next_ref
        if not c.empty:
            days.append(c)
        d += timedelta(days=1)
    return ai_context.merge(days, start, end), days


def preview(conn: sqlite3.Connection, kind: str, day: date, repos_dir: Optional[Path] = None) -> dict:
    """Exactly what would be sent to the AI (nothing is sent)."""
    start, end = period(kind, day)
    ctx, days = _plan(conn, kind, start, end, repos_dir)
    if not days:
        return {"start": start.isoformat(), "end": end.isoformat(),
                "prompt": ai_report.prompt(kind, ctx), "system": ai_report.SYSTEM}
    parts = [f"--- {c.start.isoformat()} (날짜별 초안 {i + 1}/{len(days)}) ---\n" + ai_report.prompt("daily", c)
             for i, c in enumerate(days)]
    parts.append("--- 합치기: 위 날짜별 초안(한 일과 근거 번호)을 보내 주간보고로 합칩니다 ---")
    return {"start": start.isoformat(), "end": end.isoformat(), "prompt": "\n\n".join(parts),
            "system": ai_report.SYSTEM + "\n\n[합치기 지시문]\n" + ai_report.MERGE_SYSTEM}


def generate_report(conn: sqlite3.Connection, kind: str, day: date, demo: bool,
                    factory: ModelFactory = default_factory, repos_dir: Optional[Path] = None) -> dict:
    check_allowed(conn, demo)
    start, end = period(kind, day)
    ctx, days = _plan(conn, kind, start, end, repos_dir)
    if ctx.empty:
        raise Invalid("이 기간에는 보고서로 만들 업무 기록이 없습니다")
    model_name = _model_name(conn, demo)
    started, usage = time.time(), Usage()
    try:
        model = _model(conn, demo, factory)
        if not days:
            raw, usage = model.generate_json(ai_report.SYSTEM, ai_report.prompt(kind, ctx), ai_report.SCHEMA)
        else:
            drafts = []
            for c in days:
                raw, used = model.generate_json(ai_report.SYSTEM, ai_report.prompt("daily", c), ai_report.SCHEMA)
                usage.add(used)
                drafts.append(ai_report.validate("daily", c, raw))
            raw, used = model.generate_json(ai_report.MERGE_SYSTEM,
                                            ai_report.merge_prompt(start.isoformat(), end.isoformat(), drafts),
                                            ai_report.SCHEMA)
            usage.add(used)
    except AIError as e:
        log_run(conn, f"report_{kind}", model_name, started, usage, 0, False, e.kind)
        raise
    report = ai_report.validate(kind, ctx, raw)
    if days:
        report.notes.append(f"기록이 많아 {len(days)}일을 하루씩 정리한 뒤 주간보고로 합쳤습니다.")
    if ctx.trimmed:
        report.notes.append("하루 기록도 많아 줄여서 보낸 날이 있습니다: " + ", ".join(ctx.trimmed))
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
