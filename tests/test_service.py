import sqlite3
import unittest
from datetime import date, timedelta

from workhistory import service, store
from workhistory.db import SCHEMA_VERSION, Database

from tests.helpers import TempDB, local_ts

DAY = date(2026, 9, 30)


def add_source(conn) -> int:
    return conn.execute("INSERT INTO sources (bucket_id, bucket_created, kind, hostname) "
                        "VALUES ('w', 'c', 'window', 'h')").lastrowid


def all_rows(t):
    return [r for s in t["sessions"] for r in s["rows"]]


def add_window(conn, source, aw_id, start, end, app, title):
    store.mark_days_dirty(conn, start, end)  # as sync does
    return conn.execute("INSERT INTO window_events (source_id, aw_id, start_ts, end_ts, app, title) "
                        "VALUES (?, ?, ?, ?, ?, ?)", (source, aw_id, start, end, app, title)).lastrowid


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        with self.db.connect() as conn:
            src = add_source(conn)
            self.e1 = add_window(conn, src, 1, local_ts(DAY, 9), local_ts(DAY, 9, 30), "idea64.exe", "gov24-api – UserController.java")
            self.e2 = add_window(conn, src, 2, local_ts(DAY, 9, 30), local_ts(DAY, 9, 40), "chrome.exe", "Spring 트랜잭션_전파 100% 정리 - Chrome")
            self.e3 = add_window(conn, src, 3, local_ts(DAY, 10), local_ts(DAY, 10, 20), "chrome.exe", "Kubernetes Pods - Chrome")
            self.e4 = add_window(conn, src, 4, local_ts(DAY + timedelta(days=1), 9), local_ts(DAY + timedelta(days=1), 9, 10), "idea64.exe", "gov24-api – UserService.java")
            self.gov = service.create_project(conn, {"name": "Gov24 백엔드"})["id"]
            self.k8s = service.create_project(conn, {"name": "Kubernetes 학습", "color": "#20a37a"})["id"]
            service.add_rule(conn, self.gov, {"field": "title", "pattern": "gov24"})

    def tearDown(self):
        self._tmp.__exit__(None, None, None)


class MigrationTests(unittest.TestCase):
    def test_migrate_is_idempotent(self):
        with TempDB() as db:
            self.assertEqual(db.migrate(), SCHEMA_VERSION)
            self.assertEqual(Database(db.path).migrate(), SCHEMA_VERSION)
            with db.connect() as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_refuses_newer_schema(self):
        with TempDB() as db:
            with db.connect() as conn:
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
            with self.assertRaises(RuntimeError):
                db.migrate()

    def test_failed_write_rolls_back(self):
        with TempDB() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                with db.connect() as conn:
                    service.create_project(conn, {"name": "A"})
                    conn.execute("INSERT INTO projects (name, created_at) VALUES ('a', 0)")  # NOCASE dup
            with db.connect() as conn:
                self.assertEqual(service.list_projects(conn), [])


