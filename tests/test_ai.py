import importlib.util
import json
import re
import unittest
from datetime import date, timedelta
from unittest import mock

from workhistory import ai_context, ai_report, ai_service, service, store
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
            self.unused = service.create_project(conn, {"name": "안 쓴 프로젝트"})["id"]
            folder = conn.execute("INSERT INTO watched_folders (path, label, created_at) "
                                  "VALUES ('C:\\app\\gov24-api', 'gov24-api', 0)").lastrowid
            conn.execute("INSERT INTO file_state (folder_id, path, size, mtime_ns, blob_id) "
                         "VALUES (?, 'src/AuthService.java', 0, 0, '')", (folder,))
            self.note = service.create_note(conn, {"day": DAY.isoformat(), "body": "QA 서버 배포 대기 중",
                                                   "project_id": self.gov})["id"]
        self.aw = 0
        self.add(9, 0, 9, 40, "idea64.exe", "gov24-api – AuthService.java")         # the work folder
        self.add(9, 40, 9, 50, "chrome.exe", "JWT refresh token - Google Chrome")   # 웹
        self.add(9, 50, 10, 0, "chrome.exe", "[GOV-1423] 토큰 정리 - Jira - Google Chrome")
        self.add(10, 30, 10, 30, "slack.exe", "Slack", s2=40)  # 40 seconds of 소통: too short to send
        self.add(11, 0, 11, 30, "", "")                          # 업무 외
        self.folder, self.web = "W1", "W2"

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def add(self, h1, m1, h2, m2, app, title, s2=0):
        self.aw += 1
        start, end = local_ts(DAY, h1, m1), local_ts(DAY, h2, m2, s2)
        with self.db.connect() as conn:
            conn.execute("INSERT INTO window_events (source_id, aw_id, start_ts, end_ts, app, title) "
                         "VALUES (?, ?, ?, ?, ?, ?)", (self.src, self.aw, start, end, app, title))

    def context(self):
        with self.db.connect() as conn:
            return ai_context.build(conn, DAY, DAY)


class ContextTests(AIDataTestCase):
    def test_work_records_are_sent(self):
        ctx = self.context()
        text = json.dumps(ctx.payload, ensure_ascii=False)
        self.assertNotIn("Slack", text)                        # under a minute
        self.assertNotIn("안 쓴 프로젝트", text)               # irrelevant project
        self.assertNotIn("Gov24 백엔드", text)                  # no projects: the model groups the work
        self.assertNotIn("QA 서버 배포 대기", text)              # notes are not sent
        self.assertEqual(ctx.payload["offwork_minutes"], 30)
        folder, web = ctx.payload["work"]
        self.assertEqual((folder["ref"], folder["folder"], folder["minutes"], folder["times"]),
                         ("W1", "gov24-api", 40, "09:00-09:40"))
        self.assertEqual(folder["windows"], [{"app": "IntelliJ IDEA", "title": "gov24-api – AuthService.java",
                                              "minutes": 40}])
        self.assertEqual((web["ref"], web["kind"], web["minutes"]), ("W2", "웹", 20))
        self.assertEqual(ctx.refs, {"W1", "W2"})
        self.assertEqual(ctx.work_seconds, {"W1": 40 * 60})      # only work folders give a group time
        self.assertEqual(ctx.folder_refs, {"gov24-api": ["W1"]})
        self.assertEqual(ctx.sources["W1"]["type"], "work")

    def test_period_limits(self):
        with self.db.connect() as conn:
            with self.assertRaises(store.Invalid):
                ai_context.build(conn, DAY, DAY - timedelta(days=1))
            with self.assertRaises(store.Invalid):
                ai_context.build(conn, DAY, DAY + timedelta(days=40))


