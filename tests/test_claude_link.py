import subprocess
import tempfile
import unittest
from pathlib import Path

from workhistory import ai_service
from workhistory.claude_link import MODULE, ClaudeLink, ClaudeLinkError, console_python
from workhistory.store import Invalid

from tests.helpers import TempDB


class FakeClaude:
    """Mimics `claude mcp add|remove|get` and `claude --version` with an in-memory registry."""

    def __init__(self, fail_add=False):
        self.servers: dict[str, list[str]] = {}
        self.calls: list[list[str]] = []
        self.fail_add = fail_add

    def __call__(self, args, timeout):
        self.calls.append(args)
        rest = args[1:]

        def done(code=0, out="", err=""):
            return subprocess.CompletedProcess(args, code, out, err)
        if rest == ["--version"]:
            return done(out="2.1.268 (Claude Code)\n")
        if rest[:2] == ["mcp", "get"]:
            name = rest[2]
            if name not in self.servers:
                return done(1, f'No MCP server named "{name}".')
            return done(out=f"{name}:\n  Scope: User config\n  Command: {' '.join(self.servers[name])}\n")
        if rest[:2] == ["mcp", "add"]:
            if self.fail_add:
                return done(1, err="permission denied")
            split = rest.index("--")
            name = rest[split - 1]
            self.servers[name] = rest[split + 1:]
            return done(out=f"Added stdio MCP server {name}")
        if rest[:2] == ["mcp", "remove"]:
            self.servers.pop(rest[-1], None)
            return done(out="Removed")
        return done(1, err="unknown")


def link(fake, demo=False, python="C:\\app\\.venv\\Scripts\\python.exe"):
    return ClaudeLink(demo=demo, cli="claude", runner=fake, python=python)


class ClaudeLinkTests(unittest.TestCase):
    def test_register_and_unregister(self):
        fake = FakeClaude()
        l = link(fake)
        self.assertEqual(l.status(), {"installed": True, "version": "2.1.268 (Claude Code)", "name": "workhistory",
                                      "registered": False, "ours": False})
        status = l.register()
        self.assertTrue(status["registered"] and status["ours"])
        add = next(c for c in fake.calls if c[1:3] == ["mcp", "add"])
        self.assertEqual(add[add.index("--") + 1:], ["C:\\app\\.venv\\Scripts\\python.exe", "-m", MODULE])
        self.assertIn("--scope", add)
        self.assertEqual(add[add.index("--scope") + 1], "user")
        self.assertFalse(l.unregister()["registered"])

    def test_reregister_updates_the_path(self):
        fake = FakeClaude()
        link(fake, python="C:\\old\\python.exe").register()
        link(fake, python="C:\\new\\python.exe").register()
        self.assertEqual(fake.servers["workhistory"][0], "C:\\new\\python.exe")

    def test_demo_uses_its_own_name(self):
        fake = FakeClaude()
        link(fake, demo=True).register()
        self.assertEqual(fake.servers["workhistory-demo"][-1], "--demo")
        self.assertNotIn("workhistory", fake.servers)

    def test_foreign_server_with_same_name_is_never_touched(self):
        fake = FakeClaude()
        fake.servers["workhistory"] = ["npx", "someone-elses-server"]
        with self.assertRaises(ClaudeLinkError):
            link(fake).register()
        link(fake).unregister()
        self.assertEqual(fake.servers["workhistory"], ["npx", "someone-elses-server"])

    def test_not_installed_and_unsafe_paths(self):
        missing = ClaudeLink(cli="", runner=FakeClaude())
        missing.cli = None
        self.assertEqual(missing.status()["installed"], False)
        with self.assertRaises(ClaudeLinkError):
            missing.register()
        with self.assertRaises(ClaudeLinkError):
            # Through a .cmd shim, cmd.exe would misread '&'.
            ClaudeLink(cli="claude.cmd", runner=FakeClaude(), python="C:\\a&b\\python.exe").register()
        fake = FakeClaude()
        ClaudeLink(cli="claude.exe", runner=fake, python="C:\\a&b\\python.exe").register()  # passed verbatim
        self.assertEqual(fake.servers["workhistory"][0], "C:\\a&b\\python.exe")

    def test_windowless_python_is_swapped_for_the_console_one(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "python.exe").touch()
            self.assertEqual(console_python(str(Path(d) / "pythonw.exe")), str(Path(d) / "python.exe"))
            self.assertEqual(console_python(str(Path(d) / "python3")), str(Path(d) / "python3"))


class ConsentSwitchTests(unittest.TestCase):
    def test_switch_keeps_consent_and_registration_in_step(self):
        fake = FakeClaude()
        with TempDB() as db, db.connect() as conn:
            m = ai_service.get_mcp(conn, False, link(fake))
            self.assertEqual((m["enabled"], m["claude"]["registered"]), (False, False))
            m = ai_service.set_mcp(conn, {"enabled": True}, False, link(fake))
            self.assertEqual((m["enabled"], m["claude"]["registered"], m["claude"]["ours"]), (True, True, True))
            m = ai_service.set_mcp(conn, {"enabled": False}, False, link(fake))
            self.assertEqual((m["enabled"], m["claude"]["registered"]), (False, False))

    def test_failed_registration_rolls_back_consent(self):
        with TempDB() as db, db.connect() as conn:
            with self.assertRaises(Invalid):
                ai_service.set_mcp(conn, {"enabled": True}, False, link(FakeClaude(fail_add=True)))
            self.assertFalse(ai_service.get_mcp(conn, False, link(FakeClaude()))["enabled"])

    def test_turning_off_withdraws_consent_even_if_removal_fails(self):
        class BrokenRemove(FakeClaude):
            def __call__(self, args, timeout):
                if args[1:3] == ["mcp", "remove"]:
                    return subprocess.CompletedProcess(args, 1, "", "locked")
                return super().__call__(args, timeout)
        fake = BrokenRemove()
        with TempDB() as db:
            with db.connect() as conn:
                ai_service.set_mcp(conn, {"enabled": True}, False, link(fake))
            with self.assertRaises(Invalid):
                with db.connect() as conn:
                    ai_service.set_mcp(conn, {"enabled": False}, False, link(fake))
            with db.connect() as conn:
                self.assertFalse(ai_service.get_mcp(conn, False, link(fake))["enabled"])


if __name__ == "__main__":
    unittest.main()
