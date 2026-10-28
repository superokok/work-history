import io
import os
import shutil
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

try:
    from dulwich.repo import Repo
    from workhistory import snapshots
    HAS_DULWICH = True
except ImportError:  # run with the project venv
    HAS_DULWICH = False

from workhistory.store import Invalid

from tests.helpers import TempDB


def docx(paragraphs: list[str]) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f"<w:body>{body}</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


@unittest.skipUnless(HAS_DULWICH, "dulwich not installed (run tests with .venv python)")
class SnapshotTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        self.work = Path(tempfile.mkdtemp(prefix="wh-work-"))
        self.repos = Path(self.db.path).parent / "snapshots"
        self.write("src/Main.java", "class Main {\n  void run() {}\n}\n")
        self.write("README.md", "# 민원 API\n")
        with self.db.connect() as conn:
            self.folder = snapshots.add_folder(conn, {"path": str(self.work), "label": "gov24"}, self.repos)["id"]
        self.t = time.time() - 3600

    def tearDown(self):
        self._tmp.__exit__(None, None, None)
        shutil.rmtree(self.work, ignore_errors=True)

    def write(self, rel: str, content, mtime_shift: float = 0):
        p = self.work / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        if mtime_shift:
            st = p.stat()
            os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + int(mtime_shift * 1e9)))

    def scan(self):
        self.t += 60
        with self.db.connect() as conn:
            return snapshots.scan(conn, self.repos, self.folder, now=self.t)

    def change_files(self, change_id):
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT path, kind, is_text, lines_added, lines_removed FROM change_files WHERE change_id = ? "
                "ORDER BY path", (change_id,))]

    def tree_paths(self):
        repo = Repo(str(snapshots.repo_path(self.repos, self.folder)))
        try:
            head = repo.refs[snapshots.REF]
            out = []

            def walk(tree_id, prefix):
                for entry in repo[tree_id].items():
                    name = entry.path.decode()
                    if entry.mode == snapshots.DIR_MODE:
                        walk(entry.sha, prefix + name + "/")
                    else:
                        out.append(prefix + name)
            walk(repo[head].tree, "")
            return sorted(out)
        finally:
            repo.close()


