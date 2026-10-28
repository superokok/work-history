"""Entry point: python -m workhistory [--demo] [--window] [--port N]"""

import argparse
import logging
import sys
import threading
import webbrowser
from pathlib import Path

try:
    import dulwich  # noqa: F401  (required for folder snapshots)
except ImportError:
    sys.exit("\n".join([
        "필요한 패키지(dulwich)가 없습니다. 프로젝트 폴더에서 다음을 실행하세요:",
        "  uv venv .venv --python 3.12",
        "  uv pip install --python .venv/Scripts/python.exe -e .[claude]",
        "그다음 start-workhistory.cmd 로 실행하세요.",
    ]))

from . import demo
from .collector import Collector, default_probe
from .config import DB_FILENAME, DEFAULT_PORT, DEMO_DB_FILENAME, default_data_dir
from .db import Database
from .server import App, create_server, find_running_instance, snapshots_dir
from .snapshots import Snapshotter
from .sync import Syncer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="workhistory", description="개인 작업 기록")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--db", type=Path, help="데이터베이스 파일 경로")
    parser.add_argument("--demo", action="store_true", help="별도 DB에 샘플 데이터로 실행")
    parser.add_argument("--window", action="store_true", help="pywebview 창으로 열기")
    parser.add_argument("--no-browser", action="store_true", help="브라우저를 열지 않음")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    # A Korean Windows console is cp949; never crash on characters it can't show.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    db_path = args.db or default_data_dir() / (DEMO_DB_FILENAME if args.demo else DB_FILENAME)
    db = Database(db_path)
    try:
        db.migrate()
    except Exception as e:
        print(f"데이터베이스를 열 수 없습니다 ({db_path}): {e}", file=sys.stderr)
        return 1

    syncer = snapshotter = collector = None
    if args.demo:
        if demo.seed(db, snapshots_dir(db)):
            print("데모 데이터를 생성했습니다.")
    else:
        probe = default_probe()
        if probe is None:
            print("창 기록을 읽을 수 없습니다 (Windows 전용). 작업 폴더 기록만 동작합니다.", file=sys.stderr)
        else:
            collector = Collector(db, probe)
        syncer = Syncer(db, collector)
        snapshotter = Snapshotter(db, snapshots_dir(db))

    try:
        server = create_server(App(db, syncer, demo=args.demo, snapshotter=snapshotter), args.port)
    except OSError as e:
        if find_running_instance(args.port):
            # Launched twice (e.g. double-clicked the shortcut): reuse the running app.
            url = f"http://127.0.0.1:{args.port}/"
            print(f"이미 실행 중입니다: {url}")
            if not args.no_browser:
                webbrowser.open(url)
            return 0
        print(f"포트 {args.port}를 다른 프로그램이 쓰고 있습니다. --port로 다른 포트를 지정하세요: {e}",
              file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"작업 기록: {url}  (DB: {db_path})")
    print("이 창을 닫으면 작업 기록이 종료됩니다. (기록은 저장되어 있습니다)")

    if collector:
        collector.start()
    if syncer:
        syncer.start()
    if snapshotter:
        snapshotter.start()
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()

    try:
        if args.window:
            try:
                import webview  # optional dependency
            except ImportError:
                print("pywebview가 설치되어 있지 않아 브라우저로 엽니다. (pip install pywebview)")
            else:
                webview.create_window("작업 기록", url, width=1280, height=860)
                webview.start()  # blocks until the window is closed
                return 0
        if not args.no_browser:
            webbrowser.open(url)
        threading.Event().wait()  # until Ctrl+C
    except KeyboardInterrupt:
        pass
    finally:
        if syncer:
            syncer.stop()
        if collector:
            collector.stop()
        if snapshotter:
            snapshotter.stop()
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