class ValidationTests(AIDataTestCase):
    def raw(self):
        return {"summary": "인증 모듈 작업", "projects": [{
            "project": "Gov24 백엔드",
            "done": [{"text": "리프레시 토큰 처리 분석", "refs": [self.folder, "W999", self.folder, self.web]},
                     {"text": "근거 없는 성과", "refs": ["S424242"]}],
            "next": [{"text": "AI가 지어낸 계획", "refs": [self.folder]}, {"text": "", "refs": []}],
            "issues": [{"text": "예전 형식의 이슈 칸", "refs": [self.folder]}],  # no longer asked for: ignored
        }]}

    def test_refs_verified_and_flags(self):
        report = ai_report.validate("weekly", self.context(), self.raw())
        [block] = report.projects
        self.assertEqual([(i.text, i.refs, i.flag) for i in block.done],
                         [("리프레시 토큰 처리 분석", ["W1", "W2"], None), ("근거 없는 성과", [], "no_evidence")])
        self.assertNotIn("예전 형식의 이슈 칸", ai_report.render_text(report))
        self.assertNotIn("AI가 지어낸 계획", ai_report.render_text(report))  # plans are the user's to write
        self.assertNotIn("issues", json.dumps(ai_report.SCHEMA))
        self.assertNotIn("next", json.dumps(ai_report.SCHEMA))
        self.assertEqual(report.dropped_refs, 2)
        self.assertEqual(block.seconds, 40 * 60)  # the work folder's time; 웹 adds none

    def test_what_was_done_starts_in_progress(self):
        """Finished or not is the user's call: every line of what was done is 진행 중 in the draft."""
        f = self.folder
        raw = {"summary": "토큰 갱신 로직 수정 진행", "projects": [{"project": "Gov24 백엔드", "done": [
            {"text": "토큰 갱신 로직 수정", "refs": [f]},
            {"text": "AuthService 정리 (완료)", "refs": [f]},  # a status from the model is replaced
        ], "next": [{"text": "리뷰 반영", "refs": [f]}]}]}
        report = ai_report.validate("weekly", self.context(), raw)
        self.assertEqual([i.flag for i in report.projects[0].done], [None, None])  # no word checks
        text = ai_report.render_text(report)
        self.assertIn("[금주 실적]\n- 토큰 갱신 로직 수정 (진행 중)\n- AuthService 정리 (진행 중)", text)
        self.assertIn("[차주 계획]\n- \n", text)  # left empty for the user
        self.assertNotIn("리뷰 반영", text)

    def test_issue_numbers_are_not_checked(self):
        """A wrong issue number is for the user to notice: no mark on the line, the name or the summary."""
        raw = {"summary": "GOV-9999 검토", "projects": [{"project": "GOV-1418 토큰 버그", "done": [
            {"text": "GOV-1418 AuthService 수정 (MR !77)", "refs": [self.folder]}]}]}
        report = ai_report.validate("weekly", self.context(), raw)
        self.assertEqual([i.flag for i in report.projects[0].done], [None])
        self.assertNotIn("확인 필요", ai_report.render_text(report))

    def test_work_folder_left_out_by_ai_is_restored(self):
        ctx = self.context()
        ctx.folder_refs = {"gov24-api": ["W1", "C77"], "docs": ["C78"]}
        ctx.folder_seconds = {"gov24-api": 40 * 60}
        ctx.refs |= {"C77", "C78"}
        raw = {"summary": "", "projects": [{"project": "문서", "done": [{"text": "x", "refs": ["C78"]}],
                                            "next": []}]}
        report = ai_report.validate("weekly", ctx, raw)
        self.assertEqual([(b.project, b.missing_from_ai, b.seconds) for b in report.projects],
                         [("문서", False, 0), ("gov24-api (작업 폴더)", True, 40 * 60)])
        self.assertIn("이 작업 폴더의 기록이 AI 초안에 빠졌습니다", ai_report.render_text(report))

    def test_groups_are_named_by_the_model_and_timed_by_their_records(self):
        f = self.folder
        raw = {"summary": "", "projects": [
            {"project": "", "done": [{"text": "회의", "refs": []}], "next": []},
            {"project": "GOV-1418 토큰 버그", "done": [{"text": "분석", "refs": [f]}, {"text": "수정", "refs": [f]}],
             "next": []}]}
        report = ai_report.validate("weekly", self.context(), raw)
        self.assertEqual([(b.project, b.seconds) for b in report.projects],
                         [("GOV-1418 토큰 버그", 40 * 60), ("기타", 0)])  # a record counts once

    def test_a_record_shared_by_two_groups_is_split(self):
        f = self.folder
        raw = {"summary": "", "projects": [
            {"project": "토큰 버그", "done": [{"text": "a", "refs": [f]}], "next": []},
            {"project": "정리", "done": [{"text": "b", "refs": [f]}], "next": []}]}
        report = ai_report.validate("weekly", self.context(), raw)
        self.assertEqual([b.seconds for b in report.projects], [20 * 60, 20 * 60])  # never counted twice

    def test_garbage_output_does_not_crash(self):
        for raw in (None, [], {"projects": "x"}, {"projects": [1, {"done": "x"}]}):
            ai_report.validate("daily", self.context(), raw)

    def test_render_text(self):
        text = ai_report.render_text(ai_report.validate("weekly", self.context(), self.raw()))
        self.assertIn("# 주간보고 · 2026-09-30", text)
        self.assertIn("## Gov24 백엔드 (40분)", text)
        self.assertIn("[금주 실적]\n- 리프레시 토큰 처리 분석 (진행 중)\n- 근거 없는 성과 (근거 없음 — 확인 필요) (진행 중)",
                      text)
        self.assertIn("[차주 계획]\n- \n", text)
        self.assertIn("(작업 폴더 밖: 웹 20분)", text)  # 40 seconds of 소통 is left out
        self.assertIn("(보고서에서 제외한 업무 외 시간: 30분)", text)
        self.assertNotIn("W1", text)  # references stay out of the pasted text

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
        self.assertEqual([r for r in ctx.folder_refs["gov24-minwon-api"] if r.startswith("C")],
                         [c["ref"] for c in changes])

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
        self.assertEqual([r for r in ctx.folder_refs["gov24-minwon-api"] if r.startswith("C")],
                         [c["ref"] for c in changes])
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
        self.assertLess(max(len(w["windows"]) for w in small.payload["work"]),
                        max(len(w["windows"]) for w in full.payload["work"]))
        # Every work folder is still there, with refs the report can cite.
        self.assertEqual(set(small.folder_refs), set(full.folder_refs))
        self.assertTrue(all(c["ref"] in small.refs for c in small.payload["changes"]))
        self.assertLessEqual(set(small.work_seconds), {w["ref"] for w in small.payload["work"]})

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

    def test_a_weeks_folder_time_is_shared_by_the_days_each_work_changed_files(self):
        """One folder, two pieces of work in one week: each gets the days it changed files on."""
        monday = self.monday - timedelta(days=7)
        with self.db.connect() as conn:
            ctx = ai_context.build(conn, monday, monday + timedelta(days=6), self.repos)
        [w] = [x["ref"] for x in ctx.payload["work"] if x.get("folder") == "gov24-minwon-api"]
        changes = {c["ref"]: c["date"] for c in ctx.payload["changes"] if c["folder"] == "gov24-minwon-api"}
        token_days = {(monday + timedelta(days=i)).isoformat() for i in (0, 1)}   # Mon, Tue: the token bug
        token = [r for r, d in changes.items() if d in token_days]
        index = [r for r, d in changes.items() if d not in token_days]           # later: the index work
        raw = {"summary": "", "projects": [
            {"project": "토큰 버그", "done": [{"text": "토큰", "refs": [w, *token]}]},
            {"project": "조회 성능", "done": [{"text": "인덱스", "refs": [w, *index]}]}]}
        report = ai_report.validate("weekly", ctx, raw)
        got = {b.project: b.seconds for b in report.projects if not b.missing_from_ai}
        days = ctx.work_days[w]
        index_days = {changes[r] for r in index}
        neither = sum(sec for d, sec in days.items() if d not in token_days | index_days)
        self.assertAlmostEqual(got["토큰 버그"], sum(days.get(d, 0) for d in token_days) + neither / 2)
        self.assertAlmostEqual(got["조회 성능"], sum(days.get(d, 0) for d in index_days) + neither / 2)
        self.assertAlmostEqual(sum(got.values()), ctx.work_seconds[w])  # nothing counted twice

    def test_change_refs_count_as_evidence(self):
        ctx = self.build(-6)
        ref = ctx.payload["changes"][0]["ref"]
        report = ai_report.validate("daily", ctx, {"summary": "", "projects": [
            {"project": "Gov24 민원 API", "done": [{"text": "리프레시 토큰 재사용 차단", "refs": [ref]}]}]})
        block = next(b for b in report.projects if b.project == "Gov24 민원 API")
        self.assertIsNone(block.done[0].flag)


