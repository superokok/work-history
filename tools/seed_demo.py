"""Put the fictional demo scenario into the app's own (real) database, for a presentation.

Run through seed-demo.cmd (source checkout) or with the app's Python:
    .venv/Scripts/python.exe tools/seed_demo.py

1. Stops the running app, if any.
2. Moves the current database and snapshots to <data folder>/backup/<time>/ (nothing is deleted).
3. Creates a new database with the two-week demo scenario dated around today; today's work
   runs 07:00-09:00 and the scenario stops there, so live work during the presentation follows it.
   No work folders: the scenario's folders do not exist on this PC, so file changes come from
   the folders added in the app's settings.
4. Settings: AI model Claude Code Sonnet, record every app.
5. Starts the app again.

Unlike `--demo` (a separate demo.db, nothing recorded) this is the real app: no demo banner,
window activity is recorded, and the AI treats the data as real (the model is set here).
"""

import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHERS = ("start-workhistory.cmd", "작업 기록.cmd")  # source checkout, portable zip
SETTINGS = {"ai_model": "claude-code:sonnet", "track_mode": "all"}


def running_port():
    """The port of the running (non-demo) app, or None."""
    for port in range(5730, 5740):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=2) as r:
                if not json.load(r).get("demo"):
                    return port
        except OSError:
            pass
    return None


def stop_app() -> None:
    port = running_port()
    if port is None:
        return
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/quit", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json", "X-Work-History": "1"})
    urllib.request.urlopen(req, timeout=5).read()
    for _ in range(30):  # until it has let go of the database
        time.sleep(1)
        if running_port() is None:
            time.sleep(2)
            break
    print(f"켜져 있던 앱을 껐습니다 (포트 {port}).")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from workhistory import config, demo
    from workhistory.db import Database

    stop_app()
    db_path = config.default_data_dir() / config.DB_FILENAME
    snaps = db_path.with_name(db_path.stem + "-snapshots")  # where server.snapshots_dir() looks
    old = [p for p in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm"), snaps) if p.exists()]
    if old:
        backup = db_path.parent / "backup" / time.strftime("%Y%m%d-%H%M%S")
        backup.mkdir(parents=True)
        for p in old:
            shutil.move(str(p), str(backup / p.name))
        print(f"지금까지의 기록을 옮겼습니다: {backup}")

    db = Database(db_path)
    db.migrate()
    demo.seed(db, morning=True)  # no repos_dir: no work folders
    with db.connect() as conn:
        for key, value in SETTINGS.items():
            conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                         "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))
    print("데모 데이터를 넣었습니다: 지난 2주 + 오늘 07:00~09:00 (작업 폴더 없음).")
    print("설정: AI 모델 Claude Code · Sonnet, 모든 앱 기록.")

    launcher = next((ROOT / name for name in LAUNCHERS if (ROOT / name).exists()), None)
    if launcher:
        subprocess.Popen(["cmd", "/c", str(launcher)], cwd=str(ROOT),
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        print("앱을 켰습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