class TimelineTests(ServiceTestCase):
    def test_timeline_rows_and_totals(self):
        with self.db.connect() as conn:
            t = service.timeline(conn, DAY, now=local_ts(DAY, 23))
        self.assertEqual(len(all_rows(t)), 3)
        self.assertEqual(t["total_active_s"], 60 * 60)
        self.assertEqual(t["warnings"], ["afk_missing"])
        gov_row = all_rows(t)[0]
        self.assertEqual((gov_row["project_id"], gov_row["project_source"]), (self.gov, "rule"))
        totals = {p["project_id"]: p["seconds"] for p in t["by_project"]}
        # The 10-minute unclassified Chrome read right after Gov24 work belongs to that session.
        self.assertEqual(totals, {self.gov: 2400, None: 1200})

    def test_day_boundary(self):
        with self.db.connect() as conn:
            t = service.timeline(conn, DAY + timedelta(days=1), now=local_ts(DAY, 23) + 86400)
        self.assertEqual([r["event_ids"] for r in all_rows(t)], [[self.e4]])

    def test_manual_assignment_overrides_rule_and_can_be_cleared(self):
        with self.db.connect() as conn:
            service.assign_project(conn, [self.e1, self.e3], self.k8s)
            t = service.timeline(conn, DAY, now=local_ts(DAY, 23))
            self.assertEqual({(r["event_ids"][0], r["project_id"], r["project_source"]) for r in all_rows(t)},
                             {(self.e1, self.k8s, "manual"), (self.e2, None, None), (self.e3, self.k8s, "manual")})
            service.assign_project(conn, [self.e1], None)
            t = service.timeline(conn, DAY, now=local_ts(DAY, 23))
            self.assertEqual(all_rows(t)[0]["project_source"], "rule")

    def test_filters(self):
        with self.db.connect() as conn:
            only_gov = service.timeline(conn, DAY, project=str(self.gov), now=local_ts(DAY, 23))
            unassigned = service.timeline(conn, DAY, project="none", now=local_ts(DAY, 23))
            chrome = service.timeline(conn, DAY, app="chrome.exe", now=local_ts(DAY, 23))
        self.assertEqual([r["event_ids"] for r in all_rows(only_gov)], [[self.e1], [self.e2]])
        self.assertEqual([r["event_ids"] for r in all_rows(unassigned)], [[self.e3]])
        self.assertEqual(len(all_rows(chrome)), 2)
        # Totals always describe the whole day.
        self.assertEqual(only_gov["total_active_s"], 3600)

    def test_assign_validation(self):
        with self.db.connect() as conn:
            with self.assertRaises(service.Invalid):
                service.assign_project(conn, [], self.gov)
            with self.assertRaises(service.NotFound):
                service.assign_project(conn, [self.e1], 9999)

    def test_deleting_project_unassigns(self):
        with self.db.connect() as conn:
            service.assign_project(conn, [self.e3], self.k8s)
            service.delete_project(conn, self.k8s)
            t = service.timeline(conn, DAY, now=local_ts(DAY, 23))
        self.assertEqual(all_rows(t)[2]["project_id"], None)


class NotesTests(ServiceTestCase):
    def test_crud(self):
        with self.db.connect() as conn:
            n = service.create_note(conn, {"day": DAY.isoformat(), "body": " 페이징 버그 확인 ", "project_id": self.gov})
            self.assertEqual(n["body"], "페이징 버그 확인")
            service.update_note(conn, n["id"], {"body": "페이징 버그 수정함"})
            self.assertEqual([x["body"] for x in service.list_notes(conn, DAY)], ["페이징 버그 수정함"])
            service.delete_note(conn, n["id"])
            self.assertEqual(service.list_notes(conn, DAY), [])

    def test_validation(self):
        with self.db.connect() as conn:
            with self.assertRaises(service.Invalid):
                service.create_note(conn, {"day": "어제", "body": "x"})
            with self.assertRaises(service.Invalid):
                service.create_note(conn, {"day": DAY.isoformat(), "body": "   "})
            with self.assertRaises(service.NotFound):
                service.update_note(conn, 999, {"body": "x"})


