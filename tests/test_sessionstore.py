import unittest
from datetime import date

from workhistory import service, sessionstore, store

from tests.helpers import TempDB, local_ts

DAY = date(2026, 9, 30)


class SessionStoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        self.aw_id = 0
        with self.db.connect() as conn:
            self.src = conn.execute("INSERT INTO sources (bucket_id, bucket_created, kind, hostname) "
                                    "VALUES ('w', 'c', 'window', 'h')").lastrowid
            self.gov = service.create_project(conn, {"name": "Gov24"})["id"]
            self.k8s = service.create_project(conn, {"name": "K8s"})["id"]
            service.add_rule(conn, self.gov, {"field": "title", "pattern": "gov24"})
            service.add_rule(conn, self.k8s, {"field": "title", "pattern": "kube"})

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def add(self, h1, m1, h2, m2, title, app="idea64.exe"):
        self.aw_id += 1
        start, end = local_ts(DAY, h1, m1), local_ts(DAY, h2, m2)
        with self.db.connect() as conn:
            store.mark_days_dirty(conn, start, end)
            return conn.execute(
                "INSERT INTO window_events (source_id, aw_id, start_ts, end_ts, app, title) VALUES (?, ?, ?, ?, ?, ?)",
                (self.src, self.aw_id, start, end, app, title)).lastrowid

    def extend(self, event_id, h, m):
        with self.db.connect() as conn:
            conn.execute("UPDATE window_events SET end_ts = ? WHERE id = ?", (local_ts(DAY, h, m), event_id))
            store.mark_days_dirty(conn, local_ts(DAY, h, m), local_ts(DAY, h, m))

    def sessions(self):
        with self.db.connect() as conn:
            return service.timeline(conn, DAY, now=local_ts(DAY, 23, 59))["sessions"]


