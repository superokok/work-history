import unittest
from datetime import date

from workhistory import locate, service

from tests.helpers import TempDB, local_ts

DAY = date(2026, 9, 30)


class TimelinePathTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        with self.db.connect() as conn:
            src = conn.execute("INSERT INTO sources (bucket_id, bucket_created, kind, hostname) "
                               "VALUES ('w', 'c', 'window', 'h')").lastrowid
            events = [
                (1, local_ts(DAY, 9), local_ts(DAY, 9, 20), "idea64.exe", "minwon-api – MinwonRepository.java"),
                (2, local_ts(DAY, 9, 20), local_ts(DAY, 9, 30), "chrome.exe", "GOV-1 - Jira"),
                (3, local_ts(DAY, 9, 30), local_ts(DAY, 9, 40), "idea64.exe", "other – Util.java"),
            ]
            for aw_id, start, end, app, title in events:
                conn.execute("INSERT INTO window_events (source_id, aw_id, start_ts, end_ts, app, title) "
                             "VALUES (?, ?, ?, ?, ?, ?)", (src, aw_id, start, end, app, title))
            for root, files in ((r"C:\work\minwon-api", ["src/MinwonRepository.java", "src/Util.java"]),
                                (r"C:\work\other", ["Util.java"])):
                fid = conn.execute("INSERT INTO watched_folders (path, label, created_at) VALUES (?, 'x', 0)",
                                   (root,)).lastrowid
                conn.executemany("INSERT INTO file_state (folder_id, path, size, mtime_ns, blob_id) "
                                 "VALUES (?, ?, 1, 0, 'b')", [(fid, f) for f in files])

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_work_folder_files_only(self):
        with self.db.connect() as conn:
            t = service.timeline(conn, DAY, now=local_ts(DAY, 23))
        paths = {w["title"]: (w["path"], w["path_kind"]) for a in t["apps"] for w in a["windows"]}
        self.assertEqual(paths["GOV-1 - Jira"], ("", None))  # browser addresses are not read
        java = paths["minwon-api – MinwonRepository.java"]
        self.assertEqual(java[1], "file")
        self.assertTrue(java[0].endswith("MinwonRepository.java") and "minwon-api" in java[0])
        # Util.java is in both folders: the title's project name picks the right one.
        util = paths["other – Util.java"][0]
        self.assertTrue(util.endswith("Util.java") and "other" in util, util)


if __name__ == "__main__":
    unittest.main()
