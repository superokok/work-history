"""Live quality evaluation of AI report drafts on the fictional demo scenario.

Builds a fresh demo database in a temporary folder (real data is never used),
asks Claude Code for reports, and checks each draft against
facts we know from the scenario:

- required facts must appear (in the project's items),
- over-claims must not appear anywhere (e.g. "GOV-1423 완료" while it is not),
- a daily report must not contain other days' work,
- references must all verify; flags and restored projects are reported.

Usage (Claude Code installed and signed in):
    .venv/Scripts/python.exe -m evals.report_eval [--runs 2] [--model claude-code:sonnet]
"""

import argparse
import json
import re
import shutil
import statistics
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

from workhistory import ai_context, ai_report, ai_service, demo
from workhistory.ai_client import AIError
from workhistory.db import Database

GOV, K8S = "Gov24 민원 API", "K8s 전환 PoC"

# offset: day offset from this week's Monday (the demo scenario is defined the same way)
CASES = [
    {
        "name": "last week (weekly)", "kind": "weekly", "offset": -7,
        "require": {GOV: [r"GOV-1418|리프레시|토큰", r"GOV-1423|인덱스|조회"], K8S: [r"[Hh]elm|배포|차트"]},
        # GOV-1423 was not finished last week (DBA review pending).
        "forbid": [r"GOV-1423[^\n]{0,40}(완료|해결)", r"(조회|성능)[^\n]{0,15}개선[^\n]{0,10}(완료|해결)"],
    },
    {
        "name": "this week (weekly)", "kind": "weekly", "offset": 0,
        "require": {GOV: [r"count|카운트|MR !318|!318", r"DBA"]},
        # Cursor paging (GOV-1430) is only being explored.
        "forbid": [r"GOV-1430[^\n]{0,30}(완료|적용|해결)", r"커서[^\n]{0,20}(완료|적용 완료)"],
    },
    {
        "name": "fix day (daily)", "kind": "daily", "offset": -6,
        "require": {GOV: [r"TokenRefreshFilter|무효화|토큰"]},
        "forbid": [r"GOV-1423", r"[Hh]elm", r"인덱스"],  # none of this happened that day
    },
    {
        "name": "K8s day (daily)", "kind": "daily", "offset": -5,
        "require": {K8S: [r"[Hh]elm|배포|values|Deployment|deployment"]},
        "forbid": [r"GOV-1423", r"인덱스"],
    },
]


def evaluate(case: dict, report: ai_report.Report) -> list[str]:
    failures = []
    blocks = {b.project: b for b in report.projects}
    for project, patterns in case["require"].items():
        block = blocks.get(project)
        text = "\n".join(i.text for s in (block.done, block.next, block.issues) for i in s) if block else ""
        for p in patterns:
            if not re.search(p, text):
                failures.append(f"missing in {project}: /{p}/")
    everything = report.summary + "\n" + "\n".join(
        i.text for b in report.projects for s in (b.done, b.next, b.issues) for i in s)
    for p in case["forbid"]:
        m = re.search(p, everything)
        if m:
            failures.append(f"forbidden /{p}/ -> '{m.group(0)}'")
    return failures


def _generate_with_patience(model, case, ctx, args):
    """Retry usage-limit/overload errors after a pause (they are about capacity, not the prompt)."""
    for attempt in range(3):
        try:
            return model.generate_json(ai_report.SYSTEM, ai_report.prompt(case["kind"], ctx),
                                       ai_report.SCHEMA, thinking=args.thinking)
        except AIError as e:
            if e.kind not in ("quota", "server") or attempt == 2:
                raise
            print(f"       ({e.kind}: waiting {args.retry_wait}s before retrying)")
            time.sleep(args.retry_wait)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--pause", type=float, default=0, help="seconds between calls")
    parser.add_argument("--retry-wait", type=float, default=65, help="seconds to wait after quota/overload")
    parser.add_argument("--model", default=ai_service.DEFAULT_MODEL, choices=ai_service.MODELS)
    parser.add_argument("--thinking", default=None, choices=[None, "low", "medium", "high"])
    parser.add_argument("--out", type=Path, help="write raw results as JSON")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")

    try:
        model = ai_service.default_factory(args.model)
    except AIError as e:
        print(e.message)
        return 2

    work = Path(tempfile.mkdtemp(prefix="wh-eval-"))
    results, all_ok = [], True
    try:
        db = Database(work / "demo.db")
        db.migrate()
        demo.seed(db, work / "snapshots")
        monday = date.today() - timedelta(days=date.today().weekday())
        for case in CASES:
            day = monday + timedelta(days=case["offset"])
            start, end = ((day, day) if case["kind"] == "daily"
                          else (day - timedelta(days=day.weekday()), day - timedelta(days=day.weekday()) + timedelta(days=6)))
            with db.connect() as conn:
                ctx = ai_context.build(conn, start, end, work / "snapshots")
            for run in range(args.runs):
                if results:
                    time.sleep(args.pause)  # spread calls out under a tight usage limit
                started = time.time()
                try:
                    raw, usage = _generate_with_patience(model, case, ctx, args)
                except AIError as e:
                    print(f"[ERROR] {case['name']} run {run + 1}: {e.kind} {e.message}")
                    results.append({"case": case["name"], "error": e.kind})
                    all_ok = False
                    continue
                report = ai_report.validate(case["kind"], ctx, raw)
                failures = evaluate(case, report)
                flags = [i.flag for b in report.projects for s in (b.done, b.next, b.issues) for i in s if i.flag]
                row = {"case": case["name"], "run": run + 1, "seconds": round(time.time() - started, 1),
                       "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
                       "thinking_tokens": usage.thinking_tokens, "dropped_refs": report.dropped_refs,
                       "flags": flags, "restored": [b.project for b in report.projects if b.missing_from_ai],
                       "failures": failures, "text": ai_report.render_text(report)}
                results.append(row)
                ok = not failures and not report.dropped_refs and not row["restored"]
                all_ok &= ok
                print(f"[{'PASS' if ok else 'FAIL'}] {case['name']} run {run + 1}: {row['seconds']}s, "
                      f"in {usage.input_tokens} out {usage.output_tokens} think {usage.thinking_tokens}, "
                      f"dropped {report.dropped_refs}, flags {flags}, restored {row['restored']}")
                for f in failures:
                    print(f"       - {f}")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    timed = [r["seconds"] for r in results if "seconds" in r]
    if timed:
        print(f"\nlatency median {statistics.median(timed):.1f}s, max {max(timed):.1f}s; "
              f"passed {sum(1 for r in results if 'seconds' in r and not r['failures'] and not r['dropped_refs'] and not r['restored'])}"
              f"/{len(results)}")
    if args.out:
        args.out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
