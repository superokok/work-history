import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workhistory import autostart
from workhistory.autostart import Launch
from workhistory.store import Invalid

CMD = ["/opt/작업 기록/start-workhistory.sh"]


class FakePowerShell:
    """Records the environment it was given and 'creates' the shortcut file."""

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, env):
        self.calls.append(env)
        if not self.fail:
            Path(env["WH_LNK"]).write_text("lnk")
        return subprocess.CompletedProcess([], 1 if self.fail else 0, "", "denied" if self.fail else "")


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "", "XDG_DATA_HOME": ""})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def test_windows_shortcuts_go_through_the_shell_with_paths_in_env_vars(self):
        ps = FakePowerShell()
        folders = {"startup": self.home / "Startup", "desktop": self.home / "Desktop"}
        launch = Launch("Windows", self.home, [r"C:\앱\python.exe", "-m", "workhistory"], folders.__getitem__, ps)
        self.assertFalse(launch.status()["autostart"]["enabled"])
        status = launch.set_autostart(True)
        self.assertTrue(status["autostart"]["enabled"])
        env = ps.calls[0]
        self.assertEqual(Path(env["WH_LNK"]), self.home / "Startup" / "작업 기록.lnk")
        self.assertEqual(env["WH_TARGET"], r"C:\앱\python.exe")
        self.assertEqual(env["WH_ARGS"], "-m workhistory --no-browser")  # at logon: no browser
        launch.create_shortcut()
        self.assertEqual(ps.calls[1]["WH_ARGS"], "-m workhistory")
        self.assertTrue(launch.status()["shortcut"]["exists"])
        self.assertFalse(launch.set_autostart(False)["autostart"]["enabled"])
        self.assertFalse((self.home / "Startup" / "작업 기록.lnk").exists())
        self.assertTrue((self.home / "Desktop" / "작업 기록.lnk").exists())  # only autostart was removed

    def test_windows_failure_is_reported(self):
        folders = {"startup": self.home / "Startup", "desktop": self.home / "Desktop"}
        launch = Launch("Windows", self.home, CMD, folders.__getitem__, FakePowerShell(fail=True))
        with self.assertRaises(Invalid):
            launch.set_autostart(True)

    def test_linux_desktop_entries(self):
        launch = Launch("Linux", self.home, CMD + ["--db", 'a "b" $c'])
        launch.set_autostart(True)
        entry = (self.home / ".config" / "autostart" / "workhistory.desktop").read_text(encoding="utf-8")
        self.assertIn('Exec="/opt/작업 기록/start-workhistory.sh" --db "a \\"b\\" \\$c" --no-browser', entry)
        self.assertIn("X-GNOME-Autostart-enabled=true", entry)
        launch.create_shortcut()
        menu = (self.home / ".local" / "share" / "applications" / "workhistory.desktop").read_text(encoding="utf-8")
        self.assertNotIn("--no-browser", menu)
        self.assertEqual(launch.status()["shortcut"]["where"], "프로그램 메뉴")
        launch.set_autostart(False)
        self.assertFalse((self.home / ".config" / "autostart" / "workhistory.desktop").exists())

    def test_macos_launch_agent(self):
        launch = Launch("Darwin", self.home, CMD)
        launch.set_autostart(True)
        plist = plistlib.loads((self.home / "Library" / "LaunchAgents" / "com.workhistory.app.plist").read_bytes())
        self.assertEqual(plist["ProgramArguments"], CMD + ["--no-browser"])
        self.assertTrue(plist["RunAtLoad"])
        self.assertFalse(launch.status()["shortcut"]["supported"])
        with self.assertRaises(Invalid):
            launch.create_shortcut()

    def test_launch_command_prefers_the_launcher(self):
        script = self.home / "start.cmd"
        script.write_text("@echo off")
        with mock.patch.dict(os.environ, {"WORKHISTORY_LAUNCHER": str(script)}):
            self.assertEqual(autostart.launch_command(), [str(script.resolve())])
        with mock.patch.dict(os.environ, {"WORKHISTORY_LAUNCHER": str(self.home / "gone.cmd")}):
            self.assertEqual(autostart.launch_command()[1:], ["-m", "workhistory"])


if __name__ == "__main__":
    unittest.main()
