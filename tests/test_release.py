"""tools/release_parts.py: release files and the setup script that puts them back together."""

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import release_parts  # noqa: E402


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.app_zip = self.dir / "WorkHistory-9.9.9-windows-x64-offline.zip"
        with zipfile.ZipFile(self.app_zip, "w") as z:
            z.writestr("WorkHistory/runtime/pythonw.exe", b"fake")
            z.writestr("WorkHistory/llm/models/readme.txt", b"models go here")
        self.models = self.dir / "models"
        self.models.mkdir()
        self.big = os.urandom(2500)  # 3 parts of 1000 bytes
        (self.models / "Big-Q4.gguf").write_bytes(self.big)
        (self.models / "Small-Q4.gguf").write_bytes(b"small model")  # 1 part
        self.out = self.dir / "release"
        self.files = release_parts.make_release(self.app_zip, self.models, self.out, part_bytes=1000)

    def tearDown(self):
        self.tmp.cleanup()

    def test_release_files(self):
        self.assertEqual([f.name for f in self.files], [
            "Big-Q4.gguf.001", "Big-Q4.gguf.002", "Big-Q4.gguf.003", "Small-Q4.gguf.001",
            "WorkHistory-9.9.9-windows-x64-offline.zip", "WorkHistory-setup.cmd"])
        self.assertEqual(b"".join((self.out / f"Big-Q4.gguf.00{n}").read_bytes() for n in (1, 2, 3)), self.big)
        script = (self.out / "WorkHistory-setup.cmd").read_bytes()
        self.assertTrue(script.isascii())
        self.assertNotIn(b"\n", script.replace(b"\r\n", b""))  # CRLF only
        self.assertIn(hashlib.sha256(self.big).hexdigest().encode(), script)
        self.assertIn(b'copy /b "Big-Q4.gguf.001"+"Big-Q4.gguf.002"+"Big-Q4.gguf.003"', script)

    def run_setup(self):
        return subprocess.run(["cmd", "/c", str(self.out / "WorkHistory-setup.cmd")], cwd=self.out,
                              env={**os.environ, "WH_UNATTENDED": "1"}, capture_output=True, text=True, timeout=120)

    @unittest.skipUnless(sys.platform == "win32", "the setup script is a Windows .cmd")
    def test_setup_unpacks_app_and_joins_models(self):
        (self.out / "Small-Q4.gguf.001").unlink()  # not downloaded: optional
        r = self.run_setup()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        app = self.out / "WorkHistory"
        self.assertTrue((app / "runtime" / "pythonw.exe").is_file())
        self.assertEqual((app / "llm" / "models" / "Big-Q4.gguf").read_bytes(), self.big)
        self.assertFalse((app / "llm" / "models" / "Small-Q4.gguf").exists())
        self.assertIn("Not downloaded, optional: Small-Q4.gguf", r.stdout)

        # Run again (an update): the app is unpacked over, the joined model is kept.
        r = self.run_setup()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Already in place: Big-Q4.gguf", r.stdout)

    @unittest.skipUnless(sys.platform == "win32", "the setup script is a Windows .cmd")
    def test_setup_reports_missing_and_damaged_parts(self):
        (self.out / "Big-Q4.gguf.002").unlink()
        damaged = self.out / "Small-Q4.gguf.001"
        damaged.write_bytes(b"other bytes")
        r = self.run_setup()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)  # the app itself is set up
        self.assertIn("Some parts of Big-Q4.gguf are missing", r.stdout)
        self.assertIn("Small-Q4.gguf is damaged", r.stdout)
        self.assertFalse((self.out / "WorkHistory" / "llm" / "models" / "Small-Q4.gguf").exists())
        self.assertIn("No AI model yet", r.stdout)


if __name__ == "__main__":
    unittest.main()
