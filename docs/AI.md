# AI features (a local model first)

Goal for the AX hackathon: AI that works on the user's *structured, chosen*
work history — not on screenshots — and never states more than the data
supports.

## Features

1. **Work report drafts** (daily 업무일지 / weekly 주간보고): per group of work,
   금주 실적 (오늘 한 일), every bullet citing the records it came from, and an
   empty 차주 계획 (다음 할 일) section for the user to write (2026-11-11: plans
   are the user's, the model no longer suggests them). (An 이슈 section was
   dropped on 2026-11-10: it had nothing to come from.)
   There are no projects (2026-11-05): the model groups the records itself by
   issue key, MR, work folder or topic and names each group from the records
   ("기타" for the rest); a group's time is the work folder records it cites
   (a record cited by several groups is shared day by day: a day goes to the
   groups that cite a file change of that folder on that day, a day without
   one evenly; 웹/소통/기타 records add no time and are listed under the report
   as "작업 폴더 밖").
2. **Ask your history** ("지난주 로그인 버그 어디까지 했지?"): an agent that
   searches the local history with tools and answers with citations.

## Which model

The model is chosen in 설정 → AI 설정 (AI is off until the user picks one):

| Provider | For | In the released zip |
|---|---|---|
| A local model on this PC (llama.cpp + Qwen3.5) | the AI users run; nothing leaves the PC, works without internet | yes, with the models |
| Claude Code (headless) | development: quality comparison and evals on the demo data | no |

The only package handed out is the closed-network zip (`--offline --with-models`,
GitHub Releases), so on a user's PC the AI is always a local model. Claude Code
stays in the source as a development and comparison tool.

## Provider: a local model on this PC (llama.cpp)

`workhistory/ai_local.py`. For PCs where records must not leave the machine
or that have no internet (폐쇄망). The app ships llama.cpp's `llama-server`
(MIT) in its `llm/bin` folder; every `llm/models/*.gguf` file appears in
설정 → AI 설정 as "내 PC · <file name>" (model name `local:<stem>`).

- The app starts `llama-server` on demand: bound to 127.0.0.1 on a free port,
  a random API key per start (other programs and web pages cannot use it),
  no web UI, one slot, 32k context, stopped after 10 minutes unused and when
  the app exits. Requests bypass any system proxy.
- Reports: `generate_json` sends the same prompt with the report schema as a
  `response_format` JSON schema (llama.cpp turns it into a grammar).
  Thinking mode is off (`enable_thinking: false`): it costs minutes on a CPU
  and the app verifies the answer anyway.
- Questions: no MCP; the app runs the tool loop itself (`run_tools`) with the
  same read-only `HistoryTools`, so reference checks work exactly as before.
- Runtime: llama.cpp's DLLs need the Microsoft C++ runtime, which a bare Windows
  lacks; the zip carries it in `llm/bin` (Windows loads DLLs from the program's
  folder first), so nothing has to be installed.
- Devices: llama.cpp's Vulkan build uses NVIDIA, AMD or Intel graphics when
  the driver is there, else the CPU. `WORKHISTORY_LLM_DEVICE` (e.g. `none`,
  `Vulkan0`) pins a device for measurements.
- Models (2026-10-23): Qwen3.5-4B and Qwen3.5-9B, Q4_K_M GGUF from
  `unsloth/Qwen3.5-*-GGUF`, Apache-2.0, SHA-256 checked against Hugging Face.
  Model files are not in git (`llm/` is ignored); they are handed out next to
  the app zip, or built in with `tools/build_portable.py --with-models`.
- Closed networks: `--offline` leaves the `mcp` package (and the ~30 libraries it
  pulls in) out of the zip; without it the app lists no Claude Code models
  (`ai_claude.bundled()`), so only local models remain;
  the settings screen then shows no Claude Code status, list entry or explanation either
  (`claude_bundled` in `/api/ai/settings`).

Speed per report, measured 2026-10-23 on a 12th-gen Core i5 laptop (32 GB,
entry-level 4 GB NVIDIA GPU + Intel integrated graphics), `evals/report_eval.py`:

| Model · device | Weekly report | Daily report |
|---|---|---|
| 9B · auto (GPUs) | 161–221 s | 65–101 s |
| 9B · CPU only | 385–583 s | 122–129 s |
| 4B · auto (GPUs) | 151–171 s | 41–60 s |
| 4B · Intel iGPU only | 251–315 s | 85–97 s |
| 4B · CPU only | 277–338 s | 80–89 s |

