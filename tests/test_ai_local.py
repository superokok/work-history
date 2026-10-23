import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workhistory import ai_local, ai_service
from workhistory.ai_client import AIError, ToolSpec

from tests.helpers import TempDB


class FakeServer:
    """Stands in for the llama-server process: records acquire/release, starts nothing."""

    def __init__(self):
        self.acquired = 0
        self.released = 0

    def acquire(self, exe, model):
        self.acquired += 1
        return "http://127.0.0.1:1", "key"

    def release(self):
        self.released += 1


class LocalLLMTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="wh-llm-"))
        (self.dir / "bin").mkdir()
        (self.dir / "models").mkdir()
        (self.dir / "bin" / ai_local.SERVER_EXE).write_bytes(b"")
        (self.dir / "models" / "Tiny-Q4.gguf").write_bytes(b"")
        (self.dir / "models" / "mmproj-F16.gguf").write_bytes(b"")  # a vision add-on, not a model
        env = mock.patch.dict(os.environ, {"WORKHISTORY_LLM_DIR": str(self.dir)})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(shutil.rmtree, self.dir, True)

    def model(self, replies):
        """A LocalModel whose HTTP calls return `replies` in order; sent bodies land in self.sent."""
        self.sent = []
        server = FakeServer()
        m = ai_local.LocalModel("local:Tiny-Q4", server=server)
        it = iter(replies)

        def post(body):
            self.sent.append(json.loads(json.dumps(body)))
            server.acquire(None, None)
            try:
                return next(it)
            finally:
                server.release()
        m._post = post
        return m, server


def reply(content="", tool_calls=None, prompt=10, completion=5):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message}], "usage": {"prompt_tokens": prompt, "completion_tokens": completion}}


class DiscoveryTests(LocalLLMTestCase):
    def test_models_are_the_gguf_files_except_add_ons(self):
        self.assertEqual(ai_local.available_models(), ["local:Tiny-Q4"])
        self.assertTrue(ai_local.is_local_model("local:Tiny-Q4"))
        self.assertFalse(ai_local.is_local_model("claude-code:sonnet"))

    def test_no_models_without_the_server_program(self):
        (self.dir / "bin" / ai_local.SERVER_EXE).unlink()
        self.assertEqual(ai_local.available_models(), [])
        with self.assertRaises(AIError) as e:
            ai_local.LocalModel("local:Tiny-Q4")
        self.assertEqual(e.exception.kind, "config")

    def test_settings_offer_local_models_and_accept_them_as_consent(self):
        with TempDB() as db, db.connect() as conn:
            s = ai_service.get_settings(conn, demo=False)
            self.assertIn("local:Tiny-Q4", s["models"])
            self.assertEqual(s["local_models"], ["local:Tiny-Q4"])
            self.assertFalse(s["enabled"])
            s = ai_service.update_settings(conn, {"model": "local:Tiny-Q4"}, demo=False)
            self.assertTrue(s["enabled"])
            ai_service.check_allowed(conn, demo=False)
            # the model file disappears: AI counts as off again, nothing falls back silently
            (self.dir / "models" / "Tiny-Q4.gguf").unlink()
            self.assertIsNone(ai_service.chosen_model(conn))
            with self.assertRaises(AIError):
                ai_service.check_allowed(conn, demo=False)

    def test_factory_picks_the_local_backend(self):
        self.assertIsInstance(ai_service.default_factory("local:Tiny-Q4"), ai_local.LocalModel)


class GenerateTests(LocalLLMTestCase):
    def test_json_is_constrained_by_the_schema_and_parsed(self):
        m, server = self.model([reply('{"summary": "요약"}', prompt=120, completion=30)])
        schema = {"type": "object", "properties": {"summary": {"type": "string"}}}
        data, usage = m.generate_json("sys", "prompt", schema)
        self.assertEqual(data, {"summary": "요약"})
        self.assertEqual((usage.input_tokens, usage.output_tokens, usage.requests), (120, 30, 1))
        body = self.sent[0]
        self.assertEqual(body["response_format"]["json_schema"]["schema"], schema)
        self.assertEqual(body["messages"][0], {"role": "system", "content": "sys"})
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual((server.acquired, server.released), (1, 1))

    def test_broken_json_is_an_invalid_output_error(self):
        m, _ = self.model([reply("not json")])
        with self.assertRaises(AIError) as e:
            m.generate_json("sys", "prompt", {"type": "object"})
        self.assertEqual(e.exception.kind, "invalid_output")


