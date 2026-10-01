import asyncio
import importlib.util
import json
import subprocess
import unittest
from pathlib import Path

from workhistory import ai_agent, ai_service
from workhistory.ai_claude import ClaudeCodeModel
from workhistory.ai_client import AIError, Usage

from tests.helpers import TempDB

HAS_MCP = importlib.util.find_spec("mcp") is not None and importlib.util.find_spec("dulwich") is not None


def result_json(**fields) -> str:
    base = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 2, "result": "",
            "usage": {"input_tokens": 100, "cache_read_input_tokens": 20, "cache_creation_input_tokens": 5,
                      "output_tokens": 50, "output_tokens_details": {"thinking_tokens": 30}}}
    return json.dumps({**base, **fields}, ensure_ascii=False)


class FakeCLI:
    """Stands in for `claude`: records each call and replies with a canned stdout."""

    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr
        self.calls = []

    def __call__(self, args, *, input, cwd, timeout, env):
        self.calls.append({"args": args, "input": input, "cwd": cwd, "env": env})
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, self.stderr)


def model(fake, name="claude-code:sonnet"):
    return ClaudeCodeModel(name, cli="claude.exe", runner=fake)


class ClaudeCodeModelTests(unittest.TestCase):
    def test_structured_output_and_locked_down_run(self):
        fake = FakeCLI(result_json(structured_output={"summary": "요약", "projects": []}))
        data, usage = model(fake).generate_json("규칙", "자료 JSON", {"type": "object"})
        self.assertEqual(data, {"summary": "요약", "projects": []})
        self.assertEqual(usage, Usage(input_tokens=125, output_tokens=20, thinking_tokens=30, requests=2))
        call = fake.calls[0]
        args = call["args"]
        self.assertEqual(call["input"], "자료 JSON")  # the prompt goes through stdin, not the command line
        self.assertEqual(args[args.index("--tools") + 1], "")  # no files, shell or web
        self.assertIn("--strict-mcp-config", args)
        self.assertNotIn("--mcp-config", args)
        self.assertIn("--no-session-persistence", args)
        self.assertEqual(args[args.index("--setting-sources") + 1], "project")
        self.assertEqual(args[args.index("--model") + 1], "sonnet")
        self.assertEqual(args[args.index("--system-prompt") + 1], "규칙")
        self.assertNotIn("CLAUDECODE", call["env"])
        self.assertFalse(Path(call["cwd"]).exists())  # a throwaway empty directory

    def test_default_model_passes_no_model_flag(self):
        fake = FakeCLI(result_json(result='{"a": 1}'))
        data, _ = model(fake, "claude-code").generate_json("s", "p", {"type": "object"})
        self.assertEqual(data, {"a": 1})  # falls back to the text result
        self.assertNotIn("--model", fake.calls[0]["args"])

    def test_mcp_run_allows_only_the_named_tools(self):
        fake = FakeCLI(result_json(result="답 [S1]"))
        text, _ = model(fake).run_mcp("s", "질문", ["py", "-m", "workhistory.mcp_server", "--trace", "t"], ["get_day"])
        self.assertEqual(text, "답 [S1]")
        args = fake.calls[0]["args"]
        config = json.loads(args[args.index("--mcp-config") + 1])
        self.assertEqual(config["mcpServers"]["workhistory"]["args"], ["-m", "workhistory.mcp_server", "--trace", "t"])
        self.assertEqual(args[args.index("--allowedTools") + 1], "mcp__workhistory__get_day")
        self.assertEqual(args[args.index("--tools") + 1], "")

    def test_errors_are_mapped(self):
        cases = [
            (FakeCLI(result_json(is_error=True, result="Not logged in · Please run /login")), "auth"),
            (FakeCLI(result_json(is_error=True, api_error_status=429, result="limit")), "quota"),
            (FakeCLI(result_json(is_error=True, subtype="error_max_budget_usd")), "quota"),
            (FakeCLI(result_json(is_error=True, api_error_status=529, result="Overloaded")), "server"),
            (FakeCLI("not json", returncode=1, stderr="boom"), "error"),
        ]
        for fake, kind in cases:
            with self.subTest(kind=kind), self.assertRaises(AIError) as ctx:
                model(fake).generate_json("s", "p", {"type": "object"})
            self.assertEqual(ctx.exception.kind, kind)

    def test_check_reads_login_state(self):
        model(FakeCLI(json.dumps({"loggedIn": True}))).check()
        with self.assertRaises(AIError) as ctx:
            model(FakeCLI(json.dumps({"loggedIn": False}))).check()
        self.assertEqual(ctx.exception.kind, "auth")

    def test_needs_the_executable(self):
        with self.assertRaises(AIError):
            ClaudeCodeModel("claude-code", cli="", runner=FakeCLI())
        with self.assertRaises(AIError):
            ClaudeCodeModel("claude-code", cli="C:\\npm\\claude.cmd", runner=FakeCLI())


