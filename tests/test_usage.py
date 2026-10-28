import json
import unittest
from datetime import date

from workhistory import ai_service, service, usage
from workhistory.store import Invalid

from tests.helpers import TempDB

DAY = date(2026, 10, 30)  # a Friday
NOW = 1_800_000_000.0


class UsageTests(unittest.TestCase):
    def test_ai_report_counts_kept_and_edited_lines(self):
        with TempDB() as db, db.connect() as conn:
            draft = "# 주간보고\n- A 작업\n- B 작업\n\n- C 작업\n"
            final = "# 주간보고\n- A 작업\n- B 작업 (수정)\n- C 작업\n- D 추가\n"
            ai_service.save_report(conn, {"kind": "weekly", "day": DAY.isoformat(), "body": final, "metrics": {
                "sid": "s1", "method": "ai", "ai_draft": draft, "active_s": 125.4, "wall_s": 400}})
            data = json.loads(conn.execute("SELECT data FROM usage_events").fetchone()[0])
            self.assertEqual(data, {"sid": "s1", "report": "weekly", "start": "2026-10-26", "method": "ai",
                                    "active_s": 125.4, "wall_s": 400.0, "lines": 5,
                                    "ai_lines": 4, "kept": 3, "edited": 2})

    def test_only_numbers_are_stored(self):
        with TempDB() as db, db.connect() as conn:
            ai_service.save_report(conn, {"kind": "daily", "day": DAY.isoformat(), "body": "비밀 프로젝트 X", "metrics": {
                "sid": "s1", "method": "ai", "ai_draft": "비밀 프로젝트 X 초안", "active_s": 10, "wall_s": 10}})
            usage.record_client(conn, {"kind": "search", "q": "비밀"})
            dump = " ".join(r[0] for r in conn.execute("SELECT data FROM usage_events"))
            self.assertNotIn("비밀", dump)

    def test_report_without_metrics_is_not_measured(self):
        with TempDB() as db, db.connect() as conn:
            ai_service.save_report(conn, {"kind": "daily", "day": DAY.isoformat(), "body": "x"})
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0], 0)

    def test_bad_values_are_rejected(self):
        with TempDB() as db, db.connect() as conn:
            for metrics in ({"sid": "s", "method": "magic", "active_s": 1, "wall_s": 1},
                            {"sid": "s", "method": "ai", "active_s": "1", "wall_s": 1},
                            {"sid": "", "method": "ai", "active_s": 1, "wall_s": 1}):
                with self.assertRaises(Invalid):
                    usage.record_report(conn, "daily", "2026-10-30", "x", metrics)
            with self.assertRaises(Invalid):
                usage.record_client(conn, {"kind": "keystrokes"})
            usage.record_report(conn, "daily", "2026-10-30", "x",
                                {"sid": "s", "method": "manual", "active_s": 1e9, "wall_s": -5})
            data = json.loads(conn.execute("SELECT data FROM usage_events").fetchone()[0])
            self.assertEqual((data["active_s"], data["wall_s"]), (usage.MAX_SECONDS, 0.0))

    def test_summary_uses_last_save_per_session(self):
        with TempDB() as db, db.connect() as conn:
            def save(sid, method, active, ts, draft=None):
                usage.record_report(conn, "weekly", "2026-10-26", "a\nb\nc", {
                    "sid": sid, "method": method, "active_s": active, "wall_s": active, "ai_draft": draft}, now=ts)
            save("m1", "manual", 900, NOW - 500)
            save("m1", "manual", 1200, NOW - 400)  # saved again later in the same session
            save("a1", "ai", 200, NOW - 300, "a\nb\nx")
            save("a2", "ai", 400, NOW - 200, "a\nb\nc")
            usage.record_client(conn, {"kind": "search"}, now=NOW - 100)
            usage.record_client(conn, {"kind": "search_open", "after_s": 8}, now=NOW - 90)
            usage.record_client(conn, {"kind": "source_open", "from": "report"}, now=NOW - 80)
            usage.record_client(conn, {"kind": "search"}, now=NOW - 40 * 86400)  # outside 30 days
            s = usage.summary(conn, 30, now=NOW)
            self.assertEqual(len(s["reports"]), 3)
            self.assertEqual(s["by_method"]["manual"], {"count": 1, "median_active_s": 1200.0})
            self.assertEqual(s["by_method"]["ai"], {"count": 2, "median_active_s": 300.0})
            self.assertEqual(s["ai_kept_ratio"], round(5 / 6, 3))
            self.assertEqual((s["searches"], s["search_opens"], s["search_open_median_s"], s["source_opens"]),
                             (1, 1, 8.0, 1))

    def test_delete_all_data_removes_measurements(self):
        with TempDB() as db, db.connect() as conn:
            usage.record_client(conn, {"kind": "search"})
            counts = service.delete_data(conn, {"all": True})
            self.assertEqual(counts["usage_events"], 1)


if __name__ == "__main__":
    unittest.main()
