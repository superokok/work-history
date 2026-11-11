"""AI features wired to the database: settings, consent, run log, reports."""

import sqlite3
import threading
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


# The report being written now (one at a time in this app): how far it is, and whether the user
# asked to stop. A report in several requests stops before its next request.
_progress = {"running": False, "step": 0, "total": 0, "cancel": False}
_progress_lock = threading.Lock()


def report_progress() -> dict:
    with _progress_lock:
        return {k: v for k, v in _progress.items() if k != "cancel"}


def cancel_report() -> dict:
    with _progress_lock:
        if _progress["running"]:
            _progress["cancel"] = True
        return {"running": _progress["running"]}


def _step(i: int, total: int) -> None:
    with _progress_lock:
        if _progress["cancel"]:
            raise AIError("AI 초안 작성을 멈췄습니다.", "cancelled")
        _progress.update(step=i, total=total)


def preview(conn: sqlite3.Connection, kind: str, day: date, repos_dir: Optional[Path] = None) -> dict:
    """Exactly what would be sent to the AI (nothing is sent)."""
    start, end = period(kind, day)
    parts = ai_context.plan(conn, start, end, repos_dir)
    if len(parts) == 1:
        return {"start": start.isoformat(), "end": end.isoformat(),
                "prompt": ai_report.prompt(kind, parts[0]), "system": ai_report.SYSTEM}
    text = [f"--- {i + 1}/{len(parts)}번째 요청 ---\n" + ai_report.prompt(kind, c) for i, c in enumerate(parts)]
    text.append(f"--- 요약: 위 {len(parts)}번의 결과(업무 이름과 한 일 문장)를 보내 요약 한두 문장을 받습니다 ---")
    return {"start": start.isoformat(), "end": end.isoformat(), "prompt": "\n\n".join(text),
            "system": ai_report.SYSTEM + "\n\n[요약 지시문]\n" + ai_report.SUMMARY_SYSTEM}


def generate_report(conn: sqlite3.Connection, kind: str, day: date, demo: bool,
                    factory: ModelFactory = default_factory, repos_dir: Optional[Path] = None) -> dict:
    """A report draft. Records that fit go in one request; more go in several (work folders packed
    together, nothing left out), joined by code, and a small last request writes the summary."""
    check_allowed(conn, demo)
    start, end = period(kind, day)
    with _progress_lock:
        if _progress["running"]:
            raise Invalid("다른 AI 초안을 작성하고 있습니다. 끝난 뒤 다시 시도하세요.")
        _progress.update(running=True, step=0, total=0, cancel=False)
    try:
        return _generate(conn, kind, day, demo, factory, repos_dir, start, end)
    finally:
        with _progress_lock:
            _progress.update(running=False, cancel=False)


def _generate(conn, kind, day, demo, factory, repos_dir, start, end) -> dict:
    parts = ai_context.plan(conn, start, end, repos_dir)
    ctx = ai_context.merge(parts, start, end)
    if ctx.empty:
        raise Invalid("이 기간에는 보고서로 만들 업무 기록이 없습니다")
    total = len(parts) + (1 if len(parts) > 1 else 0)
    model_name = _model_name(conn, demo)
    started, usage = time.time(), Usage()
    summary_failed = False
    try:
        model = _model(conn, demo, factory)
        raws = []
        for i, c in enumerate(parts):
            _step(i + 1, total)
            raw, used = model.generate_json(ai_report.SYSTEM, ai_report.prompt(kind, c), ai_report.SCHEMA)
            usage.add(used)
            raws.append(raw)
        if len(parts) == 1:
            raw = raws[0]
        else:
            projects = ai_report.join_parts(list(zip(parts, raws)))
            _step(total, total)
            try:
                got, used = model.generate_json(
                    ai_report.SUMMARY_SYSTEM,
                    ai_report.summary_prompt(kind, start.isoformat(), end.isoformat(), projects),
                    ai_report.SUMMARY_SCHEMA)
                usage.add(used)
                got = got if isinstance(got, dict) else {}
                summary = got.get("summary") or ""
                projects = ai_report.join_same_work(projects, got.get("same_work"))
            except AIError as e:
                if e.kind in ("cancelled", "auth", "not_allowed", "config"):
                    raise
                summary, summary_failed = "", True  # the work lines are written; the summary is the user's
            raw = {"summary": summary, "projects": projects}
    except AIError as e:
        log_run(conn, f"report_{kind}", model_name, started, usage, 0, False, e.kind)
        raise
    report = ai_report.validate(kind, ctx, raw)
    if len(parts) > 1:
        report.notes.append(f"기록이 많아 작업 폴더별로 묶어 {len(parts)}번에 나눠 정리했습니다. 빠진 기록은 없습니다.")
    if summary_failed:
        report.notes.append("요약을 받지 못했습니다. 요약은 직접 작성하세요.")
    if ctx.trimmed:
        report.notes.append("줄여서 보낸 부분: " + ", ".join(ctx.trimmed))
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
