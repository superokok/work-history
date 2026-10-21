# AI features (Claude Code)

Goal for the AX hackathon: AI that works on the user's *structured, chosen*
work history — not on screenshots — and never states more than the data
supports.

## Features

1. **Work report drafts** (daily 업무일지 / weekly 주간보고): per project,
   금주 실적 / 차주 계획 / 이슈. Every bullet cites the records it came from.
2. **Ask your history** ("지난주 로그인 버그 어디까지 했지?"): an agent that
   searches the local history with tools and answers with citations.

## Claude Code path (MCP)

`workhistory/mcp_server.py` exposes the same read-only tools as the in-app
agent (`ai_agent.HistoryTools`) over MCP stdio, plus `get_report_data`
(report payload + the report rules). Claude Code does the reasoning with the
user's own account, so the app needs no API key on this path. Consent is a
separate setting (`mcp_enabled`) controlled by one switch in the app, which
also registers / unregisters the server with Claude Code through its CLI
(`workhistory/claude_link.py`); enabling rolls consent back if registration
fails, disabling withdraws consent first. The demo database is always
allowed. Difference from the in-app path: Claude Code writes the final
answer, so the app cannot re-verify its citations; every tool result item
carries a reference the user can check.

## Provider: Claude Code (no API key)

`workhistory/ai_claude.py` runs the Claude Code installed and signed in on
the PC headless (`claude -p --output-format json`); usage counts against the
user's Claude account. AI is **off** until the user picks a model in
개인정보 → AI 설정 (`claude-code`, `claude-code:sonnet|opus|haiku`); that
choice is the consent to send records through Claude Code (the UI asks
first), and "사용 안 함" withdraws it. The demo database may always be used.

Every run is locked down:

- `--tools ""`: no built-in tools (no file access, shell or web);
  `--strict-mcp-config`: no MCP servers except the one the app passes;
  `--setting-sources project` in an empty temporary working directory: no
  user hooks, permissions or CLAUDE.md; `--no-session-persistence`;
  `--max-budget-usd 5` per run as a runaway guard.
- The prompt goes through stdin. On Windows the native `claude.exe` behind
  the npm `.cmd` shim is started directly, so no argument passes through
  cmd.exe.
- Reports: `--json-schema` with the report schema; `structured_output` goes
  through `ai_report.validate` (reference check, flags).
- Ask: Claude Code runs the tool loop itself against the app's MCP server
  (`--mcp-config`, only the five history tools in `--allowedTools`). The app
  starts that server with `--trace <file>`: it logs every tool call and the
  references it returned, and `verify_answer` checks the answer's citations
  against that trace; references no tool returned are removed.
- "연결 확인" runs `claude auth status` (no content sent).

Measured on the demo data (claude-code:sonnet, 2026-10-01): ask 12 s with 5
verified citations; `evals/report_eval.py` passed 4/4 with 0 dropped
references, median 30 s per report, but one weekly case took 166-214 s in
both runs (cause not yet known).

Re-measured 2026-10-21 (Claude Code 2.1.293, claude-code:sonnet,
`--runs 2`): **8/8 passed** (all required facts present, no over-claims, no
other days' work in daily reports), 0 dropped references, 0 flags, 0 restored
projects; median 7.4 s, max 11.3 s per report (the slow weekly case is gone).

## Replacing the model (e.g. a 폐쇄망 LLM)

The AI code is written against `ai_client.Model`, not against Claude Code:
`generate_json(system, prompt, schema)` for reports, and either
`run_mcp(...)` (the model's own agent uses the app's MCP server) or
`run_tools(system, prompt, tools, execute)` (the app runs the tool loop
in-process and executes `HistoryTools` itself) for questions. Reference
checks, redaction, consent and logging stay in the app, so a new provider is
one class plus a factory entry. A Gemini implementation existed until
2026-10-01 and was removed by decision; it is in no release.

## Data protection

- Nothing is sent until the user turns AI on (see above); the demo
  database (synthetic data) is always allowed.
- Only work data is sent: recorded file changes (C: files, line counts and
  a bounded excerpt of changed lines), sessions that are not excluded and not
  "업무 외" (S), the user's notes (N) and projects with next actions (P).
  "업무 외" time is sent as a total only.
- Every free-text field passes `redact.py` (keys, tokens, passwords, resident
  registration and card numbers) before it is sent.
- Diff excerpts are capped: 25 lines per file, 50 per change, 600 per request,
  200 characters per line.
- The exact payload can be previewed in the UI before sending.
- Logged locally per call: kind, model, token counts, latency, success —
  never prompt or response text.

## Honesty rules (enforced in code, not only in prompts)

- Every reference the model returns is checked against the records that were
  actually sent; unknown references are dropped.
- A 실적/이슈 bullet without a valid reference is shown as "근거 없음".
- 차주 계획 may only come from the user's own notes and next actions
  (references to notes/next actions); otherwise it is marked as a suggestion
  for the user to confirm.
- AI text is always labelled as an AI draft and must be saved by the user to
  become a report.

## Quality process

- Unit tests with a fake `claude` CLI and fake models cover the command line,
  error mapping, the MCP trace check, validation and guards.
- A small eval set on curated demo data (questions with expected referenced
  sessions; report checks) is run against the live model before changes are
  accepted.