class ScanTests(SnapshotTestCase):
    def test_baseline_then_changes(self):
        first = self.scan()
        self.assertTrue(first.baseline)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT baseline FROM changes WHERE id = ?",
                                          (first.change_id,)).fetchone()[0], 1)
        self.assertIsNone(self.scan().change_id)  # nothing changed, no new snapshot

        self.write("src/Main.java", "class Main {\n  void run() {\n    refresh();\n  }\n}\n", mtime_shift=2)
        self.write("src/TokenFilter.java", "class TokenFilter {}\n")
        (self.work / "README.md").unlink()
        r = self.scan()
        self.assertFalse(r.baseline)
        self.assertEqual(self.change_files(r.change_id), [
            {"path": "README.md", "kind": "D", "is_text": 1, "lines_added": 0, "lines_removed": 1},
            {"path": "src/Main.java", "kind": "M", "is_text": 1, "lines_added": 3, "lines_removed": 1},
            {"path": "src/TokenFilter.java", "kind": "A", "is_text": 1, "lines_added": 1, "lines_removed": 0},
        ])
        with self.db.connect() as conn:
            c = dict(conn.execute("SELECT * FROM changes WHERE id = ?", (r.change_id,)).fetchone())
        self.assertEqual((c["files_added"], c["files_modified"], c["files_deleted"], c["lines_added"],
                          c["lines_removed"]), (1, 1, 1, 4, 2))

    def test_secrets_build_outputs_and_user_git_never_stored(self):
        self.write(".env", "DB_PASSWORD=hunter2")
        self.write("config/prod.pem", "-----BEGIN PRIVATE KEY-----")
        self.write("node_modules/lib/index.js", "x")
        self.write("build/classes/Main.class", b"\xca\xfe")
        self.write(".git/config", "[core]")
        self.write("app.log", "log")
        self.write("big.txt", b"a" * (snapshots.MAX_FILE_BYTES + 1))
        git_config = self.work / ".git" / "config"
        before = git_config.stat().st_mtime_ns
        self.scan()
        self.assertEqual(self.tree_paths(), ["README.md", "src/Main.java"])
        self.assertEqual(git_config.stat().st_mtime_ns, before)
        self.assertEqual(git_config.read_text(), "[core]")

    def test_more_credential_files_never_stored(self):
        for name in (".netrc", ".npmrc", ".pypirc", ".pgpass", "deploy.ppk", "vpn.ovpn",
                     "kubeconfig", "terraform.tfstate", "terraform.tfstate.backup", "notes.gpg"):
            self.write(name, "x")
        self.scan()
        self.assertEqual(self.tree_paths(), ["README.md", "src/Main.java"])

    @unittest.skipUnless(os.name == "nt", "Windows junctions")
    def test_junction_outside_the_folder_is_not_followed(self):
        import subprocess
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "private.txt").write_text("not part of the work folder")
        link = self.work / "link"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True)
        try:
            self.scan()
        finally:
            os.rmdir(link)  # removes the junction only, before the work folder is deleted
        self.assertEqual(self.tree_paths(), ["README.md", "src/Main.java"])
        self.assertTrue((outside / "private.txt").exists())

    def test_locked_file_is_not_recorded_as_deleted(self):
        self.scan()
        self.write("src/Main.java", "changed\n", mtime_shift=2)
        real = Path.read_bytes

        def locked(path):
            if path.name == "Main.java":
                raise PermissionError("locked by editor")
            return real(path)
        with mock.patch.object(Path, "read_bytes", locked):
            r = self.scan()
        self.assertIsNone(r.change_id)
        self.assertIn("src/Main.java", self.tree_paths())
        r = self.scan()  # unlocked again: the edit is picked up now
        self.assertEqual([f["kind"] for f in self.change_files(r.change_id)], ["M"])

    def test_document_text_changes(self):
        self.write("docs/보고서.docx", docx(["1. 개요", "2. 일정"]))
        self.scan()
        self.write("docs/보고서.docx", docx(["1. 개요", "2. 일정 변경", "3. 위험 요소"]), mtime_shift=2)
        r = self.scan()
        [f] = self.change_files(r.change_id)
        self.assertEqual((f["path"], f["kind"], f["is_text"], f["lines_added"], f["lines_removed"]),
                         ("docs/보고서.docx", "M", 1, 2, 1))
        with self.db.connect() as conn:
            file_id = conn.execute("SELECT id FROM change_files WHERE change_id = ?", (r.change_id,)).fetchone()[0]
            d = snapshots.file_diff(self.repos, conn, file_id)
        self.assertIn("-2. 일정", d["diff"])
        self.assertIn("+3. 위험 요소", d["diff"])

    def test_binary_files_have_no_line_counts(self):
        self.scan()
        self.write("img/logo.png", b"\x89PNG\x00\x00binary")
        [f] = self.change_files(self.scan().change_id)
        self.assertEqual((f["is_text"], f["lines_added"]), (0, None))

    def test_snapshot_is_a_normal_git_repository(self):
        self.scan()
        repo = Repo(str(snapshots.repo_path(self.repos, self.folder)))
        try:
            commit = repo[repo.refs[snapshots.REF]]
            self.assertTrue(commit.message.startswith(b"snapshot "))
            self.assertEqual(commit.author, snapshots.AUTHOR)
        finally:
            repo.close()

    def test_missing_folder_reports_error(self):
        shutil.rmtree(self.work)
        r = self.scan()
        self.assertEqual(r.error, "폴더가 없거나 접근할 수 없습니다")
        with self.db.connect() as conn:
            self.assertEqual(snapshots.get_folder(conn, self.folder)["last_error"], r.error)


class FolderTests(SnapshotTestCase):
    def test_validation(self):
        with self.db.connect() as conn:
            for bad in ("", "relative/path", str(self.work / "nope"), str(self.work / "src"),
                        str(self.work.parent), self.work.anchor):  # anchor: C:\\ or /
                with self.assertRaises(Invalid, msg=bad):
                    snapshots.add_folder(conn, {"path": bad}, self.repos)
            with self.assertRaises(Invalid):  # the app's own data folder
                snapshots.add_folder(conn, {"path": str(Path(self.db.path).parent)}, self.repos)

    def test_remove_deletes_records_and_snapshot_repository(self):
        self.scan()
        repo_dir = snapshots.repo_path(self.repos, self.folder)
        self.assertTrue(repo_dir.exists())
        with self.db.connect() as conn:
            snapshots.remove_folder(conn, self.folder, self.repos)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_state").fetchone()[0], 0)
        self.assertFalse(repo_dir.exists())

    def test_snapshotter_keeps_going_after_a_bad_folder(self):
        other = Path(tempfile.mkdtemp(prefix="wh-work2-"))
        try:
            (other / "a.txt").write_text("a")
            with self.db.connect() as conn:
                snapshots.add_folder(conn, {"path": str(other)}, self.repos)
            shutil.rmtree(self.work)
            results = snapshots.Snapshotter(self.db, self.repos).run_now()
            self.assertEqual(sorted(r.error is None for r in results), [False, True])
        finally:
            shutil.rmtree(other, ignore_errors=True)