- No run dropped a reference or left out a project (project-based prompt, before 2026-11-05).
- 9B is the recommended model: it passed every case on the GPU run. 4B is
  faster but more often calls unfinished work "완료" (the eval counts that as a
  failure; finished or not is the user's call, below).
- A PC without an NVIDIA or AMD card runs at about CPU speed; the report is
  generated in the background while the user keeps working.

- The client is `ai_openai.OpenAICompatModel` (llama-server speaks the OpenAI chat
  API): reports try a JSON schema, then JSON mode, then plain text with the schema
  in the prompt; `<think>` output is removed; questions run the app's own tool loop.
- Every `llm/models/*.gguf` is listed automatically, and the largest one is the
  suggested default.

## Provider: Claude Code (development only, not in the released zip)

Kept for development: it is the reference the local models are measured against
(`evals/report_eval.py`). It needs the `claude` extra (`mcp`) and a Claude Code
installed and signed in on the PC; the closed-network zip has neither.

`workhistory/ai_claude.py` runs the Claude Code installed and signed in on
the PC headless (`claude -p --output-format json`); usage counts against the
user's Claude account. AI is **off** until the user picks a model in
설정 → AI 설정 (`claude-code`, `claude-code:sonnet|opus|haiku`); that
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

Measured 2026-10-21 (Claude Code 2.1.293, claude-code:sonnet,
`--runs 2`): **8/8 passed** (all required facts present, no over-claims, no
other days' work in daily reports), 0 dropped references, 0 flags, 0 restored
projects; median 7.4 s, max 11.3 s per report.

### Questions with Claude Code (internal MCP)

With a Claude Code model, "AI에게 묻기" lets Claude Code run the tool loop:
the app starts `workhistory/mcp_server.py` for that one question (`--trace
<file>`), Claude Code (headless, only these tools allowed) calls the same
read-only tools as the in-app agent (`ai_agent.HistoryTools`), and the app
checks the answer's citations against the trace. The server answers only such
app runs, and only while the AI model is a Claude Code model; it is not
registered in Claude Code and other programs get a refusal. (Until 2026-10-27
a switch in the app also registered it with the user's own Claude Code; that
connection was removed.)

## Replacing the model

The AI code is written against `ai_client.Model`, not against Claude Code:
`generate_json(system, prompt, schema)` for reports, and either
`run_mcp(...)` (the model's own agent uses the app's MCP server) or
`run_tools(system, prompt, tools, execute)` (the app runs the tool loop
in-process and executes `HistoryTools` itself) for questions. Reference
checks, redaction, consent and logging stay in the app, so a new provider is
one class plus a factory entry (`ai_local.LocalModel` is the second one).

## Data protection

- Nothing is sent until the user turns AI on (see above); the demo
  database (synthetic data) is always allowed.
- Only work data is sent: recorded file changes (C: files, line counts and
  a bounded excerpt of changed lines; snapshots of one folder less than 30
  minutes apart go as one change, from the first old version to the last new
  one), and work records (W, `work.py`). A day: each work folder's time that
  day, or the day's 웹 / 소통 / 기타 time, with spans of the day and the window
  titles that took the most time. A week (2026-11-11): one record per work
  folder (and 웹 / 소통 / 기타) over the whole week, with its minutes per day
  ("월 11/2 244분, 화 11/3 207분 …") and the window titles of the week, so a
  folder worked on every day is sent once. The demo week went from 12,960 to
  8,616 characters, and the fictional busy week now fits without trimming. Projects, next actions and notes are not sent to the report
  (2026-11-05: the app has no screens for them any more). Browser addresses
  are not recorded at all.
  "업무 외" time is sent as a total only.
- Every free-text field passes `redact.py` (keys, tokens, passwords, resident
  registration and card numbers) before it is sent.
- The payload has a size budget (`ai_context.PAYLOAD_CHARS`, about 18k tokens,
  leaving room in the 32k context for the rules and the answer). It is sent as
  JSON with one record per line, each file as one short string
  ("src/Auth.java (수정 +12 -3)"). When a period is too big, `FIT_LEVELS` are
  tried in order: fewer windows per record, wider merging of a folder's
  changes (up to one per folder per day), fewer files listed per change, and
  only then leaving out the shortest records.
- A weekly report whose records do not fit as they are is not trimmed
  (2026-11-11, `ai_service._plan`): each day with records is drafted on its own
  (`SYSTEM`, the day's records, W numbers continuing from the day before so
  none is shared), and the daily drafts (groups, lines and their references)
  are merged into the week by one more request (`ai_report.MERGE_SYSTEM`). The
  merged draft is checked against all the days' references like any draft;
  the report shows a note that it was made this way (and what was cut if a
  single day was still too big). A week that fits stays one request. Cost: one
  request per day plus one; with a local model several minutes.
- Diff excerpts use what room is left, at most 600 lines per request, 50 per
  change, 25 per file, 200 characters per line, shared fairly: each change gets
  an equal share of what is left, so early changes cannot use up the room.
- The exact payload can be previewed in the UI before sending.
- Logged locally per call: kind, model, token counts, latency, success —
  never prompt or response text.

## Honesty rules (enforced in code, not only in prompts)

- Every reference the model returns is checked against the records that were
  actually sent; unknown references are dropped.
- A bullet without a valid reference is shown as "근거 없음".
- Issue and MR numbers are not checked in code (2026-11-11: the groups follow
  the work folders, the user reads the draft, and the check also caught words
  such as UTF-8). The prompt still asks for numbers that appear in the cited
  records, and the eval treats a wrong number as a failure.
- Finished or not is the user's call (2026-11-11): file changes and window
  titles show work, not that it is done. The prompt asks for what was done
  without "완료·해결", and every 실적 / 오늘 한 일 line of the draft ends in
  "(진행 중)". On the saved report the user clicks it to switch to "(완료)"
  (and back), or edits the text. There is no word check in code any more.
- Clicking a "파일 변경" (C) citation opens that change's diffs directly.
- 차주 계획 / 다음 할 일 is not in the records, so the model does not write it:
  each group gets an empty section (`- `) for the user.
- Missing work: a work folder whose records (W) and file changes (C) in the
  period are not cited anywhere in the draft is added as "<folder> (작업 폴더)"
  with its time and "이 작업 폴더의 기록이 AI 초안에 빠졌습니다".
- AI text is always labelled as an AI draft and must be saved by the user to
  become a report.

## Quality process

- Unit tests with a fake `claude` CLI and fake models cover the command line,
  error mapping, the MCP trace check, validation and guards.
- A small eval set on curated demo data (report checks) is run against a live model before changes are
  accepted: `evals/report_eval.py --model local:<model>` for the local models
  (the default when one is installed), `--model claude-code:sonnet` for the
  Claude Code reference.
