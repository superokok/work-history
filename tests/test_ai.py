import importlib.util
import json
import unittest
from datetime import date, timedelta

from workhistory import ai_context, ai_report, ai_service, service, sessionstore, store
from workhistory.ai_client import AIError, Usage

from tests.helpers import TempDB, local_ts

DAY = date(2026, 9, 30)  # a Wednesday


class FakeModel:
    """Stands in for the model: records what it was sent, returns a canned reply."""

    def __init__(self, reply=None, error: AIError = None):
        self.name = "fake"
        self.reply = reply
        self.error = error
        self.calls = []

    def check(self):
        if self.error:
            raise self.error

    def generate_json(self, system, prompt, schema, max_output_tokens=16000, thinking=None):
        self.calls.append({"system": system, "prompt": prompt, "schema": schema})
        if self.error:
            raise self.error
        return self.reply, Usage(input_tokens=100, output_tokens=50, thinking_tokens=10, requests=1)

    def run_tools(self, system, prompt, tools, execute, max_calls=12):
        raise NotImplementedError


class AIDataTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        with self.db.connect() as conn:
            self.src = conn.execute("INSERT INTO sources (bucket_id, bucket_created, kind, hostname) "
                                    "VALUES ('w', 'c', 'window', 'h')").lastrowid
            self.gov = service.create_project(conn, {"name": "Gov24 백엔드"})["id"]
            service.add_rule(conn, self.gov, {"field": "title", "pattern": "gov24"})
            service.update_project(conn, self.gov, {"next_action": "토큰 갱신 API 테스트 작성"})
            self.unused = service.create_project(conn, {"name": "안 쓴 프로젝트"})["id"]
        self.aw = 0
        self.add(9, 0, 9, 40, "idea64.exe", "gov24-api – AuthService.java")
        self.add(9, 40, 9, 50, "chrome.exe", "JWT refresh token - Google Chrome")
        self.add(10, 30, 10, 30, "slack.exe", "Slack", s2=40)  # 40-second session, left out
        self.add(11, 0, 11, 30, "", "")                          # 업무 외
        self.add(13, 0, 13, 20, "chrome.exe", "YouTube - Google Chrome")
        with self.db.connect() as conn:
            sessions = service.timeline(conn, DAY)["sessions"]
            self.yt = sessions[-1]["id"]
            sessionstore.update_session(conn, self.yt, {"excluded": True})
            sessionstore.update_session(conn, sessions[0]["id"], {"note": "리프레시 토큰 만료 버그 원인 확인 필요"})
            self.note = service.create_note(conn, {"day": DAY.isoformat(), "body": "QA 서버 배포 대기 중",
                                                   "project_id": self.gov})["id"]
            self.first = sessions[0]["id"]  # IntelliJ + the 10-minute unclassified Chrome read

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def add(self, h1, m1, h2, m2, app, title, s2=0):
        self.aw += 1
        start, end = local_ts(DAY, h1, m1), local_ts(DAY, h2, m2, s2)
        with self.db.connect() as conn:
            store.mark_days_dirty(conn, start, end)
            conn.execute("INSERT INTO window_events (source_id, aw_id, start_ts, end_ts, app, title) "
                         "VALUES (?, ?, ?, ?, ?, ?)", (self.src, self.aw, start, end, app, title))

    def context(self):
        with self.db.connect() as conn:
            return ai_context.build(conn, DAY, DAY)


