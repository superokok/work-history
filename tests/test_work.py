import time
import unittest
from datetime import date

from workhistory import work
from workhistory.apps import app_label, clean_title
from workhistory.normalize import Activity

from tests.helpers import TempDB, local_ts

M = 60
DAY = date(2026, 9, 30)


def A(start_min, end_min, app="idea64.exe", title="project-a – A.java"):
    return Activity(int(start_min), start_min * M, end_min * M, app, title)


class AppTests(unittest.TestCase):
    def test_clean_title(self):
        self.assertEqual(clean_title("GitHub - Google Chrome", "chrome.exe"), "GitHub")
        self.assertEqual(clean_title("main.py - proj - Visual Studio Code", "Code.exe"), "main.py - proj")
        self.assertEqual(clean_title("gov24-api – UserController.java", "idea64.exe"), "gov24-api – UserController.java")
        self.assertEqual(clean_title("", "x.exe"), "")

    def test_app_label(self):
        self.assertEqual(app_label("idea64.exe"), "IntelliJ IDEA")
        self.assertEqual(app_label("SomeTool.EXE"), "SomeTool")


class RecordTests(unittest.TestCase):
    """Pure grouping: windows already matched to folders (folder_of)."""

    A_TITLE = ("idea64.exe", "project-a – A.java")
    B_TITLE = ("Code.exe", "b.yaml - project-b - Visual Studio Code")

    def records(self, acts):
        return work.records(DAY, acts, {self.A_TITLE: 1, self.B_TITLE: 2}, {1: "project-a", 2: "project-b"})

    def test_switching_back_and_forth_adds_up_per_folder(self):
        acts = [A(0, 20), A(20, 30, *self.B_TITLE), A(30, 70), A(70, 85, *self.B_TITLE),
                A(85, 90, "slack.exe", "#dev - Slack")]
        recs, offwork = self.records(acts)
        self.assertEqual([(r.name, r.seconds / M) for r in recs],
                         [("project-a", 60), ("project-b", 25), ("소통", 5)])
        # Times of day: the 10-minute switch to project-b does not cut project-a's span.
        self.assertEqual([(a / M, b / M) for a, b in recs[0].spans], [(0, 70)])
        self.assertEqual(offwork, 0)

    def test_unknown_windows_go_by_kind_not_to_a_neighbour(self):
        acts = [A(0, 30), A(30, 40, "chrome.exe", "Stack Overflow - Google Chrome"),
                A(40, 45, "Outlook.exe", "받은 편지함"), A(45, 50, "dbeaver.exe", "DBeaver"),
                A(50, 60, "", "")]  # 업무 외: time only
        recs, offwork = self.records(acts)
        self.assertEqual([(r.name, r.folder_id, r.seconds / M) for r in recs],
                         [("project-a", 1, 30), ("웹", None, 10), ("소통", None, 5), ("기타", None, 5)])
        self.assertEqual(offwork, 10 * M)

    def test_long_gap_starts_a_new_span(self):
        recs, _ = self.records([A(0, 30), A(60, 90)])
        self.assertEqual([(a / M, b / M) for a, b in recs[0].spans], [(0, 30), (60, 90)])
        self.assertEqual(recs[0].seconds, 60 * M)

    def test_windows_by_time(self):
        other = ("idea64.exe", "project-a – B.java")
        recs, _ = work.records(DAY, [A(0, 10), A(10, 40, *other)], {self.A_TITLE: 1, other: 1}, {1: "project-a"})
        self.assertEqual([(w, s / M) for w, s in recs[0].top_windows(5)],
                         [(("IntelliJ IDEA", "project-a – B.java"), 30), (("IntelliJ IDEA", "project-a – A.java"), 10)])

    def test_empty(self):
        self.assertEqual(self.records([]), ([], 0))


class FolderMatcherTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        with self.db.connect() as conn:
            self.a = self.folder(conn, r"C:\app\project-a", "project-a", ["src/Main.java", "README.md"])
            self.b = self.folder(conn, r"C:\app\docs-b", "docs-b", ["검토보고서.hwp", "charts/values.yaml"])
            self.folder(conn, r"C:\app\docs-c", "docs-c", ["README.md"])

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def folder(self, conn, path, label, files):
        fid = conn.execute("INSERT INTO watched_folders (path, label, created_at) VALUES (?, ?, ?)",
                           (path, label, time.time())).lastrowid
        for f in files:
            conn.execute("INSERT INTO file_state (folder_id, path, size, mtime_ns, blob_id) VALUES (?, ?, 0, 0, '')",
                         (fid, f))
        return fid

    def match(self, *windows):
        with self.db.connect() as conn:
            return work.FolderMatcher(conn).match(set(windows))

    def test_a_path_under_a_work_folder(self):
        cases = {
            ("idea64.exe", "project-a – Main.java"): self.a,                  # its file is in the folder
            ("Hwp.exe", "검토보고서.hwp - 한글"): self.b,                       # a document too
            ("WindowsTerminal.exe", r"PS C:\app\docs-b> make"): self.b,       # the folder's path
            ("cmd.exe", r"C:\APP\DOCS-B\charts 명령 프롬프트"): self.b,          # inside it, any case
        }
        self.assertEqual(self.match(*cases), cases)

    def test_names_alone_do_not_count(self):
        self.assertEqual(self.match(
            ("Code.exe", "notes.txt - project-a - Visual Studio Code"),  # folder name, no such file
            ("cmd.exe", "helm upgrade ./charts -n dev"),                 # relative: from where?
            ("WindowsTerminal.exe", r"PS C:\app\project-ab>"),           # another folder's path
            ("notepad.exe", "README.md - 메모장"),                        # a file in two folders
            ("chrome.exe", r"C:\app\project-a - Google Chrome"),         # browsers are 웹
            ("slack.exe", "project-a - Slack"),                           # messengers are 소통
            ("dbeaver.exe", "DBeaver 23.1 - prod"),                       # nothing to go by
        ), {})

    def test_nested_work_folders_go_to_the_deepest(self):
        with self.db.connect() as conn:
            inner = self.folder(conn, r"C:\app\project-a\module-x", "module-x", ["Inner.java"])
        self.assertEqual(self.match(("idea64.exe", "Inner.java"), ("pwsh.exe", r"PS C:\app\project-a\module-x>"),
                                    ("pwsh.exe", r"PS C:\app\project-a>")),
                         {("idea64.exe", "Inner.java"): inner, ("pwsh.exe", r"PS C:\app\project-a\module-x>"): inner,
                          ("pwsh.exe", r"PS C:\app\project-a>"): self.a})

    def test_day_records_from_the_database(self):
        with self.db.connect() as conn:
            src = conn.execute("INSERT INTO sources (bucket_id, bucket_created, kind, hostname) "
                               "VALUES ('w', 'c', 'window', 'h')").lastrowid
            for i, (h1, h2, app, title) in enumerate([(9, 10, "idea64.exe", "project-a – Main.java"),
                                                     (10, 11, "chrome.exe", "Jira - Google Chrome"),
                                                     (11, 12, "idea64.exe", "project-a – Main.java")]):
                conn.execute("INSERT INTO window_events (source_id, aw_id, start_ts, end_ts, app, title) "
                             "VALUES (?, ?, ?, ?, ?, ?)", (src, i, local_ts(DAY, h1), local_ts(DAY, h2), app, title))
            recs, offwork = work.day(conn, DAY, now=local_ts(DAY, 23))
        self.assertEqual([(r.name, r.seconds / 3600, len(r.spans)) for r in recs],
                         [("project-a", 2, 2), ("웹", 1, 1)])


if __name__ == "__main__":
    unittest.main()
