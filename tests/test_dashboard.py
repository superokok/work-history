import unittest
from datetime import date, timedelta

from workhistory import categories, dashboard, service
from workhistory.classify import OFFWORK_APP

from tests.helpers import TempDB, local_ts
from tests.test_service import add_source, add_window

DAY = date(2026, 9, 30)  # a Wednesday
NOW = local_ts(DAY, 20)


class CategoryTests(unittest.TestCase):
    def test_apps_and_browser_titles(self):
        cases = [
            ("idea64.exe", "gov24-api – UserController.java", "dev"),
            ("WindowsTerminal.exe", "kubectl get pods", "dev"),
            ("Hwp.exe", "주간 보고.hwp", "doc"),
            ("EXCEL.EXE", "일정.xlsx", "doc"),
            ("slack.exe", "#dev | Slack", "comm"),
            ("Zoom.exe", "Zoom 회의", "comm"),
            ("chrome.exe", "[GOV-1418] 토큰 재발급 - Jira - Chrome", "web"),
            ("msedge.exe", "개발자 브이로그 - YouTube - Edge", "web"),
            ("mspaint2.exe", "x", "other"),
            (OFFWORK_APP, "", "offwork"),
        ]
        for app, title, want in cases:
            self.assertEqual(categories.category(app, title), want, (app, title))

    def test_every_category_has_a_label_and_color(self):
        self.assertEqual(len(set(categories.KEYS)), len(categories.CATEGORIES))
        self.assertTrue(categories.WORK <= set(categories.KEYS))


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        with self.db.connect() as conn:
            src = add_source(conn)
            add_window(conn, src, 1, local_ts(DAY, 9), local_ts(DAY, 10), "idea64.exe", "gov24-api – UserController.java")
            add_window(conn, src, 2, local_ts(DAY, 10), local_ts(DAY, 10, 20), "chrome.exe", "[GOV-1418] - Jira - Chrome")
            add_window(conn, src, 3, local_ts(DAY, 10, 20), local_ts(DAY, 10, 30), "chrome.exe", "뉴스 - YouTube - Chrome")
            add_window(conn, src, 4, local_ts(DAY, 14), local_ts(DAY, 14, 30), "slack.exe", "#dev | Slack")
            yesterday = DAY - timedelta(days=1)
            add_window(conn, src, 5, local_ts(yesterday, 9), local_ts(yesterday, 9, 30), "idea64.exe", "gov24-api")

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
        self.assertEqual(t["active_s"], 7200)
        self.assertEqual(t["work_s"], 7200)
        self.assertEqual(r["previous"]["active_s"], 1800)  # the day before
        self.assertEqual([b["kind"] for b in r["blocks"]], ["dev", "web", "comm"])
        self.assertEqual(sum(p["seconds"] for p in r["projects"]), 7200)
        self.assertEqual(len(r["week"]), 7)
        self.assertEqual(r["week"][0]["day"], (DAY - timedelta(days=2)).isoformat())  # Monday

    def test_week_and_days_ahead(self):
        with self.db.connect() as conn:
            r = dashboard.dashboard(conn, DAY, "week", now=NOW)
        self.assertEqual((r["start"], r["end"]), ("2026-09-28", "2026-10-04"))
        self.assertEqual(r["totals"]["active_s"], 7200 + 1800)
        self.assertNotIn("blocks", r)
        self.assertEqual(sum(r["week"][4]["by_kind"].values()), 0)  # Friday has not happened yet

    def test_excluded_sessions_do_not_count(self):
        with self.db.connect() as conn:
            dashboard.dashboard(conn, DAY, "day", now=NOW)  # builds the sessions
            conn.execute("UPDATE sessions SET excluded = 1 WHERE day = ?", (DAY.isoformat(),))
            r = dashboard.dashboard(conn, DAY, "day", now=NOW)
        self.assertEqual(r["totals"]["active_s"], 0)
        self.assertEqual(r["blocks"], [])

    def test_rule_suggestions(self):
        with self.db.connect() as conn:
            items = dashboard.unassigned(conn, days=3, now=NOW)
            self.assertEqual((items[0]["app"], items[0]["seconds"]), ("idea64.exe", 3600))
            self.assertIn("gov24-api", items[0]["candidates"])
            pid = service.create_project(conn, {"name": "Gov24"})["id"]
            service.add_rule(conn, pid, {"field": "title", "pattern": "gov24-api"})
            left = dashboard.unassigned(conn, days=3, now=NOW)
        self.assertNotIn("idea64.exe", [i["app"] for i in left])  # the rule took it
        self.assertIn("chrome.exe", [i["app"] for i in left])

    def test_keyword_candidates(self):
        words = dashboard.keyword_candidates("chrome.exe", "[GOV-1418] 리프레시 토큰 재발급 - Jira - Chrome")
        self.assertEqual(words[0], "GOV-1418")
        self.assertIn("Jira", words)
        self.assertNotIn("Chrome", words)
        self.assertLessEqual(len(words), dashboard.MAX_CANDIDATES)

    def test_unknown_span(self):
        with self.db.connect() as conn, self.assertRaises(ValueError):
            dashboard.dashboard(conn, DAY, "month", now=NOW)


if __name__ == "__main__":
    unittest.main()
