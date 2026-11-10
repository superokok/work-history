import unittest
from datetime import date

from workhistory import locate, service

from tests.helpers import TempDB, local_ts

DAY = date(2026, 9, 30)


class CleanUrlTests(unittest.TestCase):
    def test_query_fragment_and_user_info_dropped(self):
        self.assertEqual(locate.clean_url(" https://a:b@git.example.com/x/y?private_token=1#L3 "),
                         "https://git.example.com/x/y")
        self.assertEqual(locate.clean_url("jira.example.com/browse/GOV-1?x=1"), "jira.example.com/browse/GOV-1")
        self.assertEqual(locate.clean_url("127.0.0.1:5730"), "127.0.0.1:5730")

    def test_typed_search_is_not_an_address(self):
        self.assertEqual(locate.clean_url("spring refresh token"), "")
        self.assertEqual(locate.clean_url("kubernetes"), "")
        self.assertEqual(locate.clean_url(""), "")

    def test_kind(self):
        self.assertEqual(locate.location_kind("https://a.example.com"), "url")
        self.assertEqual(locate.location_kind(r"C:\work\a.java"), "file")
        self.assertIsNone(locate.location_kind(""))


class TimelinePathTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        with self.db.connect() as conn:
            src = conn.execute("INSERT INTO sources (bucket_id, bucket_created, kind, hostname) "
                               "VALUES ('w', 'c', 'window', 'h')").lastrowid
            events = [
                (1, local_ts(DAY, 9), local_ts(DAY, 9, 20), "idea64.exe", "minwon-api – MinwonRepository.java", ""),
                (2, local_ts(DAY, 9, 20), local_ts(DAY, 9, 30), "chrome.exe", "GOV-1 - Jira",
                 "https://jira.example.com/browse/GOV-1"),
                (3, local_ts(DAY, 9, 30), local_ts(DAY, 9, 40), "idea64.exe", "other – Util.java", ""),
            ]
            for aw_id, start, end, app, title, path in events:
                conn.execute("INSERT INTO window_events (source_id, aw_id, start_ts, end_ts, app, title, path) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?)", (src, aw_id, start, end, app, title, path))
            for root, files in ((r"C:\work\minwon-api", ["src/MinwonRepository.java", "src/Util.java"]),
                                (r"C:\work\other", ["Util.java"])):
                fid = conn.execute("INSERT INTO watched_folders (path, label, created_at) VALUES (?, 'x', 0)",
                                   (root,)).lastrowid
                conn.executemany("INSERT INTO file_state (folder_id, path, size, mtime_ns, blob_id) "
                                 "VALUES (?, ?, 1, 0, 'b')", [(fid, f) for f in files])

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_browser_address_and_work_folder_files(self):
        with self.db.connect() as conn:
            t = service.timeline(conn, DAY, now=local_ts(DAY, 23))
        paths = {w["title"]: (w["path"], w["path_kind"]) for a in t["apps"] for w in a["windows"]}
        self.assertEqual(paths["GOV-1 - Jira"], ("https://jira.example.com/browse/GOV-1", "url"))
        java = paths["minwon-api – MinwonRepository.java"]
        self.assertEqual(java[1], "file")
        self.assertTrue(java[0].endswith("MinwonRepository.java") and "minwon-api" in java[0])
        # Util.java is in both folders: the title's project name picks the right one.
        util = paths["other – Util.java"][0]
        self.assertTrue(util.endswith("Util.java") and "other" in util, util)


if __name__ == "__main__":
    unittest.main()
