import time
import unittest

from workhistory import service
from workhistory.sync import Syncer, sync_once

from tests.helpers import TempDB, add_raw, extend_raw


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def rows(self, table="window_events"):
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY start_ts")]

    def test_initial_import(self):
        t = self.now - 3600
        add_raw(self.db, "window", t, t + 600, "Code.exe", "main.py")
        add_raw(self.db, "afk", t, t + 600, status="not-afk")
        result = sync_once(self.db, now=self.now)
        self.assertTrue(result.ok)
        self.assertEqual(result.imported, {"window": 1, "afk": 1})
        [row] = self.rows()
        self.assertEqual((row["app"], row["title"]), ("Code.exe", "main.py"))
        self.assertAlmostEqual(row["end_ts"] - row["start_ts"], 600)

    def test_resync_is_idempotent(self):
        t = self.now - 3600
        add_raw(self.db, "window", t, t + 600, "a", "b")
        sync_once(self.db, now=self.now)
        sync_once(self.db, now=self.now + 5)
        sync_once(self.db, now=self.now + 10)
        self.assertEqual(len(self.rows()), 1)

    def test_open_event_growth_updates_end(self):
        t = self.now - 300
        raw = add_raw(self.db, "window", t, t + 100, "a", "b")
        sync_once(self.db, now=self.now)
        extend_raw(self.db, raw, t + 400)
        sync_once(self.db, now=self.now + 120)
        [row] = self.rows()
        self.assertAlmostEqual(row["end_ts"], t + 400)
        self.assertAlmostEqual(row["start_ts"], t)

    def test_long_running_event_before_cursor_still_extended(self):
        t = self.now - 7200  # started well before the overlap window
        raw = add_raw(self.db, "window", t, self.now, "a", "b")
        sync_once(self.db, now=self.now)
        extend_raw(self.db, raw, self.now + 3600)
        sync_once(self.db, now=self.now + 3600)
        [row] = self.rows()
        self.assertAlmostEqual(row["start_ts"], t)
        self.assertAlmostEqual(row["end_ts"], self.now + 3600)

    def test_exclusions_skip_at_import(self):
        add_raw(self.db, "window", self.now - 100, self.now - 50, "chrome.exe", "KB 인터넷뱅킹")
        add_raw(self.db, "window", self.now - 50, self.now - 10, "chrome.exe", "GitHub")
        with self.db.connect() as conn:  # added after recording: also purges the raw event
            service.add_exclusion(conn, {"field": "title", "pattern": "뱅킹"})
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM collector_events").fetchone()[0], 1)
        add_raw(self.db, "window", self.now - 9, self.now - 5, "chrome.exe", "KB 인터넷뱅킹")
        result = sync_once(self.db, now=self.now)
        self.assertEqual(result.skipped_private, 1)
        self.assertEqual([r["title"] for r in self.rows()], ["GitHub"])

    def test_events_starting_during_pause_dropped(self):
        with self.db.connect() as conn:
            service.pause(conn, now=self.now - 200)
        add_raw(self.db, "window", self.now - 300, self.now - 250, "a", "before")
        add_raw(self.db, "window", self.now - 150, self.now - 100, "a", "during")
        sync_once(self.db, now=self.now)
        self.assertEqual([r["title"] for r in self.rows()], ["before"])
        with self.db.connect() as conn:
            service.resume(conn, now=self.now - 50)
        add_raw(self.db, "window", self.now - 40, self.now - 20, "a", "after")
        sync_once(self.db, now=self.now + 1)
        self.assertEqual([r["title"] for r in self.rows()], ["before", "after"])

    def test_unknown_status_skipped(self):
        add_raw(self.db, "afk", self.now - 10, self.now, status="weird")
        result = sync_once(self.db, now=self.now)
        self.assertTrue(result.ok)
        self.assertEqual(self.rows("afk_events"), [])

    def test_sync_marks_days_and_syncer_builds_sessions(self):
        add_raw(self.db, "window", self.now - 1200, self.now - 600, "Code.exe", "main.py")
        sync_once(self.db, now=self.now)
        with self.db.connect() as conn:
            self.assertEqual([r["version"] for r in conn.execute("SELECT version FROM session_days")], [-1])
        Syncer(self.db).run_now()
        with self.db.connect() as conn:
            rows = conn.execute("SELECT title FROM sessions").fetchall()
            self.assertEqual([r["title"] for r in rows], ["VS Code · main.py"])
            self.assertEqual(conn.execute("SELECT version FROM session_days").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
