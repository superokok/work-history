# 작업 기록 (Personal Work History)

Local-first work history for developers on Windows. It reads app/window/idle
activity from [ActivityWatch](https://activitywatch.net) and turns it into a
searchable, editable timeline with projects and notes. Nothing leaves your
computer.

Docs: [PRODUCT](docs/PRODUCT.md) · [ARCHITECTURE](docs/ARCHITECTURE.md) ·
[PRIVACY](docs/PRIVACY.md) · [IMPLEMENTATION_PLAN](docs/IMPLEMENTATION_PLAN.md)

## Requirements

- Windows 10/11, macOS or Linux (verified on Windows 11 and in a Linux
  container; the demo targets Windows)
- Python 3.11+ with a project virtual environment:

  ```bash
  uv venv .venv --python 3.12
  uv pip install --python .venv/Scripts/python.exe -e .[claude]   # Windows
  uv pip install --python .venv/bin/python -e .[claude]           # macOS / Linux
  ```

  Runtime packages (all permit commercial use): `dulwich` (folder snapshots,
  Apache-2.0) and, for AI and Claude Code access, `mcp` (MIT).
- Optional, for AI: Claude Code installed and signed in
  (https://docs.claude.com/en/docs/claude-code). No API key is needed.
- Optional: ActivityWatch for window activity
  (https://github.com/ActivityWatch/activitywatch/releases). Folder snapshots,
  AI reports and the rest of the app work without it.

## Run

| OS | Start |
|---|---|
| Windows | double-click `start-workhistory.cmd` (or a desktop shortcut to it) |
| macOS / Linux | `sh start-workhistory.sh` |

The launcher starts ActivityWatch if it is installed and not running, then
the app, and opens `http://127.0.0.1:5730/`. Launching again while the app
runs just reopens the browser. Equivalent manual command:
`python -m workhistory` from the venv.

| Option | Meaning |
|---|---|
| `--port N` | HTTP port (default 5730, always bound to 127.0.0.1) |
| `--db PATH` | Database file (default: per-user data folder, below) |
| `--aw-url URL` | ActivityWatch address (default `http://127.0.0.1:5600`) |
| `--backfill-days N` | Days of ActivityWatch history imported on the first sync (default 30) |
| `--demo` | Separate `demo.db` with a fictional two-week developer scenario (clearly labeled); no syncing |
| `--window` | Native window if `pywebview` is installed; otherwise the browser. Not yet tested. |
| `--no-browser` | Don't open a browser |
| `--verbose` | Debug logging |

Data folder: Windows `%LOCALAPPDATA%\WorkHistory`, macOS
`~/Library/Application Support/WorkHistory`, Linux
`$XDG_DATA_HOME/WorkHistory` (default `~/.local/share/WorkHistory`).

Autostart (optional): Windows: a shortcut to ActivityWatch in
`shell:startup`; macOS: ActivityWatch → Login Items; Linux: your desktop's
autostart settings.

## Using it

- **타임라인**: the day as **work sessions** inferred from window activity.
  Idle (AFK) and lock-screen time are excluded. Per session you can rename it
  (leave blank to return to the auto title), set its project, add a note,
  exclude it from totals, merge it with the next session, or open its window
  rows and split at any row. A renamed or annotated session keeps growing
  while you keep working. Day notes go on the right.
- **리뷰**: an editable daily review draft generated from sessions and notes
  (no AI). Sections are labelled [자동 집계] / [자동 추정] / [직접 작성]. Edit
  it, then save; saved reviews are searchable.
- **이어서 하기**: projects ordered by most recent work. For each: your next
  action, the last session and the last window seen in it, recent sessions,
  and previous notes.
- **검색**: substring search over sessions, window titles, app names, notes,
  reviews and projects, with date and project filters.
- **프로젝트**: **작업 폴더** (results recording: register source/document
  folders; every 5 minutes changed files are snapshotted into a hidden local
  Git repository, never touching the folder or its own .git; secrets and build
  outputs are skipped), plus projects and keyword rules (app name or window
  title contains a keyword, case-insensitive; the longest keyword wins).
  File changes appear in the timeline (click a file to see its diff), in
  search, and as evidence in AI reports.
- **개인정보**: what to record (all apps, or **only work apps**: everything
  else is kept as anonymous "업무 외" time with no app name or title; pick apps
  from the last 7 days and re-apply rules to the last 30 days), exclusion
  list, pause/resume, and deletion by date range or all.

Session grouping rules and their constants are described in
[ARCHITECTURE §6](docs/ARCHITECTURE.md).

## AI setup (optional)

Reports (보고서 tab) and "AI에게 묻기" use the Claude Code installed and
signed in on this PC. In **개인정보 → AI 설정** pick a model (Sonnet is
suggested) and confirm; until then AI is off and no real work data is sent
(the demo database always works). The app runs Claude Code headless with
every tool switched off except its own read-only history tools; usage counts
against your Claude account. **연결 확인** checks install and login without
sending work data, and "보낼 내용 보기" shows exactly what a report sends.
Details: [docs/AI.md](docs/AI.md).

## Claude Code (MCP)

Developers can ask about their own history from inside Claude Code
("지난주 GOV-1418 어디까지 했지?"). The app ships a read-only MCP server
(`workhistory.mcp_server`, MCP Python SDK, MIT) with the tools
`search_history`, `get_day`, `list_projects`, `get_project`, `get_change`
and `get_report_data`.

1. Install the extra: `uv pip install --python <venv python> -e .[claude]`
   (Claude Code itself must be installed and signed in).
2. In the app: **개인정보 → Claude Code 연결**, turn on
   "Claude Code에서 내 업무 기록 사용". The app registers its server with Claude
   Code for you (`claude mcp add --scope user workhistory ...`, through Claude
   Code's own CLI) and shows the state. Turning it off removes the
   registration again. No terminal step is needed.

The app only ever adds or removes its own name (`workhistory`, or
`workhistory-demo` with `--demo`) and refuses to touch a server of that name
that points elsewhere. A manual command is still shown under "수동 설정"
for unusual setups. Results reach the model provider of your Claude account;
secrets are masked and recording rules apply.

Verified end to end with Claude Code 2.1.268 in headless mode on the demo
data: it searched the history with the tools over 9 turns and answered with
correct references.

## Real-use validation

See [docs/VALIDATION.md](docs/VALIDATION.md) for the one-week checklist.
Correction rates per day (renamed / re-assigned / merged-split / excluded
sessions, share of time classified):

```bash
python -m workhistory.stats --days 7
```

## Tests

```bash
.venv/Scripts/python.exe -m unittest discover -s tests -t .   # Windows
.venv/bin/python -m unittest discover -s tests -t .           # macOS / Linux
```

The tests use temporary databases and a fake ActivityWatch server that mimics
the real API's range-overlap, trimming and heartbeat behaviour.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "ActivityWatch 연결 안 됨" | Start `aw-qt.exe` and check that `http://127.0.0.1:5600` opens. Stored history stays visible meanwhile. |
| "창 기록 없음" / "유휴 감지 없음" in the status chip | The `aw-watcher-window` / `aw-watcher-afk` watcher isn't running. Check the ActivityWatch tray menu → Modules. |
| Yellow warning that idle data is missing | That day has no AFK data, so all window time counts as active and may be overstated. |
| "포트 5730를 다른 프로그램이 쓰고 있습니다" | Another program holds the port (a running copy of this app is detected and simply reopened). Use `--port`. |
| Windows from admin (elevated) apps show as `unknown` | ActivityWatch's watcher can't read elevated windows unless AW itself runs elevated. |
| Data from before you installed this app is missing | Only `--backfill-days` (default 30) are imported on the first sync. To import more, start with a fresh `--db` and a larger value. |

## Limitations

See [ARCHITECTURE §9](docs/ARCHITECTURE.md) and [PRIVACY](docs/PRIVACY.md).
Most importantly: pause, exclusions and deletion apply to this app's database
only. ActivityWatch keeps its own copy until you remove it there.
