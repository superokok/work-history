import contextlib
import importlib.util
import io
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from workhistory import demo
from workhistory.db import Database

from tests.helpers import TempDB, local_ts

HAS_DULWICH = importlib.util.find_spec("dulwich") is not None
THURSDAY = date(2026, 11, 5)


def day_range(conn, table, day):
    lo, hi = local_ts(day, 0), local_ts(day, 23, 59)
    return conn.execute(f"SELECT MIN(start_ts), MAX(end_ts) FROM {table} WHERE start_ts >= ? AND start_ts < ?",
                        (lo, hi)).fetchone()


@unittest.skipUnless(HAS_DULWICH, "dulwich not installed (run tests with .venv python)")
class MorningTests(unittest.TestCase):
    """seed(morning=True): the presentation set-up of tools/seed_demo.py."""

    def seed(self, now):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        self.addCleanup(self._tmp.__exit__, None, None, None)
        demo.seed(self.db, Path(self.db.path).parent / "snaps", now=now, morning=True)

    def test_today_is_seven_to_nine_and_nothing_later(self):
        self.seed(local_ts(THURSDAY, 15))  # run in the afternoon: still stops at 09:00
        with self.db.connect() as conn:
            start, end = day_range(conn, "window_events", THURSDAY)
            self.assertEqual((start, end), (local_ts(THURSDAY, 7), local_ts(THURSDAY, 9)))
            changes = [r[0] for r in conn.execute("SELECT ts FROM changes WHERE baseline = 0 AND ts >= ?",
                                                  (local_ts(THURSDAY, 0),))]
            self.assertEqual(changes, [local_ts(THURSDAY, 8, 50)])
            yesterday = date(2026, 11, 4)
            self.assertEqual(day_range(conn, "window_events", yesterday)[0], local_ts(yesterday, 9))
            titles = {r[0] for r in conn.execute("SELECT title FROM window_events WHERE start_ts >= ?",
                                                 (local_ts(THURSDAY, 0),))}
            self.assertIn("[GOV-1430] 민원 목록 커서 기반 페이징 검토 - Jira - Chrome", titles)  # not the scrum

    def test_no_work_folders_without_a_snapshot_dir(self):
        """tools/seed_demo.py seeds this way: the folders added in settings bring the file changes."""
        self._tmp = TempDB()
        db = self._tmp.__enter__()
        self.addCleanup(self._tmp.__exit__, None, None, None)
        demo.seed(db, now=local_ts(THURSDAY, 15), morning=True)
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM watched_folders").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0], 0)
            self.assertGreater(conn.execute("SELECT COUNT(*) FROM window_events").fetchone()[0], 0)

    def test_a_weekend_borrows_thursdays_work(self):
        saturday = date(2026, 11, 7)
        self.seed(datetime(2026, 11, 7, 8).timestamp())  # run at 08:00: the morning is still complete
        with self.db.connect() as conn:
            self.assertEqual(tuple(day_range(conn, "window_events", saturday)),
                             (local_ts(saturday, 7), local_ts(saturday, 9)))


@unittest.skipUnless(HAS_DULWICH, "dulwich not installed (run tests with .venv python)")
class DemoWeekTests(unittest.TestCase):
    """tools/demo_week.py: a demo database with records on one fixed Monday-Friday only."""

    def run_tool(self, *args):
        spec = importlib.util.spec_from_file_location(
            "demo_week", Path(__file__).resolve().parent.parent / "tools" / "demo_week.py")
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        with contextlib.redirect_stdout(io.StringIO()):
            return tool.main(list(args))

    def test_records_only_on_the_chosen_week_with_file_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "week.db"
            self.assertEqual(self.run_tool("--week", "2026-11-16", "--db", str(path)), 0)
            db = Database(path)
            with db.connect() as conn:
                days = {r[0] for r in conn.execute(
                    "SELECT DISTINCT date(start_ts, 'unixepoch', 'localtime') FROM window_events")}
                self.assertEqual(days, {f"2026-11-{d}" for d in range(16, 21)})
                notes = {r[0] for r in conn.execute("SELECT day FROM notes")}
                self.assertTrue(notes <= days)
                self.assertGreater(conn.execute("SELECT COUNT(*) FROM changes WHERE baseline = 0").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT value FROM meta WHERE key = 'ai_model'").fetchone()[0],
                                 "claude-code:sonnet")
            # Never overwrites, and only takes a Monday.
            self.assertEqual(self.run_tool("--db", str(path)), 1)
            self.assertEqual(self.run_tool("--week", "2026-11-17", "--db", str(Path(tmp) / "x.db")), 1)


if __name__ == "__main__":
    unittest.main()
