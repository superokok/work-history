"""Effect measurement for the user's own before/after numbers (자동화 전후 시간, 고친 비율).

Stored only on this PC and only as numbers: how long a report took to write, how many lines of
the AI draft were kept, how quickly a search led to opening a record. Never report text, search
queries, titles or paths.
"""

import difflib
import json
import sqlite3
import statistics
import time
from typing import Any, Optional

from .store import Invalid

REPORT_METHODS = ("ai", "manual")  # AI draft then edited / written by hand in the app
CLIENT_KINDS = ("search", "search_open", "source_open")
MAX_SECONDS = 6 * 3600


def _seconds(value: Any) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value != value:
        raise Invalid("측정값이 올바르지 않습니다")
    return round(min(max(float(value), 0.0), MAX_SECONDS), 1)


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _insert(conn: sqlite3.Connection, kind: str, data: dict, now: Optional[float] = None) -> None:
    conn.execute("INSERT INTO usage_events (ts, kind, data) VALUES (?, ?, ?)",
                 (time.time() if now is None else now, kind, json.dumps(data)))


def record_report(conn: sqlite3.Connection, kind: str, start_day: str, body: str, metrics: Any,
                  now: Optional[float] = None) -> None:
    """One row per save. A writing session (sid) may be saved several times; the last save counts."""
    if not isinstance(metrics, dict):
        return
    method = metrics.get("method")
    sid = metrics.get("sid")
    if method not in REPORT_METHODS or not isinstance(sid, str) or not 0 < len(sid) <= 40:
        raise Invalid("측정값이 올바르지 않습니다")
    final = _lines(body)
    data = {"sid": sid, "report": kind, "start": start_day, "method": method,
            "active_s": _seconds(metrics.get("active_s")), "wall_s": _seconds(metrics.get("wall_s")),
            "lines": len(final)}
    draft = metrics.get("ai_draft")
    if method == "ai" and isinstance(draft, str) and len(draft) <= 200_000:
        ai = _lines(draft)
        kept = sum(b.size for b in difflib.SequenceMatcher(None, ai, final, autojunk=False).get_matching_blocks())
        data.update(ai_lines=len(ai), kept=kept, edited=len(final) - kept)
    _insert(conn, "report", data, now)


def record_client(conn: sqlite3.Connection, body: Any, now: Optional[float] = None) -> None:
    if not isinstance(body, dict) or body.get("kind") not in CLIENT_KINDS:
        raise Invalid("알 수 없는 측정 항목입니다")
    data = {}
    if body["kind"] == "search_open":
        data["after_s"] = _seconds(body.get("after_s"))
    if body["kind"] == "source_open":
        data["from"] = body.get("from") if body.get("from") in ("report", "ask") else "other"
    _insert(conn, body["kind"], data, now)


def _median(values: list[float]) -> Optional[float]:
    return round(statistics.median(values), 1) if values else None


def summary(conn: sqlite3.Connection, days: int = 30, now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    since = now - days * 86400
    events = [(r["ts"], r["kind"], json.loads(r["data"])) for r in conn.execute(
        "SELECT ts, kind, data FROM usage_events WHERE ts >= ? ORDER BY ts", (since,))]

    latest: dict[str, dict] = {}  # last save of each writing session
    for ts, kind, data in events:
        if kind == "report":
            latest[data["sid"]] = {**data, "saved_at": ts}
    reports = sorted(latest.values(), key=lambda r: r["saved_at"])
    by_method = {}
    for method in REPORT_METHODS:
        rows = [r for r in reports if r["method"] == method]
        by_method[method] = {"count": len(rows), "median_active_s": _median([r["active_s"] for r in rows])}
    ai_rows = [r for r in reports if r.get("ai_lines")]
    kept = sum(r["kept"] for r in ai_rows)
    total_ai = sum(r["ai_lines"] for r in ai_rows)

    runs = conn.execute(
        "SELECT kind, COUNT(*) AS n, AVG(duration_ms) AS avg_ms FROM ai_runs "
        "WHERE ok = 1 AND started_at >= ? GROUP BY kind", (since,)).fetchall()
    gen = [r for r in runs if r["kind"].startswith("report_")]
    gen_n = sum(r["n"] for r in gen)
    asks = sum(r["n"] for r in runs if r["kind"] == "ask")

    opens = [d["after_s"] for _, k, d in events if k == "search_open"]
    return {
        "days": days,
        "reports": reports,
        "by_method": by_method,
        "ai_kept_ratio": round(kept / total_ai, 3) if total_ai else None,
        "ai_generated": gen_n,
        "ai_generate_avg_s": round(sum(r["n"] * r["avg_ms"] for r in gen) / gen_n / 1000, 1) if gen_n else None,
        "asks": asks,
        "searches": sum(1 for _, k, _ in events if k == "search"),
        "search_opens": len(opens),
        "search_open_median_s": _median(opens),
        "source_opens": sum(1 for _, k, _ in events if k == "source_open"),
    }

