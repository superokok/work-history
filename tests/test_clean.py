import unittest
from pathlib import Path

from dulwich.repo import Repo

from tools.clean_check import markers, scan

ROOT = Path(__file__).resolve().parent.parent


class CleanRepositoryTests(unittest.TestCase):
    def test_no_traces_of_this_pc_in_tracked_files(self):
        # The repository is shown to other people: no user, paths, git identity or data of this PC.
        with Repo(str(ROOT)) as repo:
            tracked = [ROOT / p.decode("utf-8") for p in repo.open_index().paths()]
        problems = scan([f for f in tracked if f.is_file()], ROOT, markers(ROOT))
        self.assertEqual(problems, [])

    def test_scan_flags_paths_and_data_files(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "ok.md").write_text("C:\app\작업 기록.cmd", encoding="utf-8")
            (base / "leak.md").write_text(f"see {Path.home()}\Desktop", encoding="utf-8")
            (base / "work.db").write_bytes(b"")
            problems = scan(sorted(base.iterdir()), base, markers(ROOT))
        self.assertEqual(sorted({p.split(":")[0] for p in problems}), ["leak.md", "work.db"])


if __name__ == "__main__":
    unittest.main()
