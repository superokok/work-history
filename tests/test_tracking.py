import time
import unittest
from datetime import date, timedelta

from workhistory import service, sessionstore
from workhistory.awclient import AWClient
from workhistory.classify import TrackRule, is_tracked
from workhistory.store import Invalid
from workhistory.sync import recent_apps, reset_window_cursors, sync_once

from tests.helpers import FakeAW, TempDB, local_ts

WIN = "aw-watcher-window_devbox"
AFK = "aw-watcher-afk_devbox"


class IsTrackedTests(unittest.TestCase):
    def test_app_and_title_rules(self):
        rules = [TrackRule("idea64"), TrackRule("chrome", "jira"), TrackRule("", "gov24")]
        self.assertTrue(is_tracked(rules, "idea64.exe", "anything"))
        self.assertTrue(is_tracked(rules, "chrome.exe", "PROJ-1 - Jira - Google Chrome"))
        self.assertFalse(is_tracked(rules, "chrome.exe", "YouTube - Google Chrome"))
        self.assertTrue(is_tracked(rules, "notepad.exe", "gov24 메모"))
        self.assertFalse(is_tracked(rules, "KakaoTalk.exe", "가족방"))
        self.assertFalse(is_tracked([TrackRule("  ", "")], "x.exe", "y"))


class TrackingSyncTests(unittest.TestCase):
    def setUp(self):
        self.aw = FakeAW()
        self.aw.add_bucket(WIN, "currentwindow")
        self.aw.add_bucket(AFK, "afkstatus")
        self.client = AWClient(self.aw.url, timeout=5)
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        self.day = date.today() - timedelta(days=1)
        t = lambda h, m=0: local_ts(self.day, h, m)
        self.aw.add_event(WIN, t(9), t(9, 40), {"app": "idea64.exe", "title": "gov24-api – A.java"})
        self.aw.add_event(WIN, t(9, 40), t(10, 0), {"app": "chrome.exe", "title": "YouTube - Google Chrome"})
        self.aw.add_event(WIN, t(10, 0), t(10, 20), {"app": "chrome.exe", "title": "PROJ-1 - Jira - Google Chrome"})
        self.aw.add_event(WIN, t(10, 20), t(10, 30), {"app": "LockApp.exe", "title": "Windows 기본 잠금 화면"})
        self.aw.add_event(WIN, t(10, 30), t(11, 0), {"app": "KakaoTalk.exe", "title": "가족방"})
        with self.db.connect() as conn:
            service.add_track_rule(conn, {"app": "idea64"})
            service.add_track_rule(conn, {"app": "chrome", "title": "jira"})
            service.set_track_mode(conn, {"mode": "selected"})

    def tearDown(self):
        self._tmp.__exit__(None, None, None)
        self.aw.close()

    def events(self):
        with self.db.connect() as conn:
            return [(r["app"], r["title"]) for r in conn.execute(
                "SELECT app, title FROM window_events ORDER BY start_ts")]

    def test_untracked_apps_keep_only_time(self):
        sync_once(self.db, self.client, backfill_days=3)
        self.assertEqual(self.events(), [
            ("idea64.exe", "gov24-api – A.java"), ("", ""), ("chrome.exe", "PROJ-1 - Jira - Google Chrome"),
            ("LockApp.exe", "Windows 기본 잠금 화면"), ("", "")])

    def test_timeline_and_review_separate_offwork(self):
        sync_once(self.db, self.client, backfill_days=3)
        with self.db.connect() as conn:
            t = service.timeline(conn, self.day)
            r = service.get_review(conn, self.day)
        self.assertEqual(t["offwork_s"], 50 * 60)
        self.assertEqual(t["total_active_s"], 110 * 60)  # lock screen is idle, not counted
        self.assertEqual(sum(p["seconds"] for p in t["by_project"]), 60 * 60)
        self.assertNotIn("", [a["app"] for a in t["by_app"]])
        offwork = [s for s in t["sessions"] if s["offwork"]]
        # YouTube (20 min) and KakaoTalk (30 min): each long enough to be its own session.
        self.assertEqual([s["title"] for s in offwork], ["업무 외", "업무 외"])
        self.assertIn("- 업무 외 (기록 대상이 아닌 앱): 50분", r["generated"])
        self.assertIn("- 합계: 업무 1시간 0분 / 전체 1시간 50분", r["generated"])
        self.assertNotIn("업무 외", r["generated"].split("## 주요 작업 세션")[1].split("##")[1])
        self.assertNotIn("가족방", r["generated"])

    def test_reapply_recovers_forgotten_app_and_back_to_all(self):
        sync_once(self.db, self.client, backfill_days=3)
        with self.db.connect() as conn:
            service.add_track_rule(conn, {"app": "kakaotalk"})
            reset_window_cursors(conn, 3)
        sync_once(self.db, self.client)
        self.assertIn(("KakaoTalk.exe", "가족방"), self.events())
        self.assertIn(("", ""), self.events())  # YouTube still untracked
        with self.db.connect() as conn:
            service.set_track_mode(conn, {"mode": "all"})
            reset_window_cursors(conn, 3)
        sync_once(self.db, self.client)
        self.assertNotIn(("", ""), self.events())
        with self.db.connect() as conn:
            t = service.timeline(conn, self.day)
        self.assertEqual(t["offwork_s"], 0)

    def test_rules_guarded(self):
        with self.db.connect() as conn:
            with self.assertRaises(Invalid):
                service.add_track_rule(conn, {"app": " ", "title": ""})
            rules = service.get_tracking(conn)["rules"]
            service.delete_track_rule(conn, rules[0]["id"])
            with self.assertRaises(Invalid):  # last rule while recording only selected apps
                service.delete_track_rule(conn, rules[1]["id"])
            service.add_track_rule(conn, {"app": "IDEA64"})  # duplicate of the remaining rule
            self.assertEqual(len(service.get_tracking(conn)["rules"]), 1)

    def test_mode_needs_rules(self):
        with TempDB() as db, db.connect() as conn:
            with self.assertRaises(Invalid):
                service.set_track_mode(conn, {"mode": "selected"})
            self.assertEqual(service.get_tracking(conn)["mode"], "all")

    def test_recent_apps_reads_aw_without_storing(self):
        with self.db.connect() as conn:
            rules = [TrackRule(r["app_pattern"], r["title_pattern"]) for r in service.get_tracking(conn)["rules"]]
        apps = recent_apps(self.client, rules, days=3)
        self.assertEqual([(a["app"], round(a["seconds"] / 60), a["tracked"]) for a in apps],
                         [("chrome.exe", 40, True), ("idea64.exe", 40, True), ("KakaoTalk.exe", 30, False)])
        self.assertEqual(self.events(), [])  # nothing imported


class OffworkSessionTests(unittest.TestCase):
    def test_short_offwork_detour_absorbed_with_work_title(self):
        from workhistory.normalize import Activity
        from workhistory.sessions import auto_title, dominant_project, group_activities, offwork_seconds
        acts = [Activity(1, 0, 1200, "idea64.exe", "A.java", 5, "rule"),
                Activity(2, 1200, 1320, "", "", None, None),  # 2-minute detour
                Activity(3, 1320, 2400, "idea64.exe", "A.java", 5, "rule")]
        [g] = group_activities(acts)
        self.assertEqual((auto_title(g), dominant_project(g), offwork_seconds(g)),
                         ("IntelliJ IDEA · A.java", 5, 120))


if __name__ == "__main__":
    unittest.main()
