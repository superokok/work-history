import unittest
from datetime import date, timedelta

from workhistory import categories, dashboard
from workhistory.classify import OFFWORK_APP

from tests.helpers import TempDB, local_ts
from tests.test_service import add_source, add_window

DAY = date(2026, 9, 30)  # a Wednesday
NOW = local_ts(DAY, 20)


class CategoryTests(unittest.TestCase):
    def test_apps(self):
        cases = [
            ("idea64.exe", "project-a – UserController.java", "dev"),
            ("WindowsTerminal.exe", "kubectl get pods", "dev"),
            ("Hwp.exe", "주간 보고.hwp", "doc"),
            ("EXCEL.EXE", "일정.xlsx", "doc"),
            ("slack.exe", "#dev | Slack", "comm"),
            ("Zoom.exe", "Zoom 회의", "comm"),
            ("chrome.exe", "[ABC-12] 토큰 재발급 - Jira - Chrome", "web"),
            ("msedge.exe", "동영상 - YouTube - Edge", "web"),
            ("mspaint2.exe", "x", "other"),
            (OFFWORK_APP, "", "offwork"),
        ]
        for app, title, want in cases:
            self.assertEqual(categories.category(app, title), want, (app, title))

    def test_every_category_has_a_label_and_color(self):
        self.assertEqual(len(set(categories.KEYS)), len(categories.CATEGORIES))
        self.assertTrue(categories.WORK <= set(categories.KEYS))


def add_change(conn, folder_id, ts, files=1, added=0, removed=0, baseline=0):
    conn.execute("INSERT INTO changes (folder_id, commit_id, ts, baseline, files_modified, lines_added, "
                 "lines_removed) VALUES (?, 'c', ?, ?, ?, ?, ?)", (folder_id, ts, baseline, files, added, removed))


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        with self.db.connect() as conn:
            src = add_source(conn)
            add_window(conn, src, 1, local_ts(DAY, 9), local_ts(DAY, 10), "idea64.exe", "project-a – UserController.java")
            add_window(conn, src, 2, local_ts(DAY, 10), local_ts(DAY, 10, 20), "chrome.exe", "[ABC-12] - Jira - Chrome")
            add_window(conn, src, 3, local_ts(DAY, 10, 20), local_ts(DAY, 10, 30), "chrome.exe", "뉴스 - Chrome")
            add_window(conn, src, 4, local_ts(DAY, 14), local_ts(DAY, 14, 30), "slack.exe", "#dev | Slack")
            yesterday = DAY - timedelta(days=1)
            add_window(conn, src, 5, local_ts(yesterday, 9), local_ts(yesterday, 9, 30), "idea64.exe", "project-a")
            a = conn.execute("INSERT INTO watched_folders (path, label, created_at) VALUES ('C:\app\a', 'project-a', 0)").lastrowid
            b = conn.execute("INSERT INTO watched_folders (path, label, created_at) VALUES ('C:\app\b', 'docs-b', 0)").lastrowid
            add_change(conn, a, local_ts(DAY, 9, 30), files=3, added=40, removed=5)
            add_change(conn, a, local_ts(DAY, 9, 50), files=1, added=2, removed=1)
            add_change(conn, b, local_ts(DAY, 15), files=1, added=1)
            add_change(conn, b, local_ts(DAY, 8), files=50, baseline=1)  # the first snapshot is not work

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_day(self):
        with self.db.connect() as conn:
            r = dashboard.dashboard(conn, DAY, "day", now=NOW)
        self.assertEqual((r["start"], r["end"]), (DAY.isoformat(), DAY.isoformat()))
        self.assertEqual(r["by_kind"]["dev"], 3600)
        self.assertEqual(r["by_kind"]["web"], 1800)
        self.assertEqual(r["by_kind"]["comm"], 1800)
        t = r["totals"]
        self.assertEqual((t["active_s"], t["work_s"]), (7200, 7200))
        self.assertEqual(r["previous"]["active_s"], 1800)  # the day before
        self.assertEqual([b["kind"] for b in r["blocks"]], ["dev", "web", "comm"])
        self.assertEqual(r["top_app"]["app"], "idea64.exe")
        self.assertEqual(sum(w["seconds"] for w in r["work"]), 7200)
        self.assertTrue(all(w["day"] == DAY.isoformat() for w in r["work"]))
        self.assertEqual(len(r["week"]), 7)
        self.assertEqual(r["week"][0]["day"], (DAY - timedelta(days=2)).isoformat())  # Monday

    def test_folders(self):
        with self.db.connect() as conn:
            r = dashboard.dashboard(conn, DAY, "day", now=NOW)
        self.assertEqual([(f["label"], f["changes"], f["files"]) for f in r["folders"]],
                         [("project-a", 2, 4), ("docs-b", 1, 1)])
        self.assertEqual(r["folders"][0]["day"], DAY.isoformat())
        self.assertEqual((r["files"], r["lines_added"], r["lines_removed"]), (5, 43, 6))

    def test_week_and_days_ahead(self):
        with self.db.connect() as conn:
            r = dashboard.dashboard(conn, DAY, "week", now=NOW)
        self.assertEqual((r["start"], r["end"]), ("2026-09-28", "2026-10-04"))
        self.assertEqual(r["totals"]["active_s"], 7200 + 1800)
        self.assertNotIn("blocks", r)
        self.assertEqual(sum(r["week"][4]["by_kind"].values()), 0)  # Friday has not happened yet

    def test_unknown_span(self):
        with self.db.connect() as conn, self.assertRaises(ValueError):
            dashboard.dashboard(conn, DAY, "month", now=NOW)


if __name__ == "__main__":
    unittest.main()
