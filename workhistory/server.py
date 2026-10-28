"""Localhost-only JSON API and static UI server."""

import json
import logging
import mimetypes
import re
import socket
import time
import urllib.request
from dataclasses import asdict
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from . import ai_agent, ai_remote, ai_service, service, sessionstore, snapshots, usage
from .ai_client import AIError
from .autostart import Launch
from .autostart import default as default_launch
from .db import Database
from .store import load_tracking
from .snapshots import Snapshotter
from .sync import Syncer, recent_apps, reset_window_cursors

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY = 1_000_000
WRITE_HEADER = "X-Work-History"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}


class ApiError(Exception):
    def __init__(self, status: int, message: str, kind: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.kind = kind


# AI failures: our own refusal/config problems are 4xx, upstream trouble 502/503/429.
AI_ERROR_STATUS = {"not_allowed": 403, "config": 409, "auth": 502, "quota": 429,
                   "network": 503, "server": 503, "blocked": 422, "invalid_output": 502}


class App:
    """Everything a request handler needs."""

    def __init__(self, db: Database, syncer: Optional[Syncer], demo: bool = False,
                 ai_factory: ai_service.ModelFactory = ai_service.default_factory,
                 snapshotter: Optional[Snapshotter] = None, repos_dir: Optional[Path] = None,
                 launch: Optional[Launch] = None):
        self.db = db
        self.launch = launch or default_launch()
        self.syncer = syncer
        self.snapshotter = snapshotter
        self.repos_dir = Path(repos_dir) if repos_dir else snapshots_dir(db)
        self.demo = demo
        self.ai_factory = ai_factory
        self.routes: list[tuple[str, re.Pattern, Callable]] = []
        self._register_routes()

    def route(self, method: str, pattern: str):
        def decorator(fn):
            self.routes.append((method, re.compile(f"^{pattern}$"), fn))
            return fn
        return decorator

    def _register_routes(self) -> None:  # noqa: C901 - flat list of endpoints
        r = self.route

        @r("GET", "/api/status")
        def status(conn, query, body):
            last = self.syncer.last_result if self.syncer else None
            collector = self.syncer.collector if self.syncer else None
            return {
                "demo": self.demo,
                "paused": service.is_paused(conn),
                "db_path": self.db.path,
                # False when window activity can't be recorded (only file changes then).
                "collecting": collector is not None,
                "collector_error": collector.last_error if collector else None,
                "sync": asdict(last) if last else None,
            }

        @r("POST", "/api/sync")
        def sync_now(conn, query, body):
            if self.syncer is None:
                raise ApiError(409, "데모 모드에서는 동기화하지 않습니다")
            return asdict(self.syncer.run_now())

        @r("GET", "/api/timeline")
        def timeline(conn, query, body):
            project = query.get("project") or None
            if project not in (None, "none") and not project.isdigit():
                raise ApiError(400, "project 필터가 올바르지 않습니다")
            return service.timeline(conn, _day(query.get("date")), project, query.get("app") or None)

        @r("POST", "/api/assign")
        def assign(conn, query, body):
            return {"changed": service.assign_project(conn, body.get("event_ids"), body.get("project_id"))}

        @r("PATCH", r"/api/sessions/(\d+)")
        def update_session(conn, query, body, sid):
            return sessionstore.update_session(conn, int(sid), body)

        @r("POST", "/api/sessions/merge")
        def merge_sessions(conn, query, body):
            return sessionstore.merge_sessions(conn, body.get("ids"))

        @r("POST", r"/api/sessions/(\d+)/split")
        def split_session(conn, query, body, sid):
            return sessionstore.split_session(conn, int(sid), body.get("at"))

        @r("GET", "/api/review")
        def get_review(conn, query, body):
            return service.get_review(conn, _day(query.get("date")))

        @r("POST", "/api/review")
        def save_review(conn, query, body):
            return {"saved": service.save_review(conn, body)}

        @r("GET", "/api/resume")
        def resume_overview(conn, query, body):
            return service.resume_overview(conn)

        @r("GET", r"/api/resume/(\d+)")
        def resume_project(conn, query, body, pid):
            return service.resume_project(conn, int(pid))

        @r("GET", "/api/projects")
        def projects(conn, query, body):
            return service.list_projects(conn)

        @r("POST", "/api/projects")
        def create_project(conn, query, body):
            return service.create_project(conn, body)

        @r("PATCH", r"/api/projects/(\d+)")
        def update_project(conn, query, body, pid):
            return service.update_project(conn, int(pid), body)

        @r("DELETE", r"/api/projects/(\d+)")
        def delete_project(conn, query, body, pid):
            service.delete_project(conn, int(pid))
            return {"ok": True}

        @r("POST", r"/api/projects/(\d+)/rules")
        def add_rule(conn, query, body, pid):
            return service.add_rule(conn, int(pid), body)

        @r("DELETE", r"/api/rules/(\d+)")
        def delete_rule(conn, query, body, rid):
            service.delete_rule(conn, int(rid))
            return {"ok": True}

        @r("POST", "/api/notes")
        def create_note(conn, query, body):
            return service.create_note(conn, body)

        @r("PATCH", r"/api/notes/(\d+)")
        def update_note(conn, query, body, nid):
            return service.update_note(conn, int(nid), body)

        @r("DELETE", r"/api/notes/(\d+)")
        def delete_note(conn, query, body, nid):
            service.delete_note(conn, int(nid))
            return {"ok": True}

        @r("GET", "/api/search")
        def search(conn, query, body):
            project = query.get("project") or None
            if project not in (None, "none") and not project.isdigit():
                raise ApiError(400, "project 필터가 올바르지 않습니다")
            return service.search(
                conn, query.get("q", ""),
                _day(query["from"]) if query.get("from") else None,
                _day(query["to"]) if query.get("to") else None,
                project)

        @r("GET", "/api/tracking")
        def tracking(conn, query, body):
            return service.get_tracking(conn)

        @r("POST", "/api/tracking/mode")
        def tracking_mode(conn, query, body):
            return service.set_track_mode(conn, body)

        @r("POST", "/api/tracking/rules")
        def add_track_rule(conn, query, body):
            return service.add_track_rule(conn, body)

        @r("DELETE", r"/api/tracking/rules/(\d+)")
        def delete_track_rule(conn, query, body, rid):
            return service.delete_track_rule(conn, int(rid))

        @r("GET", "/api/tracking/apps")
        def tracking_apps(conn, query, body):
            if self.syncer is None:
                raise ApiError(409, "데모 모드에서는 창 기록을 읽지 않습니다")
            days = query.get("days", "7")
            if not days.isdigit() or not 1 <= int(days) <= 90:
                raise ApiError(400, "days는 1~90 사이여야 합니다")
            return recent_apps(conn, load_tracking(conn) or [], int(days))

        @r("POST", "/api/tracking/reapply")
        def tracking_reapply(conn, query, body):
            if self.syncer is None:
                raise ApiError(409, "데모 모드에서는 동기화하지 않습니다")
            days = body.get("days", 30)
            if not isinstance(days, int) or not 1 <= days <= 365:
                raise ApiError(400, "days는 1~365 사이여야 합니다")
            reset_window_cursors(conn, days)
            conn.commit()
            return asdict(self.syncer.run_now())

        @r("GET", "/api/ai/settings")
        def ai_settings(conn, query, body):
            return {**ai_service.get_settings(conn, self.demo), "stats": ai_service.run_stats(conn)}

        @r("POST", "/api/ai/settings")
        def update_ai_settings(conn, query, body):
            return ai_service.update_settings(conn, body, self.demo)

        @r("POST", "/api/ai/check")
        def check_ai(conn, query, body):
            return ai_service.check_connection(conn, self.demo, self.ai_factory)

        # Registered AI servers (OpenAI-compatible, by address). Keys travel only in request bodies.
        @r("POST", "/api/ai/servers")
        def add_ai_server(conn, query, body):
            return ai_remote.add_endpoint(conn, body)

        @r("POST", "/api/ai/servers/models")
        def ai_server_models(conn, query, body):
            return {"models": ai_remote.fetch_models(body.get("url"), body.get("api_key"))}

        @r("PATCH", r"/api/ai/servers/(\d+)")
        def update_ai_server(conn, query, body, sid):
            return ai_remote.update_endpoint(conn, int(sid), body)

        @r("DELETE", r"/api/ai/servers/(\d+)")
        def delete_ai_server(conn, query, body, sid):
            ai_remote.delete_endpoint(conn, int(sid))
            return {"ok": True}

        @r("POST", r"/api/ai/servers/(\d+)/check")
        def check_ai_server(conn, query, body, sid):
            ai_remote.model(conn, ai_remote.model_name(int(sid))).check()
            return {"ok": True}

        @r("GET", "/api/launch")
        def get_launch(conn, query, body):
            return {**self.launch.status(), "demo": self.demo}

        @r("POST", "/api/launch")
        def set_launch(conn, query, body):
            if self.demo:
                raise ApiError(409, "데모 모드에서는 자동 실행을 바꾸지 않습니다")
            if not isinstance(body.get("autostart"), bool):
                raise ApiError(400, "autostart는 true/false입니다")
            return {**self.launch.set_autostart(body["autostart"]), "demo": self.demo}

        @r("POST", "/api/launch/shortcut")
        def create_shortcut(conn, query, body):
            if self.demo:
                raise ApiError(409, "데모 모드에서는 바로가기를 만들지 않습니다")
            return {**self.launch.create_shortcut(), "demo": self.demo}

        @r("POST", "/api/ai/ask")
        def ai_ask(conn, query, body):
            return ai_agent.ask(conn, body.get("question"), self.demo, self.ai_factory, self.repos_dir)

        @r("GET", "/api/ai/preview")
        def ai_preview(conn, query, body):
            return ai_service.preview(conn, query.get("kind", ""), _day(query.get("date")), self.repos_dir)

        @r("POST", "/api/ai/report")
        def ai_report(conn, query, body):
            return ai_service.generate_report(conn, body.get("kind", ""), _day(body.get("day")),
                                              self.demo, self.ai_factory, self.repos_dir)

        @r("GET", "/api/reports")
        def get_report(conn, query, body):
            return {"saved": ai_service.get_report(conn, query.get("kind", ""), _day(query.get("date")))}

        @r("POST", "/api/reports")
        def save_report(conn, query, body):
            return {"saved": ai_service.save_report(conn, body)}

        @r("POST", "/api/usage")
        def usage_event(conn, query, body):
            usage.record_client(conn, body)
            return {"ok": True}

        @r("GET", "/api/usage/summary")
        def usage_summary(conn, query, body):
            days = query.get("days", "30")
            if not days.isdigit() or not 1 <= int(days) <= 365:
                raise ApiError(400, "days 값이 올바르지 않습니다")
            return usage.summary(conn, int(days))

        @r("GET", "/api/folders")
        def folders(conn, query, body):
            return snapshots.list_folders(conn)

        @r("POST", "/api/folders")
        def add_folder(conn, query, body):
            return snapshots.add_folder(conn, body, self.repos_dir)

        @r("PATCH", r"/api/folders/(\d+)")
        def update_folder(conn, query, body, fid):
            return snapshots.update_folder(conn, int(fid), body)

        @r("DELETE", r"/api/folders/(\d+)")
        def remove_folder(conn, query, body, fid):
            snapshots.remove_folder(conn, int(fid), self.repos_dir)
            return {"ok": True}

        @r("GET", r"/api/folders/(\d+)/areas")
        def folder_areas(conn, query, body, fid):
            return snapshots.list_areas(conn, int(fid), query.get("under", ""))

        @r("POST", r"/api/folders/(\d+)/areas")
        def set_folder_area(conn, query, body, fid):
            return snapshots.set_area(conn, int(fid), body)

        @r("DELETE", r"/api/folders/(\d+)/areas")
        def remove_folder_area(conn, query, body, fid):
            return snapshots.remove_area(conn, int(fid), body)

        @r("POST", "/api/folders/scan")
        def scan_folders(conn, query, body):
            if self.snapshotter is None:
                raise ApiError(409, "데모 모드에서는 폴더를 기록하지 않습니다")
            conn.commit()
            return [asdict(r) for r in self.snapshotter.run_now()]

        @r("GET", r"/api/changes/(\d+)")
        def change(conn, query, body, cid):
            return service.get_change(conn, int(cid))

        @r("GET", r"/api/change-files/(\d+)/diff")
        def change_file_diff(conn, query, body, fid):
            return snapshots.file_diff(self.repos_dir, conn, int(fid))

        @r("GET", "/api/exclusions")
        def exclusions(conn, query, body):
            return service.list_exclusions(conn)

        @r("POST", "/api/exclusions")
        def add_exclusion(conn, query, body):
            return service.add_exclusion(conn, body)

        @r("DELETE", r"/api/exclusions/(\d+)")
        def delete_exclusion(conn, query, body, eid):
            service.delete_exclusion(conn, int(eid))
            return {"ok": True}

        @r("POST", "/api/pause")
        def pause(conn, query, body):
            service.pause(conn)
            return {"paused": True}

        @r("POST", "/api/resume")
        def resume(conn, query, body):
            service.resume(conn)
            return {"paused": False}

        @r("POST", "/api/delete-data")
        def delete_data(conn, query, body):
            return service.delete_data(conn, body, repos_dir=self.repos_dir)

    def dispatch(self, method: str, path: str, query: dict, body: dict) -> Any:
        for m, pattern, fn in self.routes:
            match = pattern.match(path)
            if match and m == method:
                try:
                    with self.db.connect() as conn:
                        return fn(conn, query, body, *match.groups())
                except service.Invalid as e:
                    raise ApiError(400, str(e)) from None
                except service.NotFound as e:
                    raise ApiError(404, str(e)) from None
                except AIError as e:
                    raise ApiError(AI_ERROR_STATUS.get(e.kind, 502), e.message, e.kind) from None
        if any(p.match(path) for _, p, _ in self.routes):
            raise ApiError(405, "허용되지 않는 메서드입니다")
        raise ApiError(404, "없는 API입니다")


def _day(value: Optional[str]) -> date:
    return service.parse_day(value) if value else date.today()


def make_handler(app: App, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}

    class Handler(BaseHTTPRequestHandler):
        server_version = "WorkHistory"

        def log_message(self, fmt, *args):  # URLs may hold search terms; keep them out of logs
            pass

        def _send(self, status: int, payload: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(payload)

        def _from_other_site(self) -> bool:
            """A browser request started by another site (browsers set these headers; other clients don't)."""
            origin = self.headers.get("Origin")
            if origin is not None and origin not in allowed_origins:
                return True
            return self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site")

        def _json(self, status: int, data: Any) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _handle(self, method: str) -> None:
            if self.headers.get("Host") not in allowed_hosts:
                self._json(403, {"error": "허용되지 않는 호스트입니다"})
                return
            url = urlparse(self.path)
            if url.path.startswith("/api/") and self._from_other_site():
                # Even a GET can have side effects (session regrouping, the Claude Code status check
                # starts processes), so no other web page may call the API, not even "blind".
                self._json(403, {"error": "요청이 거부되었습니다"})
                return
            if not url.path.startswith("/api/"):
                if method == "GET":
                    self._static(url.path)
                else:
                    self._json(405, {"error": "허용되지 않는 메서드입니다"})
                return
            try:
                body: dict = {}
                if method != "GET":
                    # Read (bounded) before deciding, so a rejected client gets a clean 403
                    # instead of a connection reset from the unread request body.
                    raw = self._read_raw_body()
                    # Custom header + JSON content type force a CORS pre-flight that
                    # we never approve, so other web pages cannot call write APIs.
                    if self.headers.get(WRITE_HEADER) != "1" or not (
                        self.headers.get("Content-Type", "").startswith("application/json")
                    ):
                        raise ApiError(403, "요청이 거부되었습니다")
                    body = self._parse_body(raw)
                query = {k: v[-1] for k, v in parse_qs(url.query).items()}
                started = time.perf_counter()
                result = app.dispatch(method, url.path, query, body)
                log.debug("%s %s %.0fms", method, url.path, (time.perf_counter() - started) * 1000)
                self._json(200, result)
            except ApiError as e:
                self._json(e.status, {"error": e.message, "kind": e.kind})
            except Exception:
                log.exception("unhandled error in %s %s", method, url.path)
                self._json(500, {"error": "서버 내부 오류가 발생했습니다"})

        def _read_raw_body(self) -> bytes:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ApiError(400, "Content-Length가 올바르지 않습니다") from None
            if length > MAX_BODY:
                self.close_connection = True  # do not read megabytes we will reject anyway
                raise ApiError(413, "요청이 너무 큽니다")
            return self.rfile.read(length) if length > 0 else b""

        @staticmethod
        def _parse_body(raw: bytes) -> dict:
            raw = raw or b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ApiError(400, "JSON 형식이 올바르지 않습니다") from None
            if not isinstance(data, dict):
                raise ApiError(400, "JSON 객체가 필요합니다")
            return data

        def _static(self, path: str) -> None:
            name = "index.html" if path in ("", "/") else path.lstrip("/")
            target = (STATIC_DIR / name).resolve()
            if STATIC_DIR.resolve() not in target.parents or not target.is_file():
                self._json(404, {"error": "찾을 수 없습니다"})
                return
            # Windows' registry can map .js to text/plain, so pin the types we serve.
            ctype = CONTENT_TYPES.get(target.suffix) or (
                mimetypes.guess_type(target.name)[0] or "application/octet-stream")
            self._send(200, target.read_bytes(), ctype)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PATCH(self):
            self._handle("PATCH")

        def do_DELETE(self):
            self._handle("DELETE")

    return Handler


class _ExclusiveServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets another process bind the same port (a second
    # instance or a squatter). Demand exclusive use instead.
    allow_reuse_address = False

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def snapshots_dir(db: Database) -> Path:
    """Snapshot repositories live next to their database (demo and real never mix)."""
    p = Path(db.path)
    return p.with_name(p.stem + "-snapshots")


def create_server(app: App, port: int) -> ThreadingHTTPServer:
    """Bind 127.0.0.1:port exclusively (0 = any free port); OSError if taken."""
    server = _ExclusiveServer(("127.0.0.1", port), BaseHTTPRequestHandler)
    server.RequestHandlerClass = make_handler(app, server.server_address[1])
    return server


def find_running_instance(port: int, timeout: float = 2) -> bool:
    """True if this app (not some other program) is already serving on the port."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/status", timeout=timeout) as resp:
            data = json.load(resp)
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and "db_path" in data and "paused" in data