class ContextTests(AIDataTestCase):
    def test_only_work_data_is_included(self):
        ctx = self.context()
        text = json.dumps(ctx.payload, ensure_ascii=False)
        self.assertNotIn("YouTube", text)                      # excluded session
        self.assertNotIn("Slack", text)                        # under a minute
        self.assertNotIn("안 쓴 프로젝트", text)               # irrelevant project
        self.assertEqual(ctx.payload["offwork_minutes"], 30)
        self.assertEqual([(s["ref"], s["project"], s["minutes"]) for s in ctx.payload["sessions"]],
                         [(f"S{self.first}", "Gov24 백엔드", 50)])
        self.assertEqual(ctx.payload["project_minutes"], {"Gov24 백엔드": 50})
        session = ctx.payload["sessions"][0]
        self.assertEqual(session["windows"][0], {"app": "IntelliJ IDEA", "title": "gov24-api – AuthService.java",
                                                 "minutes": 40})
        self.assertEqual(session["user_note"], "리프레시 토큰 만료 버그 원인 확인 필요")
        self.assertEqual(ctx.payload["projects"], [{"ref": f"P{self.gov}", "name": "Gov24 백엔드",
                                                    "user_next_action": "토큰 갱신 API 테스트 작성"}])
        self.assertEqual(ctx.refs, {f"S{self.first}", f"N{self.note}", f"P{self.gov}"})

    def test_period_limits(self):
        with self.db.connect() as conn:
            with self.assertRaises(store.Invalid):
                ai_context.build(conn, DAY, DAY - timedelta(days=1))
            with self.assertRaises(store.Invalid):
                ai_context.build(conn, DAY, DAY + timedelta(days=40))


class ValidationTests(AIDataTestCase):
    def raw(self):
        s, n, p = f"S{self.first}", f"N{self.note}", f"P{self.gov}"
        return {"summary": "인증 모듈 작업", "projects": [{
            "project": "Gov24 백엔드",
            "done": [{"text": "리프레시 토큰 처리 분석", "refs": [s, "S999999", s]},
                     {"text": "근거 없는 성과", "refs": ["S424242"]}],
            "next": [{"text": "토큰 갱신 API 테스트 작성", "refs": [p]},
                     {"text": "AI가 지어낸 계획", "refs": [s]}],
            "issues": [{"text": "QA 서버 배포 대기", "refs": [n]}, {"text": "", "refs": []}],
        }]}

    def test_refs_verified_and_flags(self):
        report = ai_report.validate("weekly", self.context(), self.raw())
        [block] = report.projects
        self.assertEqual([(i.text, i.refs, i.flag) for i in block.done],
                         [("리프레시 토큰 처리 분석", [f"S{self.first}"], None), ("근거 없는 성과", [], "no_evidence")])
        self.assertEqual([i.flag for i in block.next], [None, "unconfirmed_plan"])
        self.assertEqual([i.text for i in block.issues], ["QA 서버 배포 대기"])
        self.assertEqual(report.dropped_refs, 2)
        self.assertEqual(block.seconds, 50 * 60)

    def test_project_left_out_by_ai_is_restored(self):
        report = ai_report.validate("weekly", self.context(), {"summary": "", "projects": []})
        self.assertEqual([(b.project, b.missing_from_ai) for b in report.projects], [("Gov24 백엔드", True)])
        self.assertIn("AI 초안에 항목이 없습니다", ai_report.render_text(report))

    def test_unclassified_time_is_not_forced_into_the_report(self):
        ctx = self.context()
        ctx.project_seconds["미분류"] = 3 * 3600  # meetings, chat: fine to leave out
        report = ai_report.validate("weekly", ctx, {"summary": "", "projects": []})
        self.assertNotIn("미분류", [b.project for b in report.projects])

    def test_garbage_output_does_not_crash(self):
        for raw in (None, [], {"projects": "x"}, {"projects": [1, {"done": "x"}]}):
            ai_report.validate("daily", self.context(), raw)

    def test_render_text(self):
        text = ai_report.render_text(ai_report.validate("weekly", self.context(), self.raw()))
        self.assertIn("# 주간보고 · 2026-09-30", text)
        self.assertIn("## Gov24 백엔드 (50분)", text)
        self.assertIn("[금주 실적]\n- 리프레시 토큰 처리 분석\n- 근거 없는 성과 (근거 없음 — 확인 필요)", text)
        self.assertIn("- AI가 지어낸 계획 (AI 제안 — 확인 필요)", text)
        self.assertIn("(보고서에서 제외한 업무 외 시간: 30분)", text)
        self.assertNotIn("S" + str(self.first), text)  # references stay out of the pasted text

    def test_daily_labels(self):
        text = ai_report.render_text(ai_report.validate("daily", self.context(), self.raw()))
        self.assertIn("[오늘 한 일]", text)
        self.assertIn("# 업무일지 · 2026-09-30", text)