class GroupingPersistenceTests(SessionStoreTestCase):
    def test_sessions_created_with_auto_fields(self):
        self.add(9, 0, 9, 40, "gov24-api – A.java")
        self.add(10, 0, 10, 30, "kubernetes docs - Google Chrome", app="chrome.exe")
        s = self.sessions()
        self.assertEqual([(x["project_id"], x["title"]) for x in s],
                         [(self.gov, "IntelliJ IDEA · gov24-api – A.java"),
                          (self.k8s, "Chrome · kubernetes docs")])
        self.assertEqual([x["active_s"] for x in s], [2400, 1800])
        self.assertFalse(any(x["title_edited"] or x["locked"] for x in s))

    def test_ids_stable_across_refreshes(self):
        self.add(9, 0, 9, 40, "gov24-api")
        ids = [x["id"] for x in self.sessions()]
        with self.db.connect() as conn:
            sessionstore.refresh_day(conn, DAY, now=local_ts(DAY, 23, 59))
        self.assertEqual([x["id"] for x in self.sessions()], ids)

    def test_ongoing_session_grows_and_keeps_edits(self):
        e = self.add(9, 0, 9, 20, "gov24-api")
        [s] = self.sessions()
        with self.db.connect() as conn:
            sessionstore.update_session(conn, s["id"], {"title": "로그인 버그 수정", "note": "토큰 만료 확인"})
        self.extend(e, 10, 0)
        [s2] = self.sessions()
        self.assertEqual((s2["id"], s2["title"], s2["note"]), (s["id"], "로그인 버그 수정", "토큰 만료 확인"))
        self.assertEqual(s2["end"], local_ts(DAY, 10, 0))
        self.assertFalse(s2["locked"])

    def test_edited_session_never_lost_when_regrouping_changes(self):
        self.add(9, 0, 9, 20, "gov24-api")
        [s] = self.sessions()
        with self.db.connect() as conn:
            sessionstore.update_session(conn, s["id"], {"note": "중요"})
        # New earlier activity bridges the start: the group now starts at 8:50.
        self.add(8, 50, 9, 0, "gov24-api")
        sessions = self.sessions()
        kept = [x for x in sessions if x["id"] == s["id"]]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["note"], "중요")
        self.assertTrue(kept[0]["locked"])
        self.assertEqual(sum(x["active_s"] for x in sessions), 30 * 60)  # nothing double counted

    def test_rule_change_updates_auto_project_but_not_manual(self):
        self.add(9, 0, 9, 30, "side-project README")
        self.add(10, 0, 10, 30, "side-project main.py")
        a, b = self.sessions()
        self.assertIsNone(a["project_id"])
        with self.db.connect() as conn:
            sessionstore.update_session(conn, b["id"], {"project_id": self.k8s})
            side = service.create_project(conn, {"name": "Side"})["id"]
            service.add_rule(conn, side, {"field": "title", "pattern": "side-project"})
        a, b = self.sessions()
        self.assertEqual((a["project_id"], a["project_manual"]), (side, False))
        self.assertEqual((b["project_id"], b["project_manual"]), (self.k8s, True))
        with self.db.connect() as conn:
            sessionstore.update_session(conn, b["id"], {"project_id": "auto"})
        self.assertEqual(self.sessions()[1]["project_id"], side)

    def test_manual_unassigned(self):
        self.add(9, 0, 9, 30, "gov24-api")
        [s] = self.sessions()
        with self.db.connect() as conn:
            sessionstore.update_session(conn, s["id"], {"project_id": None})
        [s] = self.sessions()
        self.assertEqual((s["project_id"], s["project_manual"]), (None, True))

    def test_blank_title_reverts_to_auto(self):
        self.add(9, 0, 9, 30, "gov24-api")
        [s] = self.sessions()
        with self.db.connect() as conn:
            sessionstore.update_session(conn, s["id"], {"title": "직접 제목"})
            sessionstore.update_session(conn, s["id"], {"title": "  "})
        [s] = self.sessions()
        self.assertEqual((s["title"], s["title_edited"]), ("IntelliJ IDEA · gov24-api", False))

    def test_excluded_session_not_counted(self):
        self.add(9, 0, 9, 30, "gov24-api")
        self.add(10, 0, 10, 10, "youtube", app="chrome.exe")
        with self.db.connect() as conn:
            yt = self.sessions()[1]
            sessionstore.update_session(conn, yt["id"], {"excluded": True})
            t = service.timeline(conn, DAY, now=local_ts(DAY, 23, 59))
        self.assertEqual(t["total_active_s"], 30 * 60)
        self.assertEqual([b["app"] for b in t["by_app"]], ["idea64.exe"])
        self.assertTrue(t["sessions"][1]["excluded"])  # still visible, marked

    def test_update_validation(self):
        self.add(9, 0, 9, 30, "gov24-api")
        [s] = self.sessions()
        with self.db.connect() as conn:
            with self.assertRaises(store.Invalid):
                sessionstore.update_session(conn, s["id"], {})
            with self.assertRaises(store.NotFound):
                sessionstore.update_session(conn, s["id"], {"project_id": 999})
            with self.assertRaises(store.NotFound):
                sessionstore.update_session(conn, 999, {"note": "x"})

    def test_ensure_all_refreshes_only_stale_days(self):
        self.add(9, 0, 9, 30, "gov24-api")
        with self.db.connect() as conn:
            self.assertEqual(sessionstore.ensure_all(conn), 1)
            self.assertEqual(sessionstore.ensure_all(conn), 0)
            store.bump_session_version(conn)
            self.assertEqual(sessionstore.ensure_all(conn), 1)


