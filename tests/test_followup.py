import unittest
from datetime import date, timedelta

from workhistory import followup
from tests.helpers import TempDB, local_ts

LAST_MONDAY = date(2026, 9, 21)
MONDAY = LAST_MONDAY + timedelta(days=7)

LAST_WEEK = """# 주간보고 · 2026-09-21 ~ 2026-09-27

요약: 토큰 재발급 작업 진행

## GOV-1418 토큰 재발급 (8시간 16분)
[금주 실적]
- 회전 메서드 추가 (JwtTokenProvider) (완료)
[차주 계획]
- MR !312 리뷰 의견 반영 (제안)
- TokenRefreshFilterTest 보완
-

## project-b 화면 정리 (1시간)
[금주 실적]
- 목록 화면 수정 (진행 중)
[차주 계획]
- 목록 화면 마무리

## 기타
[차주 계획]
- 팀 회의 준비
"""


class ParseTests(unittest.TestCase):
    def test_plan_lines_of_each_group(self):
        self.assertEqual(followup.parse_plans(LAST_WEEK), [
            {"group": "GOV-1418 토큰 재발급", "text": "MR !312 리뷰 의견 반영", "suggested": True},
            {"group": "GOV-1418 토큰 재발급", "text": "TokenRefreshFilterTest 보완", "suggested": False},
            {"group": "project-b 화면 정리", "text": "목록 화면 마무리", "suggested": False},
            {"group": "기타", "text": "팀 회의 준비", "suggested": False},
        ])

    def test_clues(self):
        self.assertEqual(followup._clues("MR !312 리뷰 의견 반영"), ["!312"])
        self.assertEqual(followup._clues("GOV-1423 V42__minwon_req_index.sql 적용 (MinwonQueryRepository)"),
                         ["GOV-1423", "V42__minwon_req_index.sql", "MinwonQueryRepository"])
        self.assertEqual(followup._clues("poc-dev에 Ingress 배포 확인"), ["poc-dev", "Ingress"])
        self.assertEqual(followup._clues("팀 회의 준비"), [])
        self.assertEqual(followup._clues("Chrome에서 review 확인"), [])  # too common to point at work


class PreviousPlansTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        with self.db.connect() as conn:
            self.src = conn.execute("INSERT INTO sources (bucket_id, bucket_created, kind, hostname) "
                                    "VALUES ('w', 'c', 'window', 'h')").lastrowid
            self.folder = conn.execute("INSERT INTO watched_folders (path, label, created_at) "
                                       "VALUES ('C:/app/project-b', 'project-b', 0)").lastrowid
            conn.execute("INSERT INTO reports (kind, start_day, end_day, body, created_at, updated_at) "
                         "VALUES ('weekly', ?, ?, ?, 0, 0)",
                         (LAST_MONDAY.isoformat(), (LAST_MONDAY + timedelta(days=6)).isoformat(), LAST_WEEK))

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def window(self, conn, day, hour, title, aw):
        conn.execute("INSERT INTO window_events (source_id, aw_id, start_ts, end_ts, app, title) "
                     "VALUES (?, ?, ?, ?, 'chrome.exe', ?)", (self.src, aw, local_ts(day, hour), local_ts(day, hour, 30), title))

    def plans(self):
        with self.db.connect() as conn:
            return followup.previous_plans(conn, "weekly", MONDAY, MONDAY + timedelta(days=6))

    def test_this_weeks_records_are_found_by_the_plans_clues(self):
        with self.db.connect() as conn:
            self.window(conn, MONDAY, 10, "MR !312 토큰 재발급 - GitLab - Chrome", 1)
            self.window(conn, LAST_MONDAY, 10, "TokenRefreshFilterTest.java - IntelliJ", 2)  # last week: not counted
            change = conn.execute("INSERT INTO changes (folder_id, commit_id, ts, baseline) VALUES (?, 'c', ?, 0)",
                                  (self.folder, local_ts(MONDAY + timedelta(days=1), 15))).lastrowid
            conn.execute("INSERT INTO change_files (change_id, path, kind, is_text) VALUES (?, 'src/List.vue', 'M', 1)",
                         (change,))
        out = self.plans()
        self.assertEqual(out["from"], {"start": "2026-09-21", "end": "2026-09-27"})
        by_text = {p["text"]: p for p in out["plans"]}
        mr = by_text["MR !312 리뷰 의견 반영"]
        self.assertEqual((mr["status"], mr["count"], mr["suggested"]), ("related", 1, True))
        self.assertEqual(mr["evidence"][0]["day"], MONDAY.isoformat())
        self.assertEqual(by_text["TokenRefreshFilterTest 보완"]["status"], "none")
        # No specific clue, but the plan's work folder changed this week.
        listing = by_text["목록 화면 마무리"]
        self.assertEqual((listing["status"], listing["folders"], listing["folder_changes"]), ("folder", ["project-b"], 1))
        self.assertEqual(by_text["팀 회의 준비"]["status"], "no_clue")

    def test_the_latest_earlier_report_is_used(self):
        with self.db.connect() as conn:
            out = followup.previous_plans(conn, "weekly", LAST_MONDAY, LAST_MONDAY + timedelta(days=6))
            later = followup.previous_plans(conn, "weekly", MONDAY + timedelta(days=14), MONDAY + timedelta(days=20))
        self.assertEqual(out, {"from": None, "plans": []})  # nothing saved before that week
        self.assertEqual(later["from"]["start"], "2026-09-21")  # weeks without a report are skipped
        with self.db.connect() as conn:
            self.assertEqual(followup.previous_plans(conn, "daily", MONDAY, MONDAY)["from"], None)  # other kind


if __name__ == "__main__":
    unittest.main()