class ToolLoopTests(LocalLLMTestCase):
    TOOLS = [ToolSpec("get_day", "하루 기록", {"type": "object", "properties": {"date": {"type": "string"}}})]

    def call(self, id, name, args):
        return {"id": id, "type": "function", "function": {"name": name, "arguments": args}}

    def test_tool_results_go_back_to_the_model_until_it_answers(self):
        m, _ = self.model([
            reply(tool_calls=[self.call("a", "get_day", '{"date": "2026-10-13"}')]),
            reply("10월 13일에 토큰 필터를 고쳤습니다 [C5]."),
        ])
        seen = []

        def execute(name, args):
            seen.append((name, args))
            return {"changes": [{"ref": "C5"}]}
        r = m.run_tools("sys", "질문", self.TOOLS, execute)
        self.assertEqual(seen, [("get_day", {"date": "2026-10-13"})])
        self.assertEqual(r.text, "10월 13일에 토큰 필터를 고쳤습니다 [C5].")
        self.assertEqual([(c.name, c.ok) for c in r.calls], [("get_day", True)])
        self.assertEqual(r.usage.requests, 2)
        tool_msg = self.sent[1]["messages"][-1]
        self.assertEqual((tool_msg["role"], tool_msg["tool_call_id"]), ("tool", "a"))
        self.assertEqual(json.loads(tool_msg["content"]), {"changes": [{"ref": "C5"}]})
        self.assertEqual(self.sent[0]["tools"][0]["function"]["name"], "get_day")

    def test_bad_calls_are_reported_to_the_model_not_raised(self):
        m, _ = self.model([
            reply(tool_calls=[self.call("a", "get_day", "{not json"), self.call("b", "nope", "{}")]),
            reply("찾지 못했습니다."),
        ])

        def execute(name, args):
            raise ValueError(f"알 수 없는 도구: {name}")
        r = m.run_tools("sys", "질문", self.TOOLS, execute)
        self.assertEqual([(c.name, c.ok) for c in r.calls], [("get_day", False), ("nope", False)])
        errors = [json.loads(msg["content"])["error"] for msg in self.sent[1]["messages"] if msg["role"] == "tool"]
        self.assertEqual(len(errors), 2)
        self.assertIn("알 수 없는 도구", errors[1])

    def test_tool_budget_ends_with_an_answer_without_tools(self):
        loop = reply(tool_calls=[self.call("x", "get_day", "{}")])
        m, _ = self.model([loop, loop, reply("마지막 답")])
        r = m.run_tools("sys", "질문", self.TOOLS, lambda n, a: {}, max_calls=2)
        self.assertEqual(r.text, "마지막 답")
        self.assertEqual(len(r.calls), 2)
        self.assertNotIn("tools", self.sent[-1])  # the last turn may not call tools any more


class ServerArgsTests(unittest.TestCase):
    def test_server_is_private_to_this_app(self):
        """Bound to 127.0.0.1 with a random key and no web UI (checked on the command line it would run)."""
        started = {}

        class Proc:
            def poll(self):
                return 0  # exits at once: no model is actually loaded

        def popen(args, **kw):
            started["args"] = args
            return Proc()
        server = ai_local._Server()
        with mock.patch.object(ai_local.subprocess, "Popen", popen), self.assertRaises(AIError):
            server.acquire(Path("llama-server.exe"), Path("m.gguf"))
        args = started["args"]
        self.assertEqual(args[args.index("--host") + 1], "127.0.0.1")
        self.assertGreaterEqual(len(args[args.index("--api-key") + 1]), 24)
        self.assertIn("--no-webui", args)


if __name__ == "__main__":
    unittest.main()