class MergeSplitTests(SessionStoreTestCase):
    def setUp(self):
        super().setUp()
        self.add(9, 0, 9, 30, "gov24-api – A.java")
        self.add(9, 30, 9, 45, "kubectl kube docs", app="chrome.exe")
        self.add(9, 45, 10, 15, "gov24-api – B.java")

    def test_merge_includes_sessions_in_between(self):
        a, b, c = self.sessions()
        with self.db.connect() as conn:
            sessionstore.update_session(conn, b["id"], {"note": "k8s 문서 읽음"})
            merged = sessionstore.merge_sessions(conn, [a["id"], c["id"]])
        [m] = self.sessions()
        self.assertEqual(m["id"], merged["id"])
        self.assertTrue(m["locked"])
        self.assertEqual((m["start"], m["end"]), (local_ts(DAY, 9), local_ts(DAY, 10, 15)))
        self.assertEqual(m["note"], "k8s 문서 읽음")
        self.assertEqual(m["active_s"], 75 * 60)
        self.assertEqual(m["project_id"], self.gov)  # dominant by time

    def test_merge_validation(self):
        a = self.sessions()[0]
        with self.db.connect() as conn:
            with self.assertRaises(store.Invalid):
                sessionstore.merge_sessions(conn, [a["id"]])
            with self.assertRaises(store.Invalid):
                sessionstore.merge_sessions(conn, ["x", "y"])

    def test_split(self):
        a = self.sessions()[0]
        with self.db.connect() as conn:
            sessionstore.update_session(conn, a["id"], {"title": "A 작업", "project_id": self.gov})
            first, second = sessionstore.split_session(conn, a["id"], local_ts(DAY, 9, 10))
        s = self.sessions()
        self.assertEqual([(x["start"], x["end"]) for x in s[:2]],
                         [(local_ts(DAY, 9), local_ts(DAY, 9, 10)), (local_ts(DAY, 9, 10), local_ts(DAY, 9, 30))])
        self.assertEqual((s[0]["title"], s[1]["title"]), ("A 작업", "IntelliJ IDEA · gov24-api – A.java"))
        self.assertTrue(s[0]["locked"] and s[1]["locked"])
        self.assertEqual(s[1]["project_id"], self.gov)
        self.assertEqual(sum(x["active_s"] for x in s), 75 * 60)

    def test_split_outside_range_rejected(self):
        a = self.sessions()[0]
        with self.db.connect() as conn:
            with self.assertRaises(store.Invalid):
                sessionstore.split_session(conn, a["id"], local_ts(DAY, 9, 30))
            with self.assertRaises(store.Invalid):
                sessionstore.split_session(conn, a["id"], "soon")

    def test_new_activity_does_not_enter_locked_session(self):
        a, b, c = self.sessions()
        with self.db.connect() as conn:
            sessionstore.merge_sessions(conn, [a["id"], c["id"]])
        self.add(10, 15, 10, 45, "gov24-api – C.java")
        s = self.sessions()
        self.assertEqual(len(s), 2)
        self.assertEqual((s[1]["start"], s[1]["locked"]), (local_ts(DAY, 10, 15), False))


