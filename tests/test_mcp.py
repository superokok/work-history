import asyncio
import importlib.util
import json
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests.helpers import TempDB

HAS_MCP = importlib.util.find_spec("mcp") is not None and importlib.util.find_spec("dulwich") is not None


def text_of(result) -> dict:
    return json.loads(result.content[0].text)


@unittest.skipUnless(HAS_MCP, "mcp/dulwich not installed (run tests with .venv python)")
class MCPServerTests(unittest.TestCase):
    def setUp(self):
        from workhistory import demo
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        self.repos = Path(self.db.path).parent / "snapshots"
        demo.seed(self.db, self.repos)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def call(self, demo_mode: bool, name: str, args: dict):
        from mcp import Client
        from workhistory import mcp_server

        async def go():
            async with Client(mcp_server.build(self.db, self.repos, demo_mode=demo_mode)) as c:
                return await c.call_tool(name, args)
        return asyncio.run(go())

    def test_tools_are_read_only(self):
        from mcp import Client
        from workhistory import mcp_server

        async def go():
            async with Client(mcp_server.build(self.db, self.repos, demo_mode=True)) as c:
                return await c.list_tools()
        tools = asyncio.run(go()).tools
        self.assertEqual({t.name for t in tools}, {"search_history", "get_day", "list_projects", "get_project",
                                                   "get_change", "get_report_data"})
        self.assertTrue(all(t.annotations.read_only_hint and not t.annotations.destructive_hint for t in tools))

    def test_search_and_change_detail(self):
        found = text_of(self.call(True, "search_history", {"query": "TokenRefreshFilter"}))
        change = next(r for r in found["results"] if r["type"] == "파일 변경")
        detail = text_of(self.call(True, "get_change", {"ref": change["ref"]}))
        self.assertTrue(detail["files"])
        self.assertIn("diff_excerpt", detail)

    def test_real_data_needs_consent_in_the_app(self):
        out = text_of(self.call(False, "list_projects", {}))
        self.assertIn("Claude Code 연결", out["error"])
        with self.db.connect() as conn:
            conn.execute("INSERT INTO meta (key, value) VALUES ('mcp_enabled', '1')")
        out = text_of(self.call(False, "list_projects", {}))
        self.assertEqual({p["name"] for p in out["projects"]}, {"Gov24 민원 API", "K8s 전환 PoC"})

    def test_report_data_carries_rules_and_refs(self):
        monday = date.today() - timedelta(days=date.today().weekday())
        out = text_of(self.call(True, "get_report_data", {"kind": "weekly",
                                                          "date": (monday - timedelta(days=7)).isoformat()}))
        self.assertEqual(out["kind"], "주간보고")
        self.assertIn("refs", out["rules"])
        self.assertTrue(all(c["ref"].startswith("C") for c in out["data"]["changes"]))
        bad = text_of(self.call(True, "get_report_data", {"kind": "monthly", "date": "2026-09-01"}))
        self.assertIn("error", bad)
        bad = text_of(self.call(True, "get_day", {"date": "어제"}))
        self.assertIn("error", bad)


@unittest.skipUnless(HAS_MCP, "mcp/dulwich not installed (run tests with .venv python)")
class StdioTransportTests(unittest.TestCase):
    """The real transport: a subprocess speaking MCP over stdin/stdout, as Claude Code runs it."""

    def test_stdio_round_trip(self):
        from mcp import Client, StdioServerParameters
        with TempDB() as db:
            project_root = Path(__file__).resolve().parents[1]
            params = StdioServerParameters(command=sys.executable,
                                           args=["-m", "workhistory.mcp_server", "--demo", "--db", db.path],
                                           cwd=str(project_root))

            async def go():
                async with Client(params, read_timeout_seconds=60) as c:
                    tools = await c.list_tools()
                    result = await c.call_tool("list_projects", {})
                    return tools, result
            tools, result = asyncio.run(go())
        self.assertEqual(len(tools.tools), 6)
        self.assertEqual({p["name"] for p in text_of(result)["projects"]}, {"Gov24 민원 API", "K8s 전환 PoC"})


if __name__ == "__main__":
    unittest.main()
