import importlib.util
import json
import re
import unittest
from datetime import date, timedelta
from unittest import mock

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
        self.assertNotIn("Gov24 백엔드", text)                  # no projects: the model groups the work
        self.assertEqual([(s["ref"], s["minutes"]) for s in ctx.payload["sessions"]], [(f"S{self.first}", 50)])
        self.assertEqual(ctx.payload["work_minutes"], 50)
        session = ctx.payload["sessions"][0]
        self.assertEqual(session["windows"][0], {"app": "IntelliJ IDEA", "title": "gov24-api – AuthService.java",
                                                 "minutes": 40})
        self.assertNotIn("user_note", session)                 # notes are not sent any more
        self.assertNotIn("QA 서버 배포 대기", text)
        self.assertEqual(ctx.refs, {f"S{self.first}"})

    def test_period_limits(self):
        with self.db.connect() as conn:
            with self.assertRaises(store.Invalid):
                ai_context.build(conn, DAY, DAY - timedelta(days=1))
            with self.assertRaises(store.Invalid):
                ai_context.build(conn, DAY, DAY + timedelta(days=40))


class ValidationTests(AIDataTestCase):
    def raw(self):
        s = f"S{self.first}"
        return {"summary": "인증 모듈 작업", "projects": [{
            "project": "Gov24 백엔드",
            "done": [{"text": "리프레시 토큰 처리 분석", "refs": [s, "S999999", s]},
                     {"text": "근거 없는 성과", "refs": ["S424242"]}],
            "next": [{"text": "AI가 지어낸 계획", "refs": [s]}, {"text": "", "refs": []}],
            "issues": [{"text": "예전 형식의 이슈 칸", "refs": [s]}],  # no longer asked for: ignored
        }]}

    def test_refs_verified_and_flags(self):
        report = ai_report.validate("weekly", self.context(), self.raw())
        [block] = report.projects
        self.assertEqual([(i.text, i.refs, i.flag) for i in block.done],
                         [("리프레시 토큰 처리 분석", [f"S{self.first}"], None), ("근거 없는 성과", [], "no_evidence")])
        self.assertEqual([i.flag for i in block.next], ["unconfirmed_plan"])
        self.assertNotIn("예전 형식의 이슈 칸", ai_report.render_text(report))
        self.assertNotIn("issues", json.dumps(ai_report.SCHEMA))
        self.assertEqual(report.dropped_refs, 2)
        self.assertEqual(block.seconds, 50 * 60)

    def test_issue_numbers_must_come_from_the_cited_records(self):
        s = f"S{self.first}"  # windows: "gov24-api – AuthService.java", "JWT refresh token"
        with self.db.connect() as conn:
            sessionstore.update_session(conn, self.first, {"title": "GOV-1423 AuthService 정리"})
        raw = {"summary": "GOV-1423 작업과 GOV-9999 검토", "projects": [
            {"project": "GOV-1418 토큰 버그", "done": [
                {"text": "GOV-1418 AuthService 수정", "refs": [s]},       # number from another piece of work
                {"text": "GOV-1423 AuthService 정리 (MR !77)", "refs": [s]},  # GOV-1423 yes, !77 no
                {"text": "AuthService.java 정리", "refs": [s]}],
             "next": [{"text": "gov-1423 리뷰 반영", "refs": [s]}]},       # case does not matter
            {"project": "GOV-1423 정리", "done": [{"text": "정리", "refs": [s]}], "next": []}]}
        report = ai_report.validate("weekly", self.context(), raw)
        wrong, right = report.projects
        self.assertEqual((wrong.name_flag, right.name_flag), ("unbacked_key", None))
        self.assertEqual([i.flag for i in wrong.done], ["unbacked_key", "unbacked_key", None])
        self.assertEqual([i.flag for i in wrong.next], [None])  # the title the user wrote names it
        self.assertEqual(report.summary_flag, "unbacked_key")  # GOV-9999 is nowhere in the data
        self.assertEqual(ai_report.unbacked_keys("GOV-14 정리", [s], self.context().ref_text), ["GOV-14"])
        text = ai_report.render_text(report)
        self.assertIn("## GOV-1418 토큰 버그 (50분) (번호가 근거에 없음 — 확인 필요)", text)
        self.assertIn("- GOV-1418 AuthService 수정 (번호가 근거에 없음 — 확인 필요)", text)

    def test_finished_claims_need_the_users_own_words(self):
        s = f"S{self.first}"
        raw = {"summary": "", "projects": [{"project": "Gov24 백엔드", "next": [], "done": [
            {"text": "토큰 갱신 버그 수정 완료", "refs": [s]},   # auto session title: not proof
            {"text": "해결 방안 검토", "refs": [s]},             # not a claim
            {"text": "토큰 갱신 로직 수정", "refs": [s]},
        ]}]}
        report = ai_report.validate("weekly", self.context(), raw)
        [block] = report.projects
        self.assertEqual([i.flag for i in block.done], ["unconfirmed_done", None, None])
        self.assertIn("토큰 갱신 버그 수정 완료 (완료 근거 없음 — 확인 필요)", ai_report.render_text(report))
        with self.db.connect() as conn:  # the user's own session title says so
            sessionstore.update_session(conn, self.first, {"title": "토큰 갱신 버그 수정 완료"})
        [block] = ai_report.validate("weekly", self.context(), raw).projects
        self.assertIsNone(block.done[0].flag)

    def test_words_about_another_issue_do_not_finish_this_one(self):
        """One title of the user's, two issues, only one of them done."""
        with self.db.connect() as conn:
            sessionstore.update_session(conn, self.first, {"title": "GOV-1418 QA 확인 완료로 종료. GOV-1423 응답 3.2초"})
        s = f"S{self.first}"
        raw = {"summary": "GOV-1418 버그 수정 및 GOV-1423 조회 성능 개선을 완료하고, K8s 전환을 진행함. "
                          "GOV-1418은 QA 확인 후 종료함.",
               "projects": [{"project": "Gov24 백엔드", "next": [], "done": [
                   {"text": "GOV-1423 조회 성능 개선 완료", "refs": [s]},
                   {"text": "GOV-1418 QA 확인 후 종료", "refs": [s]},
               ]}]}
        report = ai_report.validate("weekly", self.context(), raw)
        self.assertEqual([i.flag for i in report.projects[0].done], ["unconfirmed_done", None])
        self.assertEqual(report.summary_flag, "unconfirmed_done")  # its first sentence calls GOV-1423 done
        self.assertIn("(완료 근거 없음 — 확인 필요)", ai_report.render_text(report).splitlines()[2])
        self.assertEqual(ai_report.to_dict(report, self.context())["summary_flag"], "unconfirmed_done")

        raw["summary"] = "GOV-1418은 QA 확인 후 종료했고, GOV-1423은 인덱스를 검토 중."
        self.assertIsNone(ai_report.validate("weekly", self.context(), raw).summary_flag)

    def test_a_title_the_user_wrote_backs_a_finished_claim(self):
        with self.db.connect() as conn:
            sessionstore.update_session(conn, self.first, {"title": "AuthService 리팩터링 마무리"})
        raw = {"summary": "", "projects": [{"project": "Gov24 백엔드", "next": [], "done": [
            {"text": "AuthService 리팩터링 마무리", "refs": [f"S{self.first}"]}]}]}
        [block] = ai_report.validate("weekly", self.context(), raw).projects
        self.assertIsNone(block.done[0].flag)

    def test_work_folder_left_out_by_ai_is_restored(self):
        ctx = self.context()
        ctx.folder_changes = {"gov24-api": ["C77"], "docs": ["C78"]}
        ctx.refs |= {"C77", "C78"}
        raw = {"summary": "", "projects": [{"project": "문서", "done": [{"text": "x", "refs": ["C78"]}],
                                            "next": []}]}
        report = ai_report.validate("weekly", ctx, raw)
        self.assertEqual([(b.project, b.missing_from_ai) for b in report.projects],
                         [("문서", False), ("gov24-api (작업 폴더)", True)])
        self.assertIn("파일 변경이 AI 초안에 빠졌습니다", ai_report.render_text(report))

    def test_groups_are_named_by_the_model_and_timed_by_their_sessions(self):
        s = f"S{self.first}"
        raw = {"summary": "", "projects": [
            {"project": "", "done": [{"text": "회의", "refs": []}], "next": []},
            {"project": "GOV-1418 토큰 버그", "done": [{"text": "분석", "refs": [s]}, {"text": "수정", "refs": [s]}],
             "next": []}]}
        report = ai_report.validate("weekly", self.context(), raw)
        self.assertEqual([(b.project, b.seconds) for b in report.projects],
                         [("GOV-1418 토큰 버그", 50 * 60), ("기타", 0)])  # a session counts once

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

    def test_offline_package_has_no_claude_models(self):
        # The offline package leaves out mcp; Claude Code models disappear and a saved one counts as off.
        with self.db.connect() as conn:
            ai_service.update_settings(conn, {"model": "claude-code:haiku"}, False)
            with mock.patch.object(ai_service.ai_claude, "bundled", return_value=False):
                s = ai_service.get_settings(conn, demo=False)
                self.assertFalse([m for m in s["models"] if m.startswith("claude-code")])
                self.assertEqual((s["enabled"], s["claude_bundled"], s["claude_installed"]), (False, False, False))
                with self.assertRaises(store.Invalid):
                    ai_service.update_settings(conn, {"model": "claude-code:sonnet"}, False)


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
        names = sorted(re.sub(r" [+-]\d+", "", f.rsplit("/", 1)[-1]) for f in fix["files"])  # line counts aside
        self.assertEqual(names, ["LegacyTokenTest.java (삭제)", "TokenRefreshFilter.java (수정)"])
        excerpt = "\n".join(fix["diff_excerpt"])
        self.assertIn("+            refreshTokens.delete(stored);", excerpt)
        self.assertEqual(ctx.folder_changes, {"gov24-minwon-api": [c["ref"] for c in changes]})

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

    def commit(self, conn, day, hh, mm, path, text):
        """One more snapshot of the demo API folder with `path` set to `text`."""
        from workhistory import snapshots
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
            files[path] = snapshots.add_blob(repo, text.encode())
            snapshots.commit_snapshot(conn, repo, fid, files, local_ts(day, hh, mm))
        finally:
            repo.close()

    def test_snapshots_close_together_go_as_one_change(self):
        day = self.monday - timedelta(days=6)  # demo changes at 11:45 and 17:25
        with self.db.connect() as conn:
            self.commit(conn, day, 16, 0, "docs/note.md", "one\n")
            self.commit(conn, day, 16, 5, "docs/note.md", "one\ntwo\n")
            self.commit(conn, day, 16, 10, "docs/other.md", "x\n")
            ctx = ai_context.build(conn, day, day, self.repos)
        changes = ctx.payload["changes"]
        self.assertEqual([c["time"] for c in changes], ["11:45", "16:00-16:10", "17:25"])
        burst = changes[1]
        self.assertEqual(burst["files"], ["docs/note.md (추가 +2 -0)", "docs/other.md (추가 +1 -0)"])
        self.assertIn("+two", burst["diff_excerpt"])          # first version to last, in one excerpt
        self.assertEqual(ctx.folder_changes["gov24-minwon-api"], [c["ref"] for c in changes])
        self.assertTrue(all(c["ref"] in ctx.refs and c["ref"] in ctx.sources for c in changes))

    def test_a_busy_period_is_trimmed_to_fit(self):
        last_week = (self.monday - timedelta(days=7), self.monday - timedelta(days=1))
        with self.db.connect() as conn:
            full = ai_context.build(conn, *last_week, self.repos)
            with mock.patch.object(ai_context, "PAYLOAD_CHARS", 6000):
                small = ai_context.build(conn, *last_week, self.repos)
        size = len(ai_context.dumps(small.payload))
        self.assertLess(size, len(ai_context.dumps(full.payload)))
        self.assertLessEqual(size, 6000)
        json.loads(ai_context.dumps(small.payload))  # what is sent is valid JSON
        self.assertLessEqual(max(len(s["windows"]) for s in small.payload["sessions"]), 2)
        # Every work folder is still there, with refs the report can cite.
        self.assertEqual(set(small.folder_changes), set(full.folder_changes))
        self.assertTrue(all(c["ref"] in small.refs for c in small.payload["changes"]))
        self.assertEqual(set(small.session_seconds), {s["ref"] for s in small.payload["sessions"]})

    def test_diff_lines_are_shared_between_changes(self):
        last_week = (self.monday - timedelta(days=7), self.monday - timedelta(days=1))
        with self.db.connect() as conn, mock.patch.object(ai_context, "DIFF_LINES_TOTAL", 30):
            ctx = ai_context.build(conn, *last_week, self.repos)
        changes = ctx.payload["changes"]
        with_diff = [c for c in changes if "diff_excerpt" in c]
        self.assertGreater(len(with_diff), 3)                  # not all spent on the first change
        self.assertIn("diff_excerpt", changes[-1])
        lines = sum(len([l for l in c["diff_excerpt"] if not l.startswith("# ")]) for c in with_diff)
        self.assertLessEqual(lines, 30)

    def test_folder_with_file_changes_is_restored(self):
        ctx = self.build(-6)
        report = ai_report.validate("daily", ctx, {"summary": "", "projects": []})
        self.assertEqual([(b.project, b.missing_from_ai) for b in report.projects],
                         [("gov24-minwon-api (작업 폴더)", True)])

    def test_change_refs_count_as_evidence(self):
        ctx = self.build(-6)
        ref = ctx.payload["changes"][0]["ref"]
        report = ai_report.validate("daily", ctx, {"summary": "", "projects": [
            {"project": "Gov24 민원 API", "done": [{"text": "리프레시 토큰 재사용 차단", "refs": [ref]}],
             "next": [{"text": "변경만 근거인 계획", "refs": [ref]}]}]})
        block = next(b for b in report.projects if b.project == "Gov24 민원 API")
        self.assertEqual((block.done[0].flag, block.next[0].flag), (None, "unconfirmed_plan"))


if __name__ == "__main__":
    unittest.main()
