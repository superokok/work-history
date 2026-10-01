"""Shared test utilities: temp databases and a fake ActivityWatch server."""

import json
import shutil
import tempfile
import threading
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from workhistory.awclient import parse_iso
from workhistory.db import Database


class TempDB:
    def __enter__(self) -> Database:
        self.dir = tempfile.mkdtemp(prefix="wh-test-")
        self.db = Database(Path(self.dir) / "test.db")
        self.db.migrate()
        return self.db

    def __exit__(self, *exc):
        shutil.rmtree(self.dir, ignore_errors=True)


def local_ts(day: date, hour: int, minute: int = 0, second: int = 0) -> float:
    return datetime(day.year, day.month, day.day, hour, minute, second).timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


class FakeAW:
    """In-process HTTP server mimicking ActivityWatch's api/0 endpoints.

    Range queries return events overlapping [start, end], trimmed to the range,
    newest first — the same behaviour as aw-server and aw-server-rust.
    """

    def __init__(self, hostname: str = "devbox"):
        self.hostname = hostname
        self.buckets: dict[str, dict] = {}
        self.events: dict[str, list[dict]] = {}
        self.fail = False
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if fake.fail:
                    self.send_response(500)
                    self.end_headers()
                    return
                url = urlparse(self.path)
                parts = url.path.split("/")
                if url.path == "/api/0/info":
                    body = {"hostname": fake.hostname, "version": "v0.13.2-fake"}
                elif url.path == "/api/0/buckets/":
                    body = fake.buckets
                elif len(parts) == 6 and parts[5] == "events":
                    q = {k: v[-1] for k, v in parse_qs(url.query).items()}
                    body = fake.query(unquote(parts[4]), parse_iso(q["start"]), parse_iso(q["end"]))
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                payload = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def add_bucket(self, bucket_id: str, type_: str, hostname: str = None, created: str = "2026-01-01T00:00:00+00:00"):
        self.buckets[bucket_id] = {"id": bucket_id, "type": type_, "hostname": hostname or self.hostname,
                                   "created": created, "client": "fake"}
        self.events[bucket_id] = []

    def add_event(self, bucket_id: str, start: float, end: float, data: dict) -> int:
        events = self.events[bucket_id]
        event_id = len(events) + 1
        events.append({"id": event_id, "start": start, "end": end, "data": data})
        return event_id

    def extend(self, bucket_id: str, event_id: int, new_end: float):
        """Heartbeat: same event id, later end."""
        for e in self.events[bucket_id]:
            if e["id"] == event_id:
                e["end"] = new_end

    def query(self, bucket_id: str, start: float, end: float) -> list[dict]:
        out = []
        for e in self.events.get(bucket_id, []):
            if e["end"] >= start and e["start"] <= end:
                s, en = max(e["start"], start), min(e["end"], end)
                out.append({"id": e["id"], "timestamp": iso(s), "duration": en - s, "data": e["data"]})
        out.sort(key=lambda e: e["timestamp"], reverse=True)
        return out
