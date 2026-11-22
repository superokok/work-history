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


class FilePathTests(unittest.TestCase):
    """Title shapes seen on real PCs: Eclipse relative paths, Office titles, names with spaces."""

    ROOT = r"C:\work\project-a"
    FILES = ["app-web/build.gradle", "app-batch/build.gradle",
             "app-web/src/main/java/com/example/UserController.java",
             "app-batch/src/main/java/com/example/UserController.java",
             "docs/교육 자료 v1.3.pptx", "docs/테이블 명세서_v0.4.xlsx", "docs/업무 규칙.hwp",
             "docs/회의록.docx", "docs/회의록.hwp"]

    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        with self.db.connect() as conn:
            fid = conn.execute("INSERT INTO watched_folders (path, label, created_at) VALUES (?, 'x', 0)",
                               (self.ROOT,)).lastrowid
            conn.executemany("INSERT INTO file_state (folder_id, path, size, mtime_ns, blob_id) "
                             "VALUES (?, ?, 1, 0, 'b')", [(fid, f) for f in self.FILES])

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def paths(self, *titles):
        with self.db.connect() as conn:
            return locate.file_paths(conn, list(titles))

    def full(self, rel):
        return self.ROOT + "\\" + rel.replace("/", "\\")

    def test_eclipse_relative_path_picks_the_file(self):
        t1 = "workspace - app-batch/build.gradle - Eclipse IDE"
        t2 = "workspace - app-web/src/main/java/com/example/UserController.java - Eclipse IDE"
        got = self.paths(t1, t2)
        self.assertEqual(got[t1], self.full("app-batch/build.gradle"))
        self.assertEqual(got[t2], self.full("app-web/src/main/java/com/example/UserController.java"))

    def test_copies_of_the_project_are_told_apart_by_the_workspace_name(self):
        with self.db.connect() as conn:
            fid = conn.execute("INSERT INTO watched_folders (path, label, created_at) VALUES (?, 'y', 0)",
                               (r"D:\deploy",)).lastrowid
            conn.execute("INSERT INTO file_state (folder_id, path, size, mtime_ns, blob_id) "
                         "VALUES (?, 'copy/app-batch/build.gradle', 1, 0, 'b')", (fid,))
        t = "project-a - app-batch/build.gradle - Eclipse IDE"  # both paths end in app-batch\build.gradle
        self.assertEqual(self.paths(t)[t], self.full("app-batch/build.gradle"))
        # Without a word that tells them apart, nothing is guessed.
        self.assertEqual(self.paths("workspace - app-batch/build.gradle - Eclipse IDE"), {})

    def test_relative_path_must_match_the_end_of_the_path(self):
        # "other/build.gradle" is not the end of either path, and no title word narrows it: no guess.
        self.assertEqual(self.paths("workspace - other/build.gradle - Eclipse IDE"), {})

    def test_names_with_spaces(self):
        t1 = "테이블 명세서_v0.4.xlsx - Excel"
        t2 = r"업무 규칙.hwp [Z:\shared\docs\] - 한글"
        got = self.paths(t1, t2)
        self.assertEqual(got[t1], self.full("docs/테이블 명세서_v0.4.xlsx"))
        self.assertEqual(got[t2], self.full("docs/업무 규칙.hwp"))

    def test_office_title_without_extension(self):
        t1, t2 = "교육 자료 v1.3 - PowerPoint", "회의록 - Word"
        got = self.paths(t1, t2)
        self.assertEqual(got[t1], self.full("docs/교육 자료 v1.3.pptx"))
        self.assertEqual(got[t2], self.full("docs/회의록.docx"))  # Word's extensions only, not 회의록.hwp
        self.assertEqual(self.paths("없는 문서 - PowerPoint", "교육 자료 v1.3 - 메모장"), {})


if __name__ == "__main__":
    unittest.main()
