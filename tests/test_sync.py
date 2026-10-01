import time
import unittest

from workhistory import service
from workhistory.awclient import AWClient
from workhistory.config import SYNC_CHUNK_S
from workhistory.sync import Syncer, sync_once

from tests.helpers import FakeAW, TempDB

WIN = "aw-watcher-window_devbox"
AFK = "aw-watcher-afk_devbox"


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.aw = FakeAW()
        self.aw.add_bucket(WIN, "currentwindow")
        self.aw.add_bucket(AFK, "afkstatus")
        self.client = AWClient(self.aw.url, timeout=5)
        self.now = time.time()
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)
        self.aw.close()

    def rows(self, table="window_events"):
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY start_ts")]

    def test_initial_import(self):
        t = self.now - 3600
        self.aw.add_event(WIN, t, t + 600, {"app": "Code.exe", "title": "main.py"})
        self.aw.add_event(AFK, t, t + 600, {"status": "not-afk"})
        result = sync_once(self.db, self.client, now=self.now)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.imported, {"window": 1, "afk": 1})
        self.assertEqual(result.buckets_found, {"window": True, "afk": True})
        [row] = self.rows()
        self.assertEqual((row["app"], row["title"]), ("Code.exe", "main.py"))
        self.assertAlmostEqual(row["end_ts"] - row["start_ts"], 600, places=3)

    def test_resync_is_idempotent(self):
        t = self.now - 3600
        self.aw.add_event(WIN, t, t + 600, {"app": "a", "title": "b"})
        sync_once(self.db, self.client, now=self.now)
        sync_once(self.db, self.client, now=self.now + 5)
        sync_once(self.db, self.client, now=self.now + 10)
        self.assertEqual(len(self.rows()), 1)

    def test_heartbeat_extension_updates_end(self):
        t = self.now - 300
        eid = self.aw.add_event(WIN, t, t + 100, {"app": "a", "title": "b"})
        sync_once(self.db, self.client, now=self.now)
        self.aw.extend(WIN, eid, t + 400)
        sync_once(self.db, self.client, now=self.now + 120)
        [row] = self.rows()
        self.assertAlmostEqual(row["end_ts"], t + 400, places=3)
        self.assertAlmostEqual(row["start_ts"], t, places=3)

    def test_trimmed_events_never_shrink_stored_range(self):
        # An event spanning a chunk boundary comes back trimmed in two chunks.
        start = self.now - 3 * SYNC_CHUNK_S
        self.aw.add_event(WIN, start + SYNC_CHUNK_S - 1000, start + SYNC_CHUNK_S + 1000, {"app": "a", "title": "b"})
        sync_once(self.db, self.client, now=self.now, backfill_days=3)
        [row] = self.rows()
        self.assertAlmostEqual(row["end_ts"] - row["start_ts"], 2000, places=3)

    def test_long_running_event_before_cursor_still_extended(self):
        t = self.now - 7200  # started well before the overlap window
        eid = self.aw.add_event(WIN, t, self.now, {"app": "a", "title": "b"})
        sync_once(self.db, self.client, now=self.now)
        self.aw.extend(WIN, eid, self.now + 3600)
        sync_once(self.db, self.client, now=self.now + 3600)
        [row] = self.rows()
        self.assertAlmostEqual(row["start_ts"], t, places=3)
        self.assertAlmostEqual(row["end_ts"], self.now + 3600, places=3)

    def test_other_hosts_ignored(self):
        self.aw.add_bucket("aw-watcher-window_laptop", "currentwindow", hostname="laptop")
        self.aw.add_event("aw-watcher-window_laptop", self.now - 100, self.now - 50, {"app": "x", "title": "y"})
        sync_once(self.db, self.client, now=self.now)
        self.assertEqual(self.rows(), [])

    def test_exclusions_skip_at_ingest(self):
        with self.db.connect() as conn:
            service.add_exclusion(conn, {"field": "title", "pattern": "뱅킹"})
        self.aw.add_event(WIN, self.now - 100, self.now - 50, {"app": "chrome.exe", "title": "KB 인터넷뱅킹"})
        self.aw.add_event(WIN, self.now - 50, self.now - 10, {"app": "chrome.exe", "title": "GitHub"})
        result = sync_once(self.db, self.client, now=self.now)
        self.assertEqual(result.skipped_private, 1)
        self.assertEqual([r["title"] for r in self.rows()], ["GitHub"])

    def test_events_starting_during_pause_dropped(self):
        with self.db.connect() as conn:
            service.pause(conn, now=self.now - 200)
        self.aw.add_event(WIN, self.now - 300, self.now - 250, {"app": "a", "title": "before"})
        self.aw.add_event(WIN, self.now - 150, self.now - 100, {"app": "a", "title": "during"})
        sync_once(self.db, self.client, now=self.now)
        self.assertEqual([r["title"] for r in self.rows()], ["before"])
        with self.db.connect() as conn:
            service.resume(conn, now=self.now - 50)
        self.aw.add_event(WIN, self.now - 40, self.now - 20, {"app": "a", "title": "after"})
        sync_once(self.db, self.client, now=self.now + 1)
        self.assertEqual([r["title"] for r in self.rows()], ["before", "after"])

    def test_unreachable_aw_keeps_data(self):
        self.aw.add_event(WIN, self.now - 100, self.now - 50, {"app": "a", "title": "b"})
        sync_once(self.db, self.client, now=self.now)
        self.aw.fail = True
        result = sync_once(self.db, self.client, now=self.now + 60)
        self.assertFalse(result.ok)
        self.assertIn("HTTP 500", result.error)
        self.assertEqual(len(self.rows()), 1)

    def test_connection_refused(self):
        result = sync_once(self.db, AWClient("http://127.0.0.1:9", timeout=2), now=self.now)
        self.assertFalse(result.ok)
        self.assertIn("unreachable", result.error)

    def test_recreated_bucket_is_new_source(self):
        self.aw.add_event(WIN, self.now - 100, self.now - 50, {"app": "a", "title": "old"})
        sync_once(self.db, self.client, now=self.now)
        # AW reinstalled: same bucket id, new created timestamp, ids restart at 1.
        self.aw.add_bucket(WIN, "currentwindow", created="2026-09-30T00:00:00+00:00")
        self.aw.add_event(WIN, self.now - 40, self.now - 30, {"app": "a", "title": "new"})
        sync_once(self.db, self.client, now=self.now + 1)
        self.assertEqual([r["title"] for r in self.rows()], ["old", "new"])

    def test_malformed_events_skipped(self):
        self.aw.events[WIN].append({"id": "x", "start": self.now - 10, "end": self.now, "data": {}})
        self.aw.add_event(AFK, self.now - 10, self.now, {"status": "weird"})
        result = sync_once(self.db, self.client, now=self.now)
        self.assertTrue(result.ok)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.rows("afk_events"), [])

    def test_sync_marks_days_and_syncer_builds_sessions(self):
        self.aw.add_event(WIN, self.now - 1200, self.now - 600, {"app": "Code.exe", "title": "main.py"})
        sync_once(self.db, self.client, now=self.now)
        with self.db.connect() as conn:
            self.assertEqual([r["version"] for r in conn.execute("SELECT version FROM session_days")], [-1])
        Syncer(self.db, self.client).run_now()
        with self.db.connect() as conn:
            rows = conn.execute("SELECT title FROM sessions").fetchall()
            self.assertEqual([r["title"] for r in rows], ["VS Code · main.py"])
            self.assertEqual(conn.execute("SELECT version FROM session_days").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