class ServiceTests(AIDataTestCase):
    def test_real_data_blocked_until_ai_is_turned_on(self):
        model = FakeModel({"summary": "", "projects": []})
        with self.db.connect() as conn:
            with self.assertRaises(AIError) as e:
                ai_service.generate_report(conn, "daily", DAY, demo=False, factory=lambda n: model)
            self.assertEqual(e.exception.kind, "not_allowed")
            self.assertEqual(model.calls, [])  # nothing was sent
            ai_service.update_settings(conn, {"model": "claude-code:sonnet"}, demo=False)
            out = ai_service.generate_report(conn, "daily", DAY, demo=False, factory=lambda n: model)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(out["usage"]["input_tokens"], 100)

    def test_demo_database_always_allowed_and_prompt_is_payload(self):
        model = FakeModel({"summary": "s", "projects": []})
        with self.db.connect() as conn:
            out = ai_service.generate_report(conn, "weekly", DAY, demo=True, factory=lambda n: model)
            preview = ai_service.preview(conn, "weekly", DAY)
        self.assertEqual((out["start"], out["end"]), ("2026-09-28", "2026-10-04"))
        self.assertEqual(model.calls[0]["prompt"], preview["prompt"])  # preview shows exactly what is sent
        self.assertEqual(model.calls[0]["schema"], ai_report.SCHEMA)

    def test_runs_logged_without_content(self):
        with self.db.connect() as conn:
            ai_service.generate_report(conn, "daily", DAY, demo=True, factory=lambda n: FakeModel({"projects": []}))
            with self.assertRaises(AIError):
                ai_service.generate_report(conn, "daily", DAY, demo=True,
                                           factory=lambda n: FakeModel(error=AIError("한도", "quota")))
            rows = [dict(r) for r in conn.execute("SELECT * FROM ai_runs ORDER BY id")]
        self.assertEqual([(r["kind"], r["ok"], r["error_kind"], r["input_tokens"]) for r in rows],
                         [("report_daily", 1, None, 100), ("report_daily", 0, "quota", 0)])
        self.assertNotIn("text", rows[0])

    def test_empty_period(self):
        with self.db.connect() as conn:
            with self.assertRaises(store.Invalid):
                ai_service.generate_report(conn, "daily", DAY + timedelta(days=3), demo=True,
                                           factory=lambda n: FakeModel({}))

    def test_save_and_clear_report(self):
        with self.db.connect() as conn:
            saved = ai_service.save_report(conn, {"kind": "weekly", "day": "2026-10-01", "body": "보고서"})
            self.assertEqual((saved["start_day"], saved["end_day"]), ("2026-09-28", "2026-10-04"))
            self.assertEqual(ai_service.get_report(conn, "weekly", DAY)["body"], "보고서")
            self.assertIsNone(ai_service.save_report(conn, {"kind": "weekly", "day": "2026-10-01", "body": " "}))
            self.assertIsNone(ai_service.get_report(conn, "weekly", DAY))
            with self.assertRaises(store.Invalid):
                ai_service.save_report(conn, {"kind": "monthly", "day": "2026-10-01", "body": "x"})

    def test_settings(self):
        with self.db.connect() as conn:
            s = ai_service.get_settings(conn, demo=False)
            self.assertEqual((s["model"], s["enabled"]), ("", False))  # off until the user picks a model
            s = ai_service.update_settings(conn, {"model": "claude-code:haiku"}, False)
            self.assertEqual((s["model"], s["enabled"]), ("claude-code:haiku", True))
            s = ai_service.update_settings(conn, {"model": ""}, False)
            self.assertFalse(s["enabled"])
            for bad in ("gpt-5", "gemini-3.8-flash"):
                with self.assertRaises(store.Invalid):
                    ai_service.update_settings(conn, {"model": bad}, False)
            # A model saved by an older version (a removed provider) counts as off.
            ai_service._set_meta(conn, "ai_model", "gemini-3.8-flash")
            self.assertFalse(ai_service.get_settings(conn, False)["enabled"])
            with self.assertRaises(AIError):
                ai_service.check_allowed(conn, demo=False)


HAS_DULWICH = importlib.util.find_spec("dulwich") is not None