class EchoModel:
    """Cites every reference it was sent: one group per daily draft, everything merged for the week."""

    name = "echo"

    def __init__(self):
        self.calls = []

    def check(self):
        pass

    def generate_json(self, system, prompt, schema, max_output_tokens=16000, thinking=None):
        self.calls.append((system, prompt))
        body = json.loads(prompt[prompt.index("{"):])
        if "days" in body:  # the merge step
            refs = [r for d in body["days"] for w in d["work"] for it in w["done"] for r in it["refs"]]
            reply = {"summary": "주간", "projects": [{"project": "합친 일", "done": [{"text": "합친 실적", "refs": refs}]}]}
        else:
            refs = [w["ref"] for w in body["work"]] + [c["ref"] for c in body["changes"]]
            reply = {"summary": "하루", "projects": [{"project": "하루 일", "done": [{"text": "실적", "refs": refs}]}]}
        return reply, Usage(input_tokens=10, output_tokens=5, thinking_tokens=0, requests=1)


@unittest.skipUnless(HAS_DULWICH, "dulwich not installed (run tests with .venv python)")
class SplitWeekTests(unittest.TestCase):
    """A week too big for one request is written day by day, then merged: nothing is left out."""

    def setUp(self):
        from pathlib import Path
        from workhistory import demo
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        self.repos = Path(self.db.path).parent / "snapshots"
        demo.seed(self.db, self.repos)
        today = date.today()
        self.last_monday = today - timedelta(days=today.weekday() + 7)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def generate(self, model):
        with self.db.connect() as conn:
            return ai_service.generate_report(conn, "weekly", self.last_monday, demo=True,
                                              factory=lambda n: model, repos_dir=self.repos)

    def test_a_week_that_fits_is_one_request(self):
        model = EchoModel()
        out = self.generate(model)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(out["notes"], [])
        # A week is sent as one record per work folder (and 웹/소통/기타), with its minutes per day.
        work = json.loads(model.calls[0][1][model.calls[0][1].index("{"):])["work"]
        names = [w.get("folder") or w.get("kind") for w in work]
        self.assertEqual(len(names), len(set(names)))
        api = next(w for w in work if w.get("folder") == "gov24-minwon-api")
        self.assertNotIn("date", api)
        self.assertRegex(api["days"], r"^월 \d+/\d+ \d+분, 화 ")
        per_day = [int(m) for m in re.findall(r"(\d+)분", api["days"])]
        self.assertLessEqual(abs(api["minutes"] - sum(per_day)), len(per_day))  # the days add up (rounding)

    def test_a_big_week_is_written_day_by_day_and_merged(self):
        model = EchoModel()
        with mock.patch.object(ai_context, "PAYLOAD_CHARS", 3000):  # the whole week no longer fits
            out = self.generate(model)
            with self.db.connect() as conn:
                preview = ai_service.preview(conn, "weekly", self.last_monday, self.repos)
        daily, merge = model.calls[:-1], model.calls[-1]
        self.assertGreater(len(daily), 1)
        self.assertEqual(merge[0], ai_report.MERGE_SYSTEM)
        # Every day was sent with its own W numbers: no number is used by two days.
        sent = [w["ref"] for _, prompt in daily for w in json.loads(prompt[prompt.index("{"):])["work"]]
        self.assertEqual(len(sent), len(set(sent)))
        # The merged draft keeps every reference the days had, so nothing is restored or dropped.
        self.assertEqual(out["dropped_refs"], 0)
        self.assertEqual([p["project"] for p in out["projects"]], ["합친 일"])
        self.assertEqual(set(out["projects"][0]["done"][0]["refs"]), set(sent) | {
            c["ref"] for _, prompt in daily for c in json.loads(prompt[prompt.index("{"):])["changes"]})
        self.assertGreater(out["projects"][0]["seconds"], 0)
        self.assertIn(f"{len(daily)}일을 하루씩 정리한 뒤", out["notes"][0])
        self.assertEqual(out["usage"]["input_tokens"], 10 * len(model.calls))
        self.assertIn("합치기", preview["prompt"])
        self.assertIn(ai_report.MERGE_SYSTEM, preview["system"])


if __name__ == "__main__":
    unittest.main()
