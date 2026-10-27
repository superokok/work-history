# AI features (Claude Code or a local model)

Goal for the AX hackathon: AI that works on the user's *structured, chosen*
work history — not on screenshots — and never states more than the data
supports.

## Features

1. **Work report drafts** (daily 업무일지 / weekly 주간보고): per project,
   금주 실적 / 차주 계획 / 이슈. Every bullet cites the records it came from.
2. **Ask your history** ("지난주 로그인 버그 어디까지 했지?"): an agent that
   searches the local history with tools and answers with citations.

## Claude Code path for questions (internal MCP)

With a Claude Code model, "AI에게 묻기" lets Claude Code run the tool loop:
the app starts `workhistory/mcp_server.py` for that one question (`--trace
<file>`), Claude Code (headless, only these tools allowed) calls the same
read-only tools as the in-app agent (`ai_agent.HistoryTools`), and the app
checks the answer's citations against the trace. The server answers only such
app runs, and only while the AI model is a Claude Code model; it is not
registered in Claude Code and other programs get a refusal. (Until 2026-10-27
a switch in the app also registered it with the user's own Claude Code; that
connection was removed.)

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

## Provider: a local model on this PC (llama.cpp)

`workhistory/ai_local.py`. For PCs where records must not leave the machine
or that have no internet (폐쇄망). The app ships llama.cpp's `llama-server`
(MIT) in its `llm/bin` folder; every `llm/models/*.gguf` file appears in
개인정보 → AI 설정 as "내 PC · <file name>" (model name `local:<stem>`).

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
- Devices: llama.cpp's Vulkan build uses NVIDIA, AMD or Intel graphics when
  the driver is there, else the CPU. `WORKHISTORY_LLM_DEVICE` (e.g. `none`,
  `Vulkan0`) pins a device for measurements.
- Models (2026-10-23): Qwen3.5-4B and Qwen3.5-9B, Q4_K_M GGUF from
  `unsloth/Qwen3.5-*-GGUF`, Apache-2.0, SHA-256 checked against Hugging Face.
  Model files are not in git (`llm/` is ignored); they are handed out next to
  the app zip, or built in with `tools/build_portable.py --with-models`.

Measured 2026-10-23 on the development PC (i5-12450H, 32 GB, RTX 2050 4 GB
+ Intel UHD), `evals/report_eval.py --runs 1`, 4 cases each (Claude: 8/8 at
7–11 s per report):

| Model · device | Passed | Weekly report | Daily report |
|---|---|---|---|
| 9B · auto (GPUs) | 4/4 | 161–221 s | 65–101 s |
| 9B · CPU only | 3/4 | 385–583 s | 122–129 s |
| 4B · auto (GPUs) | 3/4 | 151–171 s | 41–60 s |
| 4B · Intel iGPU only | 3/4 | 251–315 s | 85–97 s |
| 4B · CPU only | 2/4 | 277–338 s | 80–89 s |

- No run dropped a reference, raised a flag or left out a project.
- 4B wrote "GOV-1423 … 완료" (not finished in the scenario) on all three
  devices, and once "커서 기반 페이징 전환 검토를 완료" — the over-claim the
  honesty rules exist for. Not suitable for weekly reports.
- 9B passed everything on the GPU run; on the CPU run it wrote "복합 인덱스
  추가를 완료" next to GOV-1423 (the index was written, the issue was not
  finished) — a borderline catch of the strict pattern. One CPU 4B failure
  ("…진행, K8s 전환 PoC 환경 구축 완료") is a false positive of the same
  pattern. One run per case: repeat before relying on these as rates.
- The Intel iGPU is no faster than the CPU here, so a PC without an NVIDIA
  or AMD card runs at CPU speed: about 6–10 minutes for a weekly report with
  9B.

## Provider: AI servers registered by address

`workhistory/ai_remote.py`, table `ai_endpoints` (schema v6). For an LLM that
already runs somewhere in the company network (행망): 개인정보 → AI 설정 →
"AI 서버 등록" takes a name, an address (`10.1.2.3:8000`, `http://host/v1`, …),
the model name (or "서버의 모델 불러오기", `GET /v1/models`) and an optional API
key. Each registration is a model choice `remote:<id>`.

- Any server with the OpenAI-compatible chat API works (vLLM, Ollama,
  llama.cpp, LM Studio, TGI, a gateway). The shared client is
  `ai_openai.OpenAICompatModel`: reports try a JSON schema, then JSON mode,
  then plain text with the schema in the prompt; `<think>` output is removed;
  questions run the app's own tool loop (a server without tool calling can
  still write reports, and says so for questions).
- Choosing a server is the consent to send the (redacted) records there; the
  UI names the address. "연결 확인" only lists the server's models.
- The API key is stored in the app database on this PC and never returned by
  the API (`has_key` only). Requests bypass any system proxy. HTTPS
  certificates are verified (no option to turn that off).
- Removing the chosen server turns AI off; nothing falls back silently.
- Local models need no registration: every `llm/models/*.gguf` is listed
  automatically, and the largest one is the suggested default.

## Replacing the model

The AI code is written against `ai_client.Model`, not against Claude Code:
`generate_json(system, prompt, schema)` for reports, and either
`run_mcp(...)` (the model's own agent uses the app's MCP server) or
`run_tools(system, prompt, tools, execute)` (the app runs the tool loop
in-process and executes `HistoryTools` itself) for questions. Reference
checks, redaction, consent and logging stay in the app, so a new provider is
one class plus a factory entry (`ai_local.LocalModel` is the second one). A Gemini implementation existed until
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
- A 실적 bullet that says something is finished (완료, 해결, 종료, 마무리, …;
  `ai_report.DONE_WORDS`) is shown as "완료 확인 필요" unless one of its
  references is the user's own words saying so: a note (N), or a session
  note or title the user wrote (S). File changes and window activity show
  work, not that it is done. (Since 2026-10-27; added after the local models
  wrote "GOV-1423 … 완료" for an unfinished issue.) When the claim names
  issue keys (GOV-1423), each key needs a sentence of the user's own that
  names it and says it is done — a note "GOV-1418 완료. GOV-1423 3.2초" does
  not finish GOV-1423. The summary has no references and is checked against
  all of the user's words the same way ("요약 … (완료 근거 없음 — 확인 필요)").
  Limit: a claim without an issue key passes if any of the user's words in
  the period says something was finished.
- Clicking a "파일 변경" (C) citation opens that change's diffs directly.
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