@unittest.skipUnless(HAS_DULWICH, "dulwich not installed (run tests with .venv python)")
class ChangesContextTests(unittest.TestCase):
    """File changes (C refs) in the AI payload, using the demo scenario's file history."""

    def setUp(self):
        from pathlib import Path
        from workhistory import demo
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        self.repos = Path(self.db.path).parent / "snapshots"
        demo.seed(self.db, self.repos)
        today = date.today()
        self.monday = today - timedelta(days=today.weekday())

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def build(self, offset):
        day = self.monday + timedelta(days=offset)
        with self.db.connect() as conn:
            return ai_context.build(conn, day, day, self.repos)

    def test_changes_have_refs_files_and_diff_excerpts(self):
        ctx = self.build(-6)  # last Tuesday: the GOV-1418 fix
        changes = ctx.payload["changes"]
        self.assertEqual([c["folder"] for c in changes], ["gov24-minwon-api", "gov24-minwon-api"])
        fix = changes[0]
        self.assertIn(fix["ref"], ctx.refs)
        self.assertEqual(ctx.sources[fix["ref"]]["type"], "change")
        self.assertEqual({f["path"].rsplit("/", 1)[-1]: f["change"] for f in fix["files"]},
                         {"TokenRefreshFilter.java": "수정", "LegacyTokenTest.java": "삭제"})
        excerpt = "\n".join(fix["diff_excerpt"])
        self.assertIn("+            refreshTokens.delete(stored);", excerpt)
        self.assertEqual(ctx.project_changes, {"Gov24 민원 API": 2})

    def test_without_repositories_files_are_listed_without_diffs(self):
        day = self.monday - timedelta(days=6)
        with self.db.connect() as conn:
            ctx = ai_context.build(conn, day, day)  # no repos_dir
        self.assertTrue(ctx.payload["changes"])
        self.assertNotIn("diff_excerpt", ctx.payload["changes"][0])

    def test_secrets_in_changes_and_notes_are_masked(self):
        from workhistory import snapshots
        day = self.monday - timedelta(days=6)
        with self.db.connect() as conn:
            fid = conn.execute("SELECT id FROM watched_folders WHERE label = 'gov24-minwon-api'").fetchone()[0]
            repo = snapshots._open_repo(snapshots.repo_path(self.repos, fid))
            try:
                head = repo[repo[repo.refs[snapshots.REF]].tree]
                files = {}

                def collect(tree, prefix):
                    for e in tree.items():
                        name = prefix + e.path.decode()
                        if e.mode == snapshots.DIR_MODE:
                            collect(repo[e.sha], name + "/")
                        else:
                            files[name] = e.sha
                collect(head, "")
                files["src/main/resources/application-dev.yml"] = snapshots.add_blob(
                    repo, "spring:\n  datasource:\n    password: Dev!Pass123\n".encode())
                snapshots.commit_snapshot(conn, repo, fid, files, local_ts(day, 16, 0))
            finally:
                repo.close()
            service.create_note(conn, {"day": day.isoformat(), "body": "QA 계정 token=qa_tok_998877 확인"})
            ctx = ai_context.build(conn, day, day, self.repos)
        text = json.dumps(ctx.payload, ensure_ascii=False)
        self.assertNotIn("Dev!Pass123", text)
        self.assertNotIn("qa_tok_998877", text)
        self.assertIn("password: ***", text)

    def test_project_with_only_file_changes_is_restored(self):
        ctx = self.build(-6)
        ctx.project_seconds.clear()  # e.g. window tracking switched off on a 폐쇄망 PC
        report = ai_report.validate("daily", ctx, {"summary": "", "projects": []})
        self.assertEqual([(b.project, b.missing_from_ai) for b in report.projects], [("Gov24 민원 API", True)])

    def test_change_refs_count_as_evidence(self):
        ctx = self.build(-6)
        ref = ctx.payload["changes"][0]["ref"]
        report = ai_report.validate("daily", ctx, {"summary": "", "projects": [
            {"project": "Gov24 민원 API", "done": [{"text": "리프레시 토큰 재사용 차단", "refs": [ref]}],
             "next": [{"text": "변경만 근거인 계획", "refs": [ref]}], "issues": []}]})
        block = next(b for b in report.projects if b.project == "Gov24 민원 API")
        self.assertEqual((block.done[0].flag, block.next[0].flag), (None, "unconfirmed_plan"))


if __name__ == "__main__":
    unittest.main()