class SettingsTests(unittest.TestCase):
    def test_choosing_a_model_is_what_allows_real_data(self):
        with TempDB() as db, db.connect() as conn:
            with self.assertRaises(AIError):
                ai_service.check_allowed(conn, demo=False)
            s = ai_service.update_settings(conn, {"model": "claude-code:sonnet"}, False)
            self.assertEqual((s["model"], s["enabled"]), ("claude-code:sonnet", True))
            ai_service.check_allowed(conn, demo=False)

    def test_factory_routes_claude_models_to_claude_code(self):
        try:
            m = ai_service.default_factory("claude-code:haiku")
        except AIError as e:  # Claude Code not installed on this machine
            self.assertEqual(e.kind, "config")
        else:
            self.assertIsInstance(m, ClaudeCodeModel)


class MCPServerModel:
    """Plays Claude Code's part in-process: starts the app's MCP server from the given
    command line (same --db/--trace/--demo), calls tools, then answers."""

    name = "claude-code:sonnet"

    def __init__(self, calls, answer):
        self.calls, self.answer = calls, answer

    def run_mcp(self, system, prompt, server, tools):
        from mcp import Client
        from workhistory import mcp_server
        from workhistory.db import Database

        db = Database(Path(server[server.index("--db") + 1]))
        trace = Path(server[server.index("--trace") + 1])
        results = []

        async def go():
            async with Client(mcp_server.build(db, None, demo_mode="--demo" in server, trace=trace)) as c:
                for name, args in self.calls:
                    assert name in tools
                    results.append(json.loads((await c.call_tool(name, args)).content[0].text))
        asyncio.run(go())
        return self.answer(results), Usage(10, 5, 0, len(self.calls) + 1)


@unittest.skipUnless(HAS_MCP, "mcp/dulwich not installed (run tests with .venv python)")
class AskThroughClaudeCodeTests(unittest.TestCase):
    def test_citations_are_checked_against_the_mcp_trace(self):
        from workhistory import demo
        with TempDB() as db:
            demo.seed(db, None)
            with db.connect() as conn:
                def answer(results):
                    ref = results[0]["results"][0]["ref"]
                    return f"찾았습니다 [{ref}, S999999]."
                m = MCPServerModel([("search_history", {"query": "GOV-1418"})], answer)
                out = ai_agent.ask(conn, "GOV-1418 어디까지?", demo=True, factory=lambda n: m)
        self.assertEqual(len(out["citations"]), 1)
        self.assertEqual(out["dropped_refs"], 1)  # S999999 was never returned by a tool
        self.assertEqual(out["tool_calls"], [{"name": "search_history", "ok": True,
                                              "args": {"query": "GOV-1418", "date_from": None, "date_to": None}}])

    def test_real_data_needs_claude_code_chosen_in_the_app(self):
        with TempDB() as db:
            with db.connect() as conn:
                m = MCPServerModel([("list_projects", {})], lambda r: json.dumps(r[0], ensure_ascii=False))
                ai_service.update_settings(conn, {"model": "claude-code:sonnet"}, False)
                conn.commit()
                out = ai_agent.ask(conn, "프로젝트?", demo=False, factory=lambda n: m)
                self.assertNotIn("error", out["answer"])
                # Turning AI off withdraws it: the app's own MCP session refuses.
                ai_service.update_settings(conn, {"model": ""}, False)
                conn.commit()
                from workhistory import mcp_server
                self.assertFalse(mcp_server.is_enabled(conn, app_session=True))
                self.assertFalse(mcp_server.is_enabled(conn))


if __name__ == "__main__":
    unittest.main()
