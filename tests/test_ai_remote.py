import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from workhistory import ai_remote, ai_service
from workhistory.ai_client import AIError, ToolSpec
from workhistory.store import Invalid, NotFound

from tests.helpers import TempDB


class FakeLLMServer:
    """A tiny OpenAI-compatible server: /v1/models and /v1/chat/completions, with scripted answers."""

    def __init__(self, key="", models=("qwen3-32b",), reject_formats=(), reject_template_kwargs=False):
        self.key, self.models, self.reject_formats = key, list(models), set(reject_formats)
        self.reject_template_kwargs = reject_template_kwargs
        self.requests: list[dict] = []
        self.replies: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authorized(self):
                if outer.key and self.headers.get("Authorization") != f"Bearer {outer.key}":
                    self._send(401, {"error": "bad key"})
                    return False
                return True

            def do_GET(self):
                if not self._authorized():
                    return
                if self.path == "/v1/models":
                    self._send(200, {"data": [{"id": m} for m in outer.models]})
                else:
                    self._send(404, {"error": "no"})

            def do_POST(self):
                if not self._authorized():
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                if outer.reject_template_kwargs and "chat_template_kwargs" in body:
                    self._send(400, {"error": "unknown field chat_template_kwargs"})
                    return
                fmt = (body.get("response_format") or {}).get("type")
                if fmt in outer.reject_formats:
                    self._send(400, {"error": f"response_format {fmt} not supported"})
                    return
                self._send(200, outer.replies.pop(0))

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def reply(self, content="", tool_calls=None, finish="stop"):
        message = {"role": "assistant", "content": content}
        if tool_calls:
            message["tool_calls"] = tool_calls
        self.replies.append({"choices": [{"message": message, "finish_reason": finish}],
                             "usage": {"prompt_tokens": 50, "completion_tokens": 9}})

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class RegistryTests(unittest.TestCase):
    def test_addresses_are_normalized_to_the_api_base(self):
        for raw, url in [("10.1.2.3:8000", "http://10.1.2.3:8000/v1"),
                         ("http://llm.corp:8000/", "http://llm.corp:8000/v1"),
                         ("https://gw.corp/openai/v1/", "https://gw.corp/openai/v1"),
                         ("http://h:1/v1/chat/completions", "http://h:1/v1")]:
            self.assertEqual(ai_remote.normalize_url(raw), url)
        for bad in ("", "ftp://h", "http://user:pw@h:1", "http://h:1/?x=1", "http://"):
            with self.assertRaises(Invalid, msg=bad):
                ai_remote.normalize_url(bad)

    def test_register_list_update_delete_and_the_key_never_comes_back(self):
        with TempDB() as db, db.connect() as conn:
            e = ai_remote.add_endpoint(conn, {"name": "행망 LLM", "url": "10.1.2.3:8000", "model_id": "qwen3-32b",
                                              "api_key": "sk-secret-123"})
            self.assertEqual((e["model"], e["url"], e["has_key"]), (f"remote:{e['id']}", "http://10.1.2.3:8000/v1", True))
            self.assertNotIn("sk-secret-123", json.dumps(ai_remote.list_endpoints(conn)))
            self.assertNotIn("api_key", e)
            e = ai_remote.update_endpoint(conn, e["id"], {"name": "행망 LLM 2"})  # key kept when not sent
            self.assertEqual((e["name"], e["has_key"]), ("행망 LLM 2", True))
            e = ai_remote.update_endpoint(conn, e["id"], {"api_key": ""})
            self.assertFalse(e["has_key"])
            for bad in ({"url": "h:1", "model_id": "m"}, {"name": "n", "url": "h:1"}, {"name": "n", "model_id": "m"}):
                with self.assertRaises(Invalid):
                    ai_remote.add_endpoint(conn, bad)
            ai_remote.delete_endpoint(conn, e["id"])
            self.assertEqual(ai_remote.list_endpoints(conn), [])
            with self.assertRaises(NotFound):
                ai_remote.delete_endpoint(conn, e["id"])

    def test_settings_list_servers_and_choosing_one_is_consent(self):
        with TempDB() as db, db.connect() as conn:
            e = ai_remote.add_endpoint(conn, {"name": "행망", "url": "h:1", "model_id": "m"})
            s = ai_service.get_settings(conn, demo=False)
            self.assertIn(e["model"], s["models"])
            self.assertEqual([x["name"] for x in s["servers"]], ["행망"])
            s = ai_service.update_settings(conn, {"model": e["model"]}, demo=False)
            self.assertTrue(s["enabled"])
            model = ai_service._model(conn, False, ai_service.default_factory)
            self.assertIsInstance(model, ai_remote.RemoteModel)
            self.assertEqual((model.url, model.model_id), ("http://h:1/v1", "m"))
            ai_remote.delete_endpoint(conn, e["id"])  # the chosen server disappears: AI is off, no silent fallback
            self.assertIsNone(ai_service.chosen_model(conn))
            with self.assertRaises(AIError):
                ai_service.check_allowed(conn, demo=False)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.srv = FakeLLMServer(key="k-1")
        self.addCleanup(self.srv.close)

    def model(self, **kw):
        return ai_remote.RemoteModel(1, "test", ai_remote.normalize_url(self.srv.url), "qwen3-32b", kw.get("key", "k-1"))

    def test_models_listed_and_check_sends_no_records(self):
        self.assertEqual(ai_remote.fetch_models(self.srv.url, "k-1"), ["qwen3-32b"])
        self.model().check()
        self.assertEqual(self.srv.requests, [])  # only GET /v1/models
        with self.assertRaises(AIError) as e:
            ai_remote.RemoteModel(1, "t", ai_remote.normalize_url(self.srv.url), "other", "k-1").check()
        self.assertIn("qwen3-32b", e.exception.message)
        with self.assertRaises(AIError) as e:
            ai_remote.fetch_models(self.srv.url, "wrong")
        self.assertIn("API 키", e.exception.message)

    def test_report_json_with_schema(self):
        self.srv.reply('{"summary": "요약"}')
        data, usage = self.model().generate_json("sys", "prompt", {"type": "object"})
        self.assertEqual((data, usage.input_tokens), ({"summary": "요약"}, 50))
        body = self.srv.requests[0]
        self.assertEqual((body["model"], body["response_format"]["type"]), ("qwen3-32b", "json_schema"))
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})  # thinking off where allowed

    def test_servers_that_refuse_the_thinking_switch_are_asked_plainly(self):
        srv = FakeLLMServer(reject_template_kwargs=True)
        self.addCleanup(srv.close)
        srv.reply('{"a": 1}')
        srv.reply('{"a": 2}')
        m = ai_remote.RemoteModel(1, "t", ai_remote.normalize_url(srv.url), "m")
        self.assertEqual(m.generate_json("sys", "p", {"type": "object"})[0], {"a": 1})
        self.assertEqual(m.generate_json("sys", "p", {"type": "object"})[0], {"a": 2})
        self.assertEqual(["chat_template_kwargs" in b for b in srv.requests], [True, False, False])  # learned once

    def test_an_answer_cut_off_at_the_length_limit_says_so(self):
        self.srv.reply('{"summary": "끝나지 않은', finish="length")
        with self.assertRaises(AIError) as e:
            self.model().generate_json("sys", "prompt", {"type": "object"})
        self.assertIn("잘렸습니다", e.exception.message)

    def test_servers_without_schema_support_fall_back(self):
        srv = FakeLLMServer(reject_formats={"json_schema", "json_object"})
        self.addCleanup(srv.close)
        srv.reply('<think>생각 중</think>\n```json\n{"summary": "요약"}\n```')
        m = ai_remote.RemoteModel(1, "t", ai_remote.normalize_url(srv.url), "m")
        data, _ = m.generate_json("sys", "prompt", {"type": "object"})
        self.assertEqual(data, {"summary": "요약"})
        self.assertNotIn("response_format", srv.requests[-1])
        self.assertIn("JSON 스키마", srv.requests[-1]["messages"][0]["content"])

    def test_questions_run_the_apps_tool_loop(self):
        self.srv.reply(tool_calls=[{"id": "a", "type": "function",
                                    "function": {"name": "get_day", "arguments": '{"date": "2026-10-13"}'}}])
        self.srv.reply("<think>…</think>10월 13일에 고쳤습니다 [C5].")
        r = self.model().run_tools("sys", "질문", [ToolSpec("get_day", "d", {"type": "object"})],
                                   lambda name, args: {"ref": "C5"})
        self.assertEqual(r.text, "10월 13일에 고쳤습니다 [C5].")
        self.assertEqual([c.name for c in r.calls], ["get_day"])
        self.assertEqual(self.srv.requests[1]["messages"][-1]["role"], "tool")

    def test_unreachable_server_is_a_network_error(self):
        m = ai_remote.RemoteModel(1, "t", "http://127.0.0.1:9/v1", "m")
        with self.assertRaises(AIError) as e:
            m.generate_json("sys", "prompt", {"type": "object"})
        self.assertEqual(e.exception.kind, "network")


if __name__ == "__main__":
    unittest.main()
