import sys
import time
import unittest

from workhistory import service
from workhistory.collector import MAX_GAP_S, Collector, WindowsProbe
from workhistory.config import AFK_AFTER_S, RAW_RETENTION_S
from workhistory.sync import recent_apps, sync_once

from tests.helpers import TempDB


class FakeProbe:
    def __init__(self):
        self.app, self.title, self.idle = "Code.exe", "main.py", 0.0

    def foreground(self):
        return self.app, self.title

    def idle_seconds(self):
        return self.idle


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        self.probe = FakeProbe()
        self.c = Collector(self.db, self.probe, flush_s=0)
        self.t = time.time() - 3600

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def run_ticks(self, start, end, step=2):
        t = start
        while t <= end:
            self.c.tick(self.t + t)
            t += step

    def raw(self, kind="window"):
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM collector_events WHERE kind = ? ORDER BY start_ts", (kind,))]

    def test_heartbeats_merge_and_window_changes_split(self):
        self.run_ticks(0, 60)
        self.probe.app, self.probe.title = "chrome.exe", "Jira"
        self.run_ticks(62, 100)
        rows = self.raw()
        self.assertEqual([(r["app"], r["title"]) for r in rows], [("Code.exe", "main.py"), ("chrome.exe", "Jira")])
        self.assertAlmostEqual(rows[0]["end_ts"] - rows[0]["start_ts"], 60)
        self.assertAlmostEqual(rows[1]["end_ts"], self.t + 100)
        self.assertEqual([r["status"] for r in self.raw("afk")], ["not-afk"])

    def test_gap_starts_new_event(self):
        self.run_ticks(0, 10)
        self.run_ticks(10 + MAX_GAP_S + 5, 60)  # e.g. the PC slept
        self.assertEqual(len(self.raw()), 2)

    def test_away_starts_at_last_input(self):
        self.run_ticks(0, 100)
        for t in range(102, 400, 2):  # no input since t+100
            self.probe.idle = t - 100
            self.c.tick(self.t + t)
        self.probe.idle = 1
        self.c.tick(self.t + 400)
        afk = self.raw("afk")
        self.assertEqual([r["status"] for r in afk], ["not-afk", "afk", "not-afk"])
        self.assertAlmostEqual(afk[0]["end_ts"], self.t + 100)
        self.assertAlmostEqual(afk[1]["start_ts"], self.t + 100)
        self.assertAlmostEqual(afk[1]["end_ts"], self.t + 399)
        self.assertAlmostEqual(afk[2]["start_ts"], self.t + 399)

    def test_nothing_recorded_while_paused_or_excluded(self):
        self.run_ticks(0, 10)
        with self.db.connect() as conn:
            service.pause(conn, now=self.t + 11)
        self.run_ticks(12, 40)
        with self.db.connect() as conn:
            service.resume(conn, now=self.t + 41)
            service.add_exclusion(conn, {"field": "title", "pattern": "secret"})
        self.probe.title = "secret plan"
        self.run_ticks(42, 60)
        self.probe.title = "main.py"
        self.run_ticks(62, 70)
        rows = self.raw()
        self.assertEqual([r["title"] for r in rows], ["main.py", "main.py"])
        self.assertAlmostEqual(rows[0]["end_ts"], self.t + 10)
        self.assertAlmostEqual(rows[1]["start_ts"], self.t + 62)

    def test_new_exclusion_and_delete_purge_raw_events(self):
        self.run_ticks(0, 10)
        self.probe.title = "secret plan"
        self.run_ticks(12, 20)
        with self.db.connect() as conn:
            service.add_exclusion(conn, {"field": "title", "pattern": "secret"})
        self.assertEqual([r["title"] for r in self.raw()], ["main.py"])
        with self.db.connect() as conn:
            service.delete_data(conn, {"all": True})
        self.assertEqual(self.raw() + self.raw("afk"), [])

    def test_old_raw_events_are_pruned(self):
        self.c.tick(self.t - RAW_RETENTION_S - 100)
        self.c._last_prune = 0
        self.c.tick(self.t)
        self.assertEqual(len(self.raw()), 1)

    def test_sync_imports_raw_events(self):
        self.run_ticks(0, 60)
        self.probe.app, self.probe.title = "Slack.exe", "general"
        self.run_ticks(62, 120)
        with self.db.connect() as conn:
            service.add_track_rule(conn, {"app": "code"})
            service.set_track_mode(conn, {"mode": "selected"})
        r = sync_once(self.db, self.c, now=self.t + 130)
        self.assertTrue(r.ok)
        with self.db.connect() as conn:
            rows = [(x["app"], x["title"]) for x in conn.execute("SELECT app, title FROM window_events ORDER BY start_ts")]
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM afk_events").fetchone()[0], 1)
        self.assertEqual(rows, [("Code.exe", "main.py"), ("", "")])  # 업무 외: no app, no title
        # The app list still sees untracked apps (from the raw events).
        with self.db.connect() as conn:
            apps = {a["app"]: a["tracked"] for a in recent_apps(conn, [], now=self.t + 130)}
        self.assertEqual(apps, {"Code.exe": False, "Slack.exe": False})
        # A growing event is extended by the next sync, not duplicated.
        self.run_ticks(122, 200)
        sync_once(self.db, self.c, now=self.t + 210)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM window_events").fetchone()[0], 2)
            end = conn.execute("SELECT MAX(end_ts) FROM window_events").fetchone()[0]
        self.assertAlmostEqual(end, self.t + 200)


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class WindowsProbeTests(unittest.TestCase):
    def test_reads_foreground_and_idle(self):
        probe = WindowsProbe()
        app, title = probe.foreground()
        self.assertIsInstance(app, str)
        self.assertTrue(app)
        self.assertIsInstance(title, str)
        self.assertGreaterEqual(probe.idle_seconds(), 0)


if __name__ == "__main__":
    unittest.main()