class SearchTests(ServiceTestCase):
    def test_title_and_app_search(self):
        with self.db.connect() as conn:
            r = service.search(conn, "UserController")
            self.assertEqual([e["title"] for e in r["events"]], ["gov24-api – UserController.java"])
            self.assertEqual(r["events"][0]["project_id"], self.gov)
            self.assertEqual(len(service.search(conn, "IDEA64")["events"]), 2)

    def test_korean_substring(self):
        with self.db.connect() as conn:
            self.assertEqual(len(service.search(conn, "트랜잭션")["events"]), 1)

    def test_like_wildcards_are_literal(self):
        with self.db.connect() as conn:
            self.assertEqual(len(service.search(conn, "100%")["events"]), 1)
            self.assertEqual(len(service.search(conn, "_전파")["events"]), 1)
            self.assertEqual(len(service.search(conn, "%")["events"]), 1)

    def test_date_and_project_filters(self):
        with self.db.connect() as conn:
            self.assertEqual(len(service.search(conn, "gov24", date_from=DAY, date_to=DAY)["events"]), 1)
            self.assertEqual(len(service.search(conn, "gov24", date_from=DAY + timedelta(days=1))["events"]), 1)
            self.assertEqual(len(service.search(conn, "chrome", project=str(self.gov))["events"]), 0)
            self.assertEqual(len(service.search(conn, "chrome", project="none")["events"]), 2)

    def test_notes_and_projects_found(self):
        with self.db.connect() as conn:
            service.create_note(conn, {"day": DAY.isoformat(), "body": "민원 조회 페이징 이슈"})
            service.update_project(conn, self.gov, {"next_action": "페이징 쿼리 리팩터링"})
            r = service.search(conn, "페이징")
            self.assertEqual(len(r["notes"]), 1)
            self.assertEqual([p["id"] for p in r["projects"]], [self.gov])
            r = service.search(conn, "gov24 백엔드")
            self.assertAlmostEqual(r["projects"][0]["last_activity_ts"],
                                   local_ts(DAY + timedelta(days=1), 9, 10), places=3)

    def test_empty_query_rejected(self):
        with self.db.connect() as conn:
            with self.assertRaises(service.Invalid):
                service.search(conn, "  ")


class PrivacyTests(ServiceTestCase):
    def test_exclusion_purges_existing(self):
        with self.db.connect() as conn:
            r = service.add_exclusion(conn, {"field": "title", "pattern": "kubernetes"})
            self.assertEqual(r["purged"], 1)
            self.assertEqual(service.search(conn, "Kubernetes")["events"], [])

    def test_delete_range_and_tombstone(self):
        with self.db.connect() as conn:
            service.create_note(conn, {"day": DAY.isoformat(), "body": "x"})
            service.timeline(conn, DAY, now=local_ts(DAY, 23))
            counts = service.delete_data(conn, {"from": DAY.isoformat(), "to": DAY.isoformat()},
                                         now=local_ts(DAY, 23) + 86400 * 2)
            self.assertEqual(counts, {"window_events": 3, "afk_events": 0, "notes": 1, "sessions": 2, "reviews": 0,
                                      "changes": 0})
            self.assertEqual(len(all_rows(service.timeline(conn, DAY + timedelta(days=1)))), 1)
            self.assertEqual(conn.execute("SELECT kind FROM blocked_ranges").fetchone()[0], "deleted")

    def test_delete_all(self):
        with self.db.connect() as conn:
            counts = service.delete_data(conn, {"all": True}, now=local_ts(DAY, 23) + 86400 * 2)
            self.assertEqual(counts["window_events"], 4)
            self.assertEqual(len(service.list_projects(conn)), 2)  # projects kept

    def test_pause_hides_time_in_timeline(self):
        with self.db.connect() as conn:
            service.pause(conn, now=local_ts(DAY, 9, 10))
            self.assertTrue(service.is_paused(conn))
            service.resume(conn, now=local_ts(DAY, 9, 20))
            self.assertFalse(service.is_paused(conn))
            t = service.timeline(conn, DAY, now=local_ts(DAY, 23))
        self.assertEqual(t["total_active_s"], 50 * 60)


class ProjectTests(ServiceTestCase):
    def test_duplicate_name_rejected_case_insensitive(self):
        with self.db.connect() as conn:
            with self.assertRaises(service.Invalid):
                service.create_project(conn, {"name": "gov24 백엔드"})

    def test_bad_color_and_field(self):
        with self.db.connect() as conn:
            with self.assertRaises(service.Invalid):
                service.create_project(conn, {"name": "x", "color": "red"})
            with self.assertRaises(service.Invalid):
                service.add_rule(conn, self.gov, {"field": "url", "pattern": "x"})

    def test_archived_project_rules_inactive(self):
        with self.db.connect() as conn:
            service.update_project(conn, self.gov, {"archived": True})
            t = service.timeline(conn, DAY, now=local_ts(DAY, 23))
        self.assertIsNone(all_rows(t)[0]["project_id"])


if __name__ == "__main__":
    unittest.main()
