import importlib.util
import unittest
from datetime import date, timedelta
from pathlib import Path

from workhistory.ai_client import AgentResult, AIError, ToolTrace, Usage
from workhistory.store import Invalid

from tests.helpers import TempDB

HAS_DULWICH = importlib.util.find_spec("dulwich") is not None


class ScriptedModel:
    """Calls the given tools in order, then answers with `answer` (which may cite refs)."""

    def __init__(self, calls, answer):
        self.name = "fake"
        self.script = calls
        self.answer = answer
        self.results = []
        self.prompt = None

    def check(self):
        pass

    def run_tools(self, system, prompt, tools, execute, max_calls=12):
        self.prompt = prompt
        self.system = system
        traces = []
        for name, args in self.script:
            try:
                self.results.append(execute(name, args))
                traces.append(ToolTrace(name, args, True))
            except Exception as e:
                self.results.append({"error": str(e)})
                traces.append(ToolTrace(name, args, False))
        answer = self.answer(self.results) if callable(self.answer) else self.answer
        return AgentResult(answer, Usage(500, 80, 20, len(self.script) + 1), traces)


@unittest.skipUnless(HAS_DULWICH, "dulwich not installed (run tests with .venv python)")
class AgentTests(unittest.TestCase):
    def setUp(self):
        from workhistory import demo
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        self.repos = Path(self.db.path).parent / "snapshots"
        demo.seed(self.db, self.repos)
        self.monday = date.today() - timedelta(days=date.today().weekday())

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def tools(self, conn):
        from workhistory.ai_agent import HistoryTools
        return HistoryTools(conn, self.repos)

    def test_search_finds_issue_across_record_types(self):
        with self.db.connect() as conn:
            t = self.tools(conn)
            r = t.search_history("GOV-1418 토큰")
        types = {item["type"] for item in r["results"]}
        self.assertIn("메모", types)
        self.assertTrue(all(item["ref"] in t.sources for item in r["results"]))
        notes = [i for i in r["results"] if i["type"] == "메모"]
        self.assertTrue(any("GOV-1418" in n["text"] for n in notes))

    def test_search_by_file_name_returns_change_refs(self):
        with self.db.connect() as conn:
            t = self.tools(conn)
            r = t.search_history("TokenRefreshFilter")
        changes = [i for i in r["results"] if i["type"] == "파일 변경"]
        self.assertTrue(changes)
        self.assertTrue(all(c["ref"].startswith("C") for c in changes))

    def test_get_day_project_and_change(self):
        fix_day = self.monday - timedelta(days=6)
        with self.db.connect() as conn:
            t = self.tools(conn)
            day = t.get_day(fix_day.isoformat())
            self.assertEqual(day["ref"], f"D{fix_day.isoformat()}")
            self.assertTrue(day["sessions"] and day["changes"] and day["notes"])
            change_ref = day["changes"][0]["ref"]
            detail = t.get_change(change_ref)
            self.assertIn("+            refreshTokens.delete(stored); // 기존 리프레시 토큰 무효화 (GOV-1418)",
                          detail["diff_excerpt"])
            project = t.get_project("gov24")  # partial, case-insensitive
            self.assertEqual(project["name"], "Gov24 민원 API")
            self.assertEqual(project["next_action"], "커서 기반 페이징 전환 검토 (GOV-1430)")
            self.assertTrue(project["recent_changes"])
            self.assertIn("error", t.get_project("없는 프로젝트"))
            self.assertIn("error", t.get_change("C999999"))
            listing = t.list_projects()
            self.assertEqual({p["name"] for p in listing["projects"]}, {"Gov24 민원 API", "K8s 전환 PoC"})
            with self.assertRaises(ValueError):
                t.execute("drop_tables", {})
            with self.assertRaises(ValueError):
                t.get_day("지난주")

    def test_ask_verifies_citations(self):
        from workhistory import ai_agent

        def answer(results):
            note_ref = next(i["ref"] for i in results[0]["results"] if i["type"] == "메모")
            return f"GOV-1418은 QA 확인 후 종료했습니다 [{note_ref}]. 지어낸 근거 [S999999]도 있습니다 [C424242]."

        model = ScriptedModel([("search_history", {"query": "GOV-1418"})], answer)
        with self.db.connect() as conn:
            out = ai_agent.ask(conn, "GOV-1418 어떻게 됐지?", demo=True, factory=lambda n: model,
                               repos_dir=self.repos)
            runs = [dict(r) for r in conn.execute("SELECT kind, ok, tool_calls FROM ai_runs")]
        self.assertEqual(out["dropped_refs"], 2)
        self.assertNotIn("S999999", out["answer"])
        self.assertNotIn("C424242", out["answer"])
        self.assertEqual(len(out["citations"]), 1)
        self.assertEqual(out["citations"][0]["type"], "note")
        self.assertEqual(out["tool_calls"], [{"name": "search_history", "args": {"query": "GOV-1418"}, "ok": True}])
        self.assertEqual(runs, [{"kind": "ask", "ok": 1, "tool_calls": 1}])
        self.assertIn("오늘은 " + date.today().isoformat(), model.system)

    def test_guard_redaction_and_validation(self):
        from workhistory import ai_agent
        model = ScriptedModel([], "답")
        with self.db.connect() as conn:
            with self.assertRaises(AIError) as e:
                ai_agent.ask(conn, "질문", demo=False, factory=lambda n: model)
            self.assertEqual(e.exception.kind, "not_allowed")
            with self.assertRaises(Invalid):
                ai_agent.ask(conn, "  ", demo=True, factory=lambda n: model)
            ai_agent.ask(conn, "token=abc123secret 이거 어디 썼지?", demo=True, factory=lambda n: model)
        self.assertNotIn("abc123secret", model.prompt)

    def test_tool_errors_do_not_break_the_answer(self):
        from workhistory import ai_agent
        model = ScriptedModel([("get_day", {"date": "어제"}), ("get_project", {"name": "Gov24"})],
                              "기록에서 찾지 못했습니다.")
        with self.db.connect() as conn:
            out = ai_agent.ask(conn, "어제 뭐 했지?", demo=True, factory=lambda n: model)
        self.assertEqual([c["ok"] for c in out["tool_calls"]], [False, True])
        self.assertEqual(out["answer"], "기록에서 찾지 못했습니다.")


class VerifyTests(unittest.TestCase):
    def test_verify_answer(self):
        from workhistory.ai_agent import verify_answer
        text, cited, dropped = verify_answer("A [S1]. B [C2] [X9] [S1]. C [N3] .", {"S1": {}, "N3": {}})
        self.assertEqual((cited, dropped), (["S1", "N3"], 1))
        self.assertNotIn("C2", text)
        self.assertIn("[S1]", text)

    def test_grouped_refs_are_split_and_checked_one_by_one(self):
        from workhistory.ai_agent import verify_answer
        text, cited, dropped = verify_answer("한 일 [S2, C9, D2026-09-24].", {"S2": {}, "D2026-09-24": {}})
        self.assertEqual((text, cited, dropped), ("한 일 [S2][D2026-09-24].", ["S2", "D2026-09-24"], 1))


if __name__ == "__main__":
    unittest.main()
