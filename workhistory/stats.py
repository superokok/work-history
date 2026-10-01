"""Real-use validation numbers: python -m workhistory.stats [--days 7]

How often did the user have to correct the automatic results? High rates
point at the grouping constants in config.py or at the auto titles.
"""

import argparse
import sys
from datetime import date, timedelta

from . import sessionstore
from .config import DB_FILENAME, default_data_dir
from .db import Database
from .review import duration


def collect(db: Database, days: int) -> list[dict]:
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    with db.connect() as conn:
        sessionstore.ensure_all(conn)
        rows = {r["day"]: dict(r) for r in conn.execute(
            "SELECT day, COUNT(*) AS sessions, SUM(active_s) AS active_s, "
            "SUM(offwork_s) AS offwork_s, "
            "SUM(CASE WHEN excluded = 0 AND project_id IS NOT NULL THEN active_s - offwork_s ELSE 0 END) "
            "AS classified_s, "
            "SUM(title_edited) AS renamed, SUM(project_manual) AS project_fixed, "
            "SUM(locked) AS range_fixed, SUM(excluded) AS excluded, SUM(note != '') AS session_notes "
            "FROM sessions WHERE day >= ? GROUP BY day", (since,))}
        notes = dict(conn.execute(
            "SELECT day, COUNT(*) FROM notes WHERE day >= ? GROUP BY day", (since,)).fetchall())
        reviews = {r[0] for r in conn.execute("SELECT day FROM daily_reviews WHERE day >= ?", (since,))}
    out = []
    for i in range(days):
        d = (date.today() - timedelta(days=days - 1 - i)).isoformat()
        r = rows.get(d, {"sessions": 0, "active_s": 0, "offwork_s": 0, "classified_s": 0, "renamed": 0,
                         "project_fixed": 0,
                         "range_fixed": 0, "excluded": 0, "session_notes": 0})
        out.append({"day": d, **r, "notes": notes.get(d, 0), "review_saved": d in reviews})
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="workhistory.stats", description="실사용 검증 통계")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--db", default=str(default_data_dir() / DB_FILENAME))
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    db = Database(args.db)
    db.migrate()
    days = collect(db, max(args.days, 1))

    print(f"{'날짜':<11}{'활동':>10}{'업무 외':>8}{'세션':>5}{'분류율':>7}{'제목수정':>6}{'프로젝트수정':>7}"
          f"{'범위조정':>6}{'제외':>4}{'메모':>4}{'리뷰':>4}")
    for d in days:
        work = (d["active_s"] or 0) - (d["offwork_s"] or 0)
        rate = f"{d['classified_s'] / work:.0%}" if work > 0 else "-"  # share of work time
        print(f"{d['day']:<11}{duration(d['active_s'] or 0):>10}{duration(d['offwork_s'] or 0):>9}"
              f"{d['sessions']:>6}{rate:>8}"
              f"{d['renamed']:>9}{d['project_fixed']:>11}{d['range_fixed']:>9}{d['excluded']:>6}"
              f"{d['notes'] + d['session_notes']:>6}{'O' if d['review_saved'] else '-':>6}")

    total = sum(d["sessions"] for d in days)
    if total:
        def pct(key: str) -> str:
            return f"{sum(d[key] for d in days) / total:.0%}"
        print()
        print(f"세션 {total}개 중 제목 수정 {pct('renamed')}, 프로젝트 수정 {pct('project_fixed')}, "
              f"병합·분할 {pct('range_fixed')}, 제외 {pct('excluded')}")
        print("해석: 제목 수정이 많으면 자동 제목 개선, 병합·분할이 많으면 세션 기준값(config.py) 조정,")
        print("      프로젝트 수정이 많거나 분류율이 낮으면 프로젝트 규칙 보완이 필요합니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
