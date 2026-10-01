import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from datetime import date, timedelta
from urllib.parse import quote

from workhistory.awclient import AWClient
from workhistory.claude_link import ClaudeLink
from workhistory.server import App, create_server, find_running_instance
from workhistory.sync import Syncer

from tests.helpers import FakeAW, TempDB, local_ts
from tests.test_claude_link import FakeClaude


class ServerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        self.aw = FakeAW()
        self.aw.add_bucket("aw-watcher-window_devbox", "currentwindow")
        self.claude = FakeClaude()  # never the real Claude Code CLI
        link = ClaudeLink(cli="claude", runner=self.claude, python="/opt/app/python")
        self.server = create_server(App(self.db, Syncer(self.db, AWClient(self.aw.url)), claude_link=link), 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.aw.close()
        self._tmp.__exit__(None, None, None)

    def request(self, method, path, body=None, host=None, write_header=True, ctype="application/json"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = ctype
            if write_header:
                headers["X-Work-History"] = "1"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        ct = resp.getheader("Content-Type", "")
        return resp.status, (json.loads(raw) if ct.startswith("application/json") else raw), ct

    def test_static_index(self):
        status, body, ct = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertTrue(ct.startswith("text/html"))
        self.assertIn("작업 기록".encode(), body)
        status, _, ct = self.request("GET", "/app.js")
        self.assertEqual((status, ct.split(";")[0]), (200, "text/javascript"))

    def test_path_traversal_blocked(self):
        status, _, _ = self.request("GET", "/../server.py")
        self.assertEqual(status, 404)
        status, _, _ = self.request("GET", "/%2e%2e/server.py")
        self.assertEqual(status, 404)

    def test_foreign_host_rejected(self):
        status, _, _ = self.request("GET", "/api/status", host="evil.example:80")
        self.assertEqual(status, 403)

    def test_write_requires_custom_header_and_json(self):
        status, _, _ = self.request("POST", "/api/projects", {"name": "x"}, write_header=False)
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/projects", {"name": "x"}, ctype="text/plain")
        self.assertEqual(status, 403)

    def test_end_to_end_flow(self):
        status, project, _ = self.request("POST", "/api/projects", {"name": "Gov24 백엔드"})
        self.assertEqual(status, 200)
        status, sync, _ = self.request("POST", "/api/sync", {})
        self.assertTrue(sync["ok"], sync)
        status, t, _ = self.request("GET", f"/api/timeline?date={date.today().isoformat()}")
        self.assertEqual(status, 200)
        status, note, _ = self.request("POST", "/api/notes", {"day": date.today().isoformat(), "body": "테스트 메모", "project_id": project["id"]})
        self.assertEqual(status, 200)
        status, r, _ = self.request("GET", "/api/search?q=" + quote("테스트"))
        self.assertEqual(status, 200)
        self.assertEqual([n["id"] for n in r["notes"]], [note["id"]])

    def test_session_review_resume_endpoints(self):
        yesterday = date.today() - timedelta(days=1)
        today = yesterday.isoformat()  # a full past day keeps the test independent of the clock
        start = local_ts(yesterday, 10)
        self.aw.add_event("aw-watcher-window_devbox", start, start + 900, {"app": "Code.exe", "title": "a.py"})
        self.aw.add_event("aw-watcher-window_devbox", start + 1800, start + 2400, {"app": "Code.exe", "title": "b.py"})
        self.request("POST", "/api/sync", {})
        status, t, _ = self.request("GET", f"/api/timeline?date={today}")
        self.assertEqual(len(t["sessions"]), 2)
        a, b = t["sessions"]
        status, s, _ = self.request("PATCH", f"/api/sessions/{a['id']}", {"title": "리팩터링", "note": "다음: 테스트"})
        self.assertEqual((status, s["title"], s["note"]), (200, "리팩터링", "다음: 테스트"))
        status, m, _ = self.request("POST", "/api/sessions/merge", {"ids": [a["id"], b["id"]]})
        self.assertEqual((status, m["title"], m["locked"]), (200, "리팩터링", 1))
        status, parts, _ = self.request("POST", f"/api/sessions/{m['id']}/split", {"at": start + 1000})
        self.assertEqual((status, len(parts)), (200, 2))
        status, r, _ = self.request("GET", f"/api/review?date={today}")
        self.assertIn("리팩터링", r["generated"])
        status, saved, _ = self.request("POST", "/api/review", {"day": today, "body": "직접 쓴 리뷰"})
        self.assertEqual(saved["saved"]["body"], "직접 쓴 리뷰")
        status, project, _ = self.request("POST", "/api/projects", {"name": "P"})
        status, overview, _ = self.request("GET", "/api/resume")
        self.assertEqual([p["name"] for p in overview], ["P"])
        status, detail, _ = self.request("GET", f"/api/resume/{project['id']}")
        self.assertEqual((status, detail["last_session"]), (200, None))
        self.assertEqual(self.request("PATCH", "/api/sessions/99999", {"note": "x"})[0], 404)

    def test_port_is_exclusive(self):
        # Windows' SO_REUSEADDR would otherwise let a second instance share the port.
        with self.assertRaises(OSError):
            create_server(App(self.db, None), self.port)

    def test_detects_running_instance(self):
        self.assertTrue(find_running_instance(self.port))
        other = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)  # not our app
        try:
            self.assertFalse(find_running_instance(other.server_address[1], timeout=1))
        finally:
            other.server_close()

    def test_claude_code_switch(self):
        status, body, _ = self.request("GET", "/api/mcp")
        self.assertEqual((status, body["enabled"], body["claude"]["registered"]), (200, False, False))
        status, body, _ = self.request("POST", "/api/mcp", {"enabled": True})
        self.assertEqual((status, body["enabled"], body["claude"]["ours"]), (200, True, True))
        self.assertEqual(self.claude.servers["workhistory"], ["/opt/app/python", "-m", "workhistory.mcp_server"])
        status, body, _ = self.request("POST", "/api/mcp", {"enabled": False})
        self.assertEqual((status, body["enabled"]), (200, False))
        self.assertEqual(self.claude.servers, {})

    def test_errors_are_json(self):
        self.assertEqual(self.request("GET", "/api/nope")[0], 404)
        self.assertEqual(self.request("DELETE", "/api/status", {})[0], 405)
        status, body, _ = self.request("GET", "/api/timeline?date=bad")
        self.assertEqual(status, 400)
        self.assertIn("error", body)
        self.assertEqual(self.request("GET", "/api/timeline?project=abc")[0], 400)
        self.assertEqual(self.request("DELETE", "/api/projects/999", {})[0], 404)


if __name__ == "__main__":
    unittest.main()