class AreaTests(SnapshotTestCase):
    """Projects for sub-folders: suggested by name, saved only when the user confirms."""

    def setUp(self):
        super().setUp()
        with self.db.connect() as conn:
            self.alpha = conn.execute("INSERT INTO projects (name, created_at) VALUES ('Alpha', 0)").lastrowid
            self.beta = conn.execute("INSERT INTO projects (name, created_at) VALUES ('beta-api', 0)").lastrowid
        self.scan()  # baseline
        self.write("alpha/a.py", "a = 1\n")
        self.write("beta_api/b.py", "b = 1\n")
        self.write("common/c.md", "c\n")
        self.write("2026/plan/p.md", "p\n")
        self.write("notes.txt", "n\n")
        self.change = self.scan().change_id

    def projects(self):
        with self.db.connect() as conn:
            files = dict(conn.execute("SELECT path, project_id FROM change_files WHERE change_id = ?",
                                      (self.change,)).fetchall())
            return files, conn.execute("SELECT project_id FROM changes WHERE id = ?", (self.change,)).fetchone()[0]

    def test_nothing_is_assigned_until_confirmed(self):
        with self.db.connect() as conn:
            areas = snapshots.list_areas(conn, self.folder)
            folder = snapshots.get_folder(conn, self.folder)
        self.assertEqual({g["name"]: g["suggested_project_id"] for g in areas["groups"]},
                         {"alpha": self.alpha, "beta_api": self.beta, "common": None, "2026": None})
        self.assertEqual((areas["loose_files"], folder["unassigned_files"], folder["unassigned_changes"]), (1, 5, 1))
        files, change = self.projects()
        self.assertEqual((set(files.values()), change), ({None}, None))  # suggestions are not saved

    def test_confirmed_sub_folders_and_deepest_wins(self):
        with self.db.connect() as conn:
            snapshots.set_area(conn, self.folder, {"subpath": "alpha", "project_id": self.alpha})
            snapshots.set_area(conn, self.folder, {"subpath": "beta_api", "project_id": self.beta})
            folder = snapshots.set_area(conn, self.folder, {"subpath": "common", "project_id": None})
            self.assertEqual(folder["unassigned_files"], 2)  # "no project" is decided, not asked again
            # drilling into a year folder lists the next level
            self.assertEqual([g["subpath"] for g in snapshots.list_areas(conn, self.folder, "2026")["groups"]],
                             ["2026/plan"])
            snapshots.set_area(conn, self.folder, {"subpath": "2026", "project_id": self.beta})
            snapshots.set_area(conn, self.folder, {"subpath": "2026\\plan", "project_id": self.alpha})
        files, change = self.projects()
        self.assertEqual(files, {"alpha/a.py": self.alpha, "beta_api/b.py": self.beta, "common/c.md": None,
                                 "2026/plan/p.md": self.alpha, "notes.txt": None})
        self.assertEqual(change, self.alpha)  # the project most of its files belong to
        # the folder's own project is the default for whatever no sub-folder claims
        with self.db.connect() as conn:
            snapshots.update_folder(conn, self.folder, {"project_id": self.beta})
            snapshots.remove_area(conn, self.folder, {"subpath": "2026/plan"})
            self.assertEqual(snapshots.get_folder(conn, self.folder)["unassigned_files"], 0)
        files, _ = self.projects()
        self.assertEqual((files["notes.txt"], files["common/c.md"], files["2026/plan/p.md"]),
                         (self.beta, None, self.beta))

    def test_new_changes_follow_confirmed_areas_and_queries_use_them(self):
        from datetime import date
        from workhistory import service
        with self.db.connect() as conn:
            snapshots.set_area(conn, self.folder, {"subpath": "alpha", "project_id": self.alpha})
        self.write("alpha/new.py", "x = 2\n")
        later = self.scan().change_id
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT project_id FROM changes WHERE id = ?", (later,)).fetchone()[0],
                             self.alpha)
            found = service.search(conn, "new.py", project=self.alpha)["files"]
            self.assertEqual([f["path"] for f in found], ["alpha/new.py"])
            day = date.fromtimestamp(self.t)
            self.assertIn(self.alpha, [c["project_id"] for c in service.timeline(conn, day)["changes"]])
            service.delete_project(conn, self.alpha)  # its sub-folder is asked about again
            self.assertEqual(snapshots.list_areas(conn, self.folder)["areas"], [])
            self.assertIsNone(conn.execute("SELECT project_id FROM changes WHERE id = ?", (later,)).fetchone()[0])

    def test_validation(self):
        with self.db.connect() as conn:
            for bad in ({"subpath": ""}, {"subpath": "../x"}, {"subpath": 3}):
                with self.assertRaises(Invalid):
                    snapshots.set_area(conn, self.folder, {**bad, "project_id": None})
            with self.assertRaises(snapshots.NotFound):
                snapshots.set_area(conn, self.folder, {"subpath": "alpha", "project_id": 999})
            with self.assertRaises(snapshots.NotFound):
                snapshots.remove_area(conn, self.folder, {"subpath": "alpha"})


