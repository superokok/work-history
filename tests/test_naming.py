import unittest
from datetime import date

from workhistory import categories, dashboard, naming, service, store, work
from workhistory.store import Invalid
from tests.helpers import TempDB, local_ts

DAY = date(2026, 9, 30)


class NamingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        with self.db.connect() as conn:
            self.src = conn.execute("INSERT INTO sources (bucket_id, bucket_created, kind, hostname) "
                                    "VALUES ('w', 'c', 'window', 'h')").lastrowid
            self.aw = 0
            self.window(conn, 9, 60, "javaw.exe", "ws - project-a/src/App.java - Eclipse IDE")
            self.window(conn, 10, 30, "javaw.exe", "ws - project-a/src/Repo.java - Eclipse IDE")
            self.window(conn, 11, 20, "javaw.exe", "gov24_dev - DBeaver 24.1")
            self.window(conn, 12, 10, "notepad.exe", "메모.txt - 메모장")

    def tearDown(self):
        categories.set_renamed({})
        self._tmp.__exit__(None, None, None)

    def window(self, conn, hour, minutes, app, title):
        self.aw += 1
        conn.execute("INSERT INTO window_events (source_id, aw_id, start_ts, end_ts, app, title) VALUES (?, ?, ?, ?, ?, ?)",
                     (self.src, self.aw, local_ts(DAY, hour), local_ts(DAY, hour) + minutes * 60, app, title))

    def by_app(self):
        with self.db.connect() as conn:
            return {a["app"]: round(a["seconds"] / 60) for a in service.timeline(conn, DAY, now=local_ts(DAY, 23))["by_app"]}

    def kinds(self):
        with self.db.connect() as conn:
            d = dashboard.dashboard(conn, DAY, now=local_ts(DAY, 23))
        return {k: round(sec / 60) for k, sec in d["by_kind"].items() if sec}

    def test_without_names_javaw_is_javaw_and_other(self):
        self.assertEqual(self.by_app(), {"javaw.exe": 110, "notepad.exe": 10})
        self.assertEqual(self.kinds(), {"other": 110, "doc": 10})

    def test_names_by_title_split_one_executable(self):
        with self.db.connect() as conn:
            naming.save_name(conn, {"app": "javaw.exe", "contains": "Eclipse IDE", "label": "Eclipse", "category": "dev"})
            naming.save_name(conn, {"app": "javaw", "contains": "dbeaver", "label": "DBeaver", "category": "dev"})
        self.assertEqual(self.by_app(), {"Eclipse": 90, "DBeaver": 20, "notepad.exe": 10})
        self.assertEqual(self.kinds(), {"dev": 110, "doc": 10})
        with self.db.connect() as conn:  # the AI's data and the dashboard's records use the name too
            [records] = [recs for _, recs, _ in work.period(conn, DAY, DAY, now=local_ts(DAY, 23))]
            names = {app for r in records for (app, _), _ in r.top_windows(None)}
            self.assertEqual(names, {"Eclipse", "DBeaver", "notepad"})
            raw = conn.execute("SELECT DISTINCT app FROM window_events").fetchall()
        self.assertIn("javaw.exe", {r[0] for r in raw})  # the recorded windows keep the executable

    def test_a_kind_only_and_removing_a_name(self):
        with self.db.connect() as conn:
            rule = naming.save_name(conn, {"app": "javaw", "label": "", "category": "dev"})
        self.assertEqual(self.kinds(), {"dev": 110, "doc": 10})
        self.assertEqual(self.by_app(), {"javaw": 110, "notepad.exe": 10})
        with self.db.connect() as conn:
            naming.save_name(conn, {"app": "javaw", "label": "Java 앱", "category": "dev"})  # same rule, updated
            self.assertEqual(len(naming.list_names(conn, now=local_ts(DAY, 23))["rules"]), 1)
            naming.delete_name(conn, rule["id"])
        self.assertEqual(self.by_app(), {"javaw.exe": 110, "notepad.exe": 10})
        self.assertEqual(self.kinds(), {"other": 110, "doc": 10})

    def test_invalid_names(self):
        with self.db.connect() as conn:
            for data in ({"app": ""}, {"app": "javaw"}, {"app": "javaw", "label": "x", "category": "offwork"},
                         {"app": "javaw", "label": "x" * 61}):
                with self.assertRaises(Invalid):
                    naming.save_name(conn, data)
            with self.assertRaises(store.NotFound):
                naming.delete_name(conn, 999)

    def test_the_app_list(self):
        with self.db.connect() as conn:
            naming.save_name(conn, {"app": "javaw", "contains": "Eclipse IDE", "label": "Eclipse", "category": "dev"})
            out = naming.list_names(conn, now=local_ts(DAY, 23))
        javaw = next(a for a in out["apps"] if a["key"] == "javaw")
        self.assertEqual((javaw["launcher"], javaw["label"], javaw["category"], javaw["seconds"]),
                         (True, "javaw", "other", 110 * 60))
        self.assertEqual(javaw["titles"][0]["title"], "ws - project-a/src/App.java - Eclipse IDE")
        self.assertEqual([r["label"] for r in javaw["rules"]], ["Eclipse"])
        self.assertNotIn("suggestions", javaw)
        notepad = next(a for a in out["apps"] if a["key"] == "notepad")
        self.assertFalse(notepad["launcher"])
        self.assertEqual([k["key"] for k in out["kinds"]], ["dev", "doc", "comm", "web", "other"])


if __name__ == "__main__":
    unittest.main()