class ReviewResumeSearchTests(SessionStoreTestCase):
    def setUp(self):
        super().setUp()
        self.add(9, 0, 9, 40, "gov24-api – UserController.java")
        self.add(10, 0, 10, 2, "Slack", app="slack.exe")
        self.add(10, 30, 11, 0, "kubernetes pods - Google Chrome", app="chrome.exe")
        with self.db.connect() as conn:
            service.create_note(conn, {"day": DAY.isoformat(), "body": "페이징 이슈 내일 확인", "project_id": self.gov})
            service.create_note(conn, {"day": DAY.isoformat(), "body": "점심 맛있었음"})
            service.update_project(conn, self.gov, {"next_action": "UserService 리팩터링"})

    def review(self):
        with self.db.connect() as conn:
            return service.get_review(conn, DAY, now=local_ts(DAY, 23, 59))

    def test_generated_review_sections(self):
        r = self.review()
        text = r["generated"]
        self.assertIn("2026-09-30 (수) 작업 리뷰", text)
        self.assertIn("## 프로젝트별 시간 [자동 집계]", text)
        self.assertIn("- Gov24: 40분", text)
        self.assertIn("- 합계: 1시간 12분", text)
        self.assertIn("[자동 추정", text)
        self.assertIn("09:00–09:40 (40분) [Gov24] IntelliJ IDEA · gov24-api – UserController.java", text)
        main_sessions = text.split("## 주요 작업 세션")[1].split("## 프로젝트별 마지막")[0]
        self.assertNotIn("Slack", main_sessions)  # shorter than 5 minutes
        self.assertIn("## 메모 [직접 작성]\n- [Gov24] 페이징 이슈 내일 확인\n- 점심 맛있었음", text)
        self.assertEqual(r["followups"], ["(메모) 페이징 이슈 내일 확인", "(다음 할 일) [Gov24] UserService 리팩터링"])
        self.assertIsNone(r["saved"])

    def test_review_excludes_excluded_sessions(self):
        with self.db.connect() as conn:
            k = self.sessions()[-1]
            sessionstore.update_session(conn, k["id"], {"excluded": True})
        self.assertNotIn("kubernetes", self.review()["generated"])

    def test_save_and_clear_review(self):
        with self.db.connect() as conn:
            saved = service.save_review(conn, {"day": DAY.isoformat(), "body": "# 내가 쓴 리뷰\n로그인 완료"})
            self.assertEqual(saved["body"], "# 내가 쓴 리뷰\n로그인 완료")
            service.save_review(conn, {"day": DAY.isoformat(), "body": "# 수정"})
        self.assertEqual(self.review()["saved"]["body"], "# 수정")
        with self.db.connect() as conn:
            self.assertIsNone(service.save_review(conn, {"day": DAY.isoformat(), "body": ""}))
        self.assertIsNone(self.review()["saved"])

    def test_resume_project(self):
        with self.db.connect() as conn:
            s = self.sessions()[0]
            sessionstore.update_session(conn, s["id"], {"note": "컨트롤러 테스트 작성 중"})
            r = service.resume_project(conn, self.gov, now=local_ts(DAY, 23, 59))
        self.assertEqual(r["project"]["next_action"], "UserService 리팩터링")
        self.assertEqual(r["last_session"]["end"], local_ts(DAY, 9, 40))
        self.assertEqual(r["last_activity"]["title"], "gov24-api – UserController.java")
        self.assertEqual([n["body"] for n in r["notes"]], ["페이징 이슈 내일 확인"])
        self.assertEqual([n["note"] for n in r["session_notes"]], ["컨트롤러 테스트 작성 중"])

    def test_resume_without_history(self):
        with self.db.connect() as conn:
            empty = service.create_project(conn, {"name": "새 프로젝트"})["id"]
            r = service.resume_project(conn, empty)
        self.assertIsNone(r["last_session"])
        self.assertIsNone(r["last_activity"])
        self.assertEqual(r["recent_sessions"], [])

    def test_resume_overview_sorted_by_recency(self):
        with self.db.connect() as conn:
            o = service.resume_overview(conn, now=local_ts(DAY, 23, 59))
        self.assertEqual([p["name"] for p in o], ["K8s", "Gov24"])

    def test_search_sessions_and_reviews(self):
        with self.db.connect() as conn:
            s = self.sessions()[0]
            sessionstore.update_session(conn, s["id"], {"title": "로그인 토큰 버그"})
            service.save_review(conn, {"day": DAY.isoformat(), "body": "오늘은 토큰 갱신 로직을 고쳤다"})
            r = service.search(conn, "토큰")
            self.assertEqual([x["title"] for x in r["sessions"]], ["로그인 토큰 버그"])
            self.assertEqual([x["day"] for x in r["reviews"]], [DAY.isoformat()])
            self.assertIn("토큰 갱신", r["reviews"][0]["excerpt"])
            self.assertEqual(r["projects"], [])
            self.assertEqual(len(service.search(conn, "토큰", project=str(self.k8s))["sessions"]), 0)

    def test_project_search_reports_last_activity(self):
        with self.db.connect() as conn:
            r = service.search(conn, "Gov24")
        self.assertEqual(r["projects"][0]["last_activity_ts"], local_ts(DAY, 9, 40))


class StatsTests(SessionStoreTestCase):
    def test_counts_corrections_per_day(self):
        from workhistory import stats
        self.add(9, 0, 9, 30, "gov24-api")
        self.add(10, 0, 10, 30, "random")
        a, b = self.sessions()
        with self.db.connect() as conn:
            sessionstore.update_session(conn, a["id"], {"title": "직접"})
            sessionstore.update_session(conn, b["id"], {"excluded": True})
            service.save_review(conn, {"day": DAY.isoformat(), "body": "리뷰"})
        days = {d["day"]: d for d in stats.collect(self.db, (date.today() - DAY).days + 1)}
        d = days[DAY.isoformat()]
        self.assertEqual((d["sessions"], d["renamed"], d["excluded"], d["review_saved"]), (2, 1, 1, True))
        self.assertEqual(d["classified_s"], 30 * 60)  # excluded and unassigned time not counted


if __name__ == "__main__":
    unittest.main()