class IntegrationTests(SnapshotTestCase):
    def test_timeline_search_and_delete(self):
        from datetime import date
        from workhistory import service
        self.scan()  # baseline: not shown as work
        self.write("src/TokenRefreshFilter.java", "class TokenRefreshFilter {}" + chr(10))
        change = self.scan()
        day = date.fromtimestamp(self.t)
        with self.db.connect() as conn:
            changes = service.timeline(conn, day)["changes"]
            self.assertEqual([(c["id"], c["folder"], [f["path"] for f in c["files"]]) for c in changes],
                             [(change.change_id, "gov24", ["src/TokenRefreshFilter.java"])])
            found = service.search(conn, "TokenRefresh")["files"]
            self.assertEqual([(f["path"], f["kind"]) for f in found], [("src/TokenRefreshFilter.java", "A")])
            counts = service.delete_data(conn, {"all": True}, repos_dir=self.repos)
            self.assertEqual(counts["changes"], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_state").fetchone()[0], 0)
        self.assertTrue(self.scan().baseline)  # fresh start after deleting everything

    def test_folder_api(self):
        import http.client
        import json
        import threading
        from workhistory.server import App, create_server
        snap = snapshots.Snapshotter(self.db, self.repos)
        server = create_server(App(self.db, None, snapshotter=snap, repos_dir=self.repos), 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]

        def call(method, path, body=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            headers = {"Host": f"127.0.0.1:{port}"}
            if body is not None:
                headers.update({"Content-Type": "application/json", "X-Work-History": "1"})
            c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
            r = c.getresponse()
            data = json.loads(r.read())
            c.close()
            return r.status, data
        try:
            status, folders = call("GET", "/api/folders")
            self.assertEqual((status, [f["label"] for f in folders]), (200, ["gov24"]))
            self.assertEqual(call("POST", "/api/folders", {"path": "relative"})[0], 400)
            status, results = call("POST", "/api/folders/scan", {})
            self.assertTrue(results[0]["baseline"])
            self.write("src/Main.java", "class Main {}" + chr(10), mtime_shift=2)
            status, results = call("POST", "/api/folders/scan", {})
            with self.db.connect() as conn:
                file_id = conn.execute("SELECT id FROM change_files WHERE change_id = ?",
                                       (results[0]["change_id"],)).fetchone()[0]
            status, diff = call("GET", f"/api/change-files/{file_id}/diff")
            self.assertEqual(status, 200)
            self.assertIn("+class Main {}", diff["diff"])
            # a C reference opens straight from a citation: the change with its files
            status, change = call("GET", f"/api/changes/{results[0]['change_id']}")
            self.assertEqual((status, change["folder"], [f["id"] for f in change["files"]]),
                             (200, "gov24", [file_id]))
            self.assertEqual(call("GET", "/api/changes/999999")[0], 404)
            with self.db.connect() as conn:
                baseline = conn.execute("SELECT id FROM changes WHERE baseline = 1").fetchone()[0]
            self.assertEqual(call("GET", f"/api/changes/{baseline}")[0], 404)  # the first snapshot is not work
            status, areas = call("GET", f"/api/folders/{self.folder}/areas")
            self.assertEqual((status, [g["subpath"] for g in areas["groups"]]), (200, ["src"]))
            status, folder = call("POST", f"/api/folders/{self.folder}/areas", {"subpath": "src", "project_id": None})
            self.assertEqual((status, folder["unassigned_files"]), (200, 0))
            self.assertEqual(call("GET", f"/api/folders/{self.folder}/areas?under=..")[0], 400)
            self.assertEqual(call("DELETE", f"/api/folders/{self.folder}/areas", {"subpath": "src"})[0], 200)
            self.assertEqual(call("DELETE", f"/api/folders/{self.folder}", {})[0], 200)
            self.assertFalse(snapshots.repo_path(self.repos, self.folder).exists())
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()


class DecompressionBombTests(unittest.TestCase):
    def test_document_that_unpacks_too_large_is_treated_as_binary(self):
        from workhistory import extract
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("word/document.xml", b"<w:document>" + b" " * (extract.MAX_UNPACKED_BYTES + 1))
        self.assertLess(len(buf.getvalue()), 1024 * 1024)  # small on disk
        self.assertIsNone(extract.extract_text("bomb.docx", buf.getvalue()))
        self.assertEqual(extract.extract_text("ok.docx", docx(["안녕"])), "안녕")
