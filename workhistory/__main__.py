"""Entry point: python -m workhistory [--demo] [--window] [--port N]

Port: 5730, or the next free one up to 5739 when another program holds it (one app per
database is ensured by instance.claim, not by the port).

The launchers start it with pythonw.exe: no console window, so closing a window by mistake
can't stop the recording. It stops with the app's own "앱 종료" button (POST /api/quit).
"""

import argparse
import logging
import logging.handlers
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

from . import demo, instance
from .collector import Collector, default_probe
from .config import DB_FILENAME, DEFAULT_PORT, DEMO_DB_FILENAME, PORT_TRIES, default_data_dir
from .db import Database
from .server import App, create_server_in, snapshots_dir
from .snapshots import Snapshotter
from .sync import Syncer

log = logging.getLogger("workhistory")

LOG_FILENAME = "workhistory.log"
LOG_MAX_BYTES = 1_000_000


def _has_console() -> bool:
    return sys.stdout is not None  # None under pythonw.exe


def _fail(message: str) -> int:
    """Report a startup error: on the console, or in a message box when there is none."""
    if _has_console():
        print(message, file=sys.stderr)
    else:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "작업 기록", 0x10)  # MB_ICONERROR
    return 1


def _setup_logging(verbose: bool, data_dir: Path) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    if _has_console():
        logging.basicConfig(level=level, format=fmt)
        return
    # No console: a small rotating file next to the database (never holds event content).
    data_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(data_dir / LOG_FILENAME, maxBytes=LOG_MAX_BYTES,
                                                   backupCount=1, encoding="utf-8")
    logging.basicConfig(level=level, format=fmt, handlers=[handler])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="workhistory", description="개인 작업 기록")
    parser.add_argument("--port", type=int, help=f"이 포트만 사용 (기본: {DEFAULT_PORT}부터 빈 포트)")
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

    db_path = args.db or default_data_dir() / (DEMO_DB_FILENAME if args.demo else DB_FILENAME)
    _setup_logging(args.verbose, db_path.parent)
    ports = [args.port] if args.port else list(range(DEFAULT_PORT, DEFAULT_PORT + PORT_TRIES))
    if not instance.claim(db_path):
        # Launched twice (e.g. double-clicked the shortcut): open the running app.
        port = instance.find(ports, db_path)
        if port is None:
            return _fail("작업 기록이 이미 실행 중인데 응답하지 않습니다. 잠시 뒤에 다시 실행해 주세요.")
        url = f"http://127.0.0.1:{port}/"
        print(f"이미 실행 중입니다: {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return 0
    db = Database(db_path)
    try:
        db.migrate()
    except Exception as e:
        return _fail(f"데이터베이스를 열 수 없습니다 ({db_path}): {e}")

    syncer = snapshotter = collector = None
    if args.demo:
        if demo.seed(db, snapshots_dir(db)):
            print("데모 데이터를 생성했습니다.")
    else:
        probe = default_probe()
        if probe is None:
            logging.warning("window activity can't be read here (Windows only); recording file changes only")
        else:
            collector = Collector(db, probe)
        syncer = Syncer(db, collector)
        snapshotter = Snapshotter(db, snapshots_dir(db))

    quit_event = threading.Event()
    try:
        server = create_server_in(App(db, syncer, demo=args.demo, snapshotter=snapshotter,
                                      on_quit=quit_event.set), ports)
    except OSError as e:
        where = f"포트 {ports[0]}" if len(ports) == 1 else f"포트 {ports[0]}~{ports[-1]}"
        return _fail(f"{where}를 모두 다른 프로그램이 쓰고 있어 작업 기록을 시작하지 못했습니다: {e}")
    if server.server_address[1] != ports[0]:
        log.warning("port %d is taken by another program; using %d", ports[0], server.server_address[1])
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    log.info("listening on %s (DB: %s)", url, db_path)
    print(f"작업 기록: {url}  (DB: {db_path})")
    print("종료: 앱의 개인정보 탭 → 실행 → '앱 종료' (또는 Ctrl+C)")

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
                window = webview.create_window("작업 기록", url, width=1280, height=860)
                threading.Thread(target=lambda: (quit_event.wait(), window.destroy()), daemon=True).start()
                webview.start()  # blocks until the window is closed
                return 0
        if not args.no_browser:
            webbrowser.open(url)
        while not quit_event.wait(1):  # until "앱 종료" or Ctrl+C
            pass
        log.info("quit requested")
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
