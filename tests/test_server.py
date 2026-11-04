import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import subprocess
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

from workhistory.autostart import Launch
from workhistory.server import App, create_server, find_running_instance
from workhistory.sync import Syncer

from tests.helpers import TempDB, add_raw, local_ts


def _fake_shortcut(env):
    Path(env["WH_LNK"]).write_text("lnk")
    return subprocess.CompletedProcess([], 0, "", "")


class ServerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        # Never the real Startup folder / desktop either.
        self.home = Path(self.db.path).parent / "home"
        launch = Launch([r"C:\app\작업 기록.cmd"], lambda name: self.home / name, _fake_shortcut)
        self.app = App(self.db, Syncer(self.db), launch=launch)
        self.server = create_server(self.app, 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._tmp.__exit__(None, None, None)

    def request(self, method, path, body=None, host=None, write_header=True, ctype="application/json",
                extra_headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{self.port}", **(extra_headers or {})}
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

    def test_save_report(self):
        day = date.today().isoformat()
        status, body, _ = self.request("POST", "/api/reports", {"kind": "daily", "day": day, "body": "a\nb"})
        self.assertEqual((status, body["saved"]["body"]), (200, "a\nb"))

    def test_status_last_day_only_in_demo(self):
        with self.db.connect() as conn:
            conn.execute("INSERT INTO sessions (day, start_ts, end_ts, created_at, updated_at) "
                         "VALUES ('2026-11-04', 0, 1, 0, 0)")
        self.assertIsNone(self.request("GET", "/api/status")[1]["last_day"])
        self.app.demo = True
        self.assertEqual(self.request("GET", "/api/status")[1]["last_day"], "2026-11-04")

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

    def test_other_sites_cannot_call_the_api_even_with_get(self):
        for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
                        {"Origin": "https://evil.example"}):
            status, _, _ = self.request("GET", "/api/status", extra_headers=headers)
            self.assertEqual(status, 403, headers)
        for headers in ({"Sec-Fetch-Site": "same-origin"}, {"Sec-Fetch-Site": "none"},
                        {"Origin": f"http://127.0.0.1:{self.port}", "Sec-Fetch-Site": "same-origin"}):
            status, _, _ = self.request("GET", "/api/status", extra_headers=headers)
            self.assertEqual(status, 200, headers)
        status, _, _ = self.request("GET", "/", extra_headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 200)  # a link to the app itself still opens it

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

    def test_session_endpoints(self):
        yesterday = date.today() - timedelta(days=1)
        today = yesterday.isoformat()  # a full past day keeps the test independent of the clock
        start = local_ts(yesterday, 10)
        add_raw(self.db, "window", start, start + 900, "Code.exe", "a.py")
        add_raw(self.db, "window", start + 1800, start + 2400, "Code.exe", "b.py")
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
        self.assertEqual(self.request("GET", f"/api/review?date={today}")[0], 404)  # tab removed
        self.assertEqual(self.request("GET", "/api/resume")[0], 405)  # tab removed; POST /api/resume = 기록 재개
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

    def test_no_claude_code_registration_api(self):
        self.assertEqual(self.request("GET", "/api/mcp")[0], 404)  # Claude Code is only an AI model choice now

    def test_launch_settings(self):
        status, body, _ = self.request("GET", "/api/launch")
        self.assertEqual((status, body["autostart"]["enabled"]), (200, False))
        status, body, _ = self.request("POST", "/api/launch", {"autostart": True})
        self.assertEqual((status, body["autostart"]["enabled"]), (200, True))
        self.assertTrue((self.home / "startup" / "작업 기록.lnk").exists())
        self.assertEqual(self.request("POST", "/api/launch", {"autostart": "yes"})[0], 400)
        status, body, _ = self.request("POST", "/api/launch/shortcut", {})
        self.assertEqual((status, body["shortcut"]["exists"]), (200, True))

    def test_quit(self):
        # Without a quit hook (tests, embedding) the app can't be stopped from the page.
        self.assertFalse(self.request("GET", "/api/launch")[1]["can_quit"])
        self.assertEqual(self.request("POST", "/api/quit", {})[0], 409)
        quit_event = threading.Event()
        self.app.on_quit = quit_event.set
        self.assertTrue(self.request("GET", "/api/launch")[1]["can_quit"])
        status, body, _ = self.request("POST", "/api/quit", {})
        self.assertEqual((status, body), (200, {"quitting": True}))
        self.assertTrue(quit_event.wait(5))  # fires after the answer went out

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
