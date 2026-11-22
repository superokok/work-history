"""Make a demo database whose dates are fixed: one week, Monday to Friday (for a presentation).

`--demo` makes demo.db dated around the day it is first created, so its "last week" moves with the
calendar. This writes the same fictional scenario into a separate database where only the chosen
week has records (window activity, notes and file changes of its work folders), and the AI model
is set to Claude Code · Sonnet. Nothing else is touched.

    .venv/Scripts/python.exe tools/demo_week.py                    # 2026-11-16 (Mon) - 11-20 (Fri)
    .venv/Scripts/python.exe tools/demo_week.py --week 2026-12-07  # another week (its Monday)

Then run the app on it:

    start-workhistory.cmd --demo --db "%LOCALAPPDATA%\\WorkHistory\\demo-1116.db"
"""

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

DEFAULT_WEEK = "2026-11-16"
AI_MODEL = "claude-code:sonnet"


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from workhistory import config, demo
    from workhistory.db import Database
    from workhistory.server import snapshots_dir

    parser = argparse.ArgumentParser(description="날짜를 고정한 데모 DB 만들기 (월–금 한 주)")
    parser.add_argument("--week", default=DEFAULT_WEEK, help=f"그 주의 월요일 (기본 {DEFAULT_WEEK})")
    parser.add_argument("--db", help="만들 DB 파일 (기본: 데이터 폴더의 demo-MMDD.db)")
    args = parser.parse_args(argv)

    monday = date.fromisoformat(args.week)
    if monday.weekday() != 0:
        print(f"{monday}는 월요일이 아닙니다. 그 주의 월요일을 넣으세요.")
        return 1
    db_path = Path(args.db) if args.db else config.default_data_dir() / f"demo-{monday:%m%d}.db"
    db = Database(db_path)
    snaps = snapshots_dir(db)
    if db_path.exists() or snaps.exists():
        print(f"이미 있습니다: {db_path} (또는 {snaps.name}). 지우거나 --db로 다른 이름을 주세요.")
        return 1

    db_path.parent.mkdir(parents=True, exist_ok=True)
    db.migrate()
    # The scenario's "this week" starts at the Monday after: as of that midnight, only the week
    # before it (the chosen one) has happened.
    now = datetime.combine(monday + timedelta(days=7), datetime.min.time()).timestamp()
    demo.seed(db, snaps, now=now)
    with db.connect() as conn:
        conn.execute("INSERT INTO meta (key, value) VALUES ('ai_model', ?) "
                     "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (AI_MODEL,))
    print(f"데모 DB를 만들었습니다: {db_path}")
    print(f"기록 날짜: {monday} (월) ~ {monday + timedelta(days=4)} (금), AI 모델: Claude Code · Sonnet")
    print(f'실행: start-workhistory.cmd --demo --db "{db_path}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
