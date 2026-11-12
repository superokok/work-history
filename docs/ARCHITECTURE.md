# Architecture

## 1. Environment findings (2026-10-01)

- Windows 11 Pro. Available: Python 3.12.5 (SQLite 3.45.3, FTS5 + trigram),
  uv 0.9, Node 22.14, Java 17, git. **No Rust** (Tauri would need extra setup).
- Window activity first came from ActivityWatch (portable v0.13.2, REST API). Since
  2026-10-28 the app records it itself (§4) and ActivityWatch is no longer used.
- Project directory was empty; no existing stack to follow.

## 2. Stack decision

| Option | Pros | Cons |
|---|---|---|
| **Python stdlib backend + plain HTML/JS UI** (chosen) | Zero required runtime deps (`http.server`, `sqlite3`, `urllib`); trivial to test; no build step | Not a "native" window by default |
| Electron + Node | Native-feeling window out of the box | ~100 MB+ runtime, `better-sqlite3` native rebuilds, packaging toolchain |
| Tauri | Small binary | Requires Rust toolchain (not installed) |

**Chosen:** Python 3.12, standard library only, serving a vanilla HTML/JS UI on
`127.0.0.1`. The UI opens in the default browser; if the optional `pywebview`
package (BSD-3-Clause, uses Edge WebView2) is installed, it opens in its own
desktop window instead. No frontend build step.

## 3. Pipeline

```
collector.py  – in-app thread, Win32 via ctypes → collector_events (raw, 35 days)
   ▼
sync.py       – incremental pull, dedupe/upsert, privacy filtering at ingest
   ▼
SQLite (db.py, versioned migrations via PRAGMA user_version)
   ▼
store.py      – shared loaders, day bounds
normalize.py  – pure: window − AFK idle − blocked ranges − exclusions, clipped to day
classify.py   – pure: manual override > keyword rule > none
apps.py       – pure: app names, labels, window title cleanup
work.py       – work records: each window put with a work folder only where it shows one,
                else 웹 / 소통 / 기타; time per (day, work folder) with spans of the day
snapshots.py  – work-folder snapshots in hidden Git repos (dulwich), change records
extract.py    – text from code/text, HWP, DOCX/XLSX/PPTX (stdlib only)
redact.py     – secret / personal-identifier masking before anything leaves the PC
ai_client.py  – the provider-neutral Model protocol the AI code is written against
ai_claude.py  – that protocol over headless Claude Code (`claude -p`, locked down); development/comparison only, not in the released zip
ai_openai.py  – that protocol over the OpenAI-compatible chat API that llama-server speaks
ai_local.py   – llama.cpp's llama-server on this PC, started on demand (local models, nothing leaves the PC)
ai_agent.py   – "AI에게 묻기": read-only history tools + citation check
mcp_server.py – the history tools over MCP (Claude Code, development only), consent-checked
autostart.py  – start at logon (Startup shortcut), switched from the app
ai_context.py – the exact payload for the AI with verifiable references (W/C), within a size budget; too much for one request is planned as several (work folders packed together)
ai_report.py  – report schema, prompt, reference checks, rendering
ai_service.py – settings, consent, run log, saved reports; a report in several requests (progress, stop), joined by code, summary last
   ▼
service.py    – timeline, search, notes, projects
dashboard.py  – day/week dashboard: time by kind of activity, the day strip, time per work
                folder (work.py), file changes per work folder; the day before / last week to compare
categories.py – kind of activity from the app name (rules, no AI)
   ▼
server.py     – JSON API + static UI, bound to 127.0.0.1 only
   ▼
static/       – dashboard / timeline / report / search / settings views (Korean)
```

Collection, processing (pure functions), persistence and UI are separate
modules, so the collector can be replaced by writing another module that
produces the same `window_events` / `afk_events` rows.

## 4. Window activity collector

`collector.py` runs on a thread inside the app (decision 2026-10-28: one install, no
ActivityWatch, nothing GPL in the bundle). Every 2 s it reads, with standard-library
`ctypes` only:

- foreground window: `GetForegroundWindow` → `GetWindowTextW` (title) and
  `GetWindowThreadProcessId` → `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` →
  `QueryFullProcessImageNameW` (app = exe file name, e.g. `Code.exe`, the same format
  ActivityWatch used, so tracking rules made before keep matching). Limited rights also
  read most elevated (admin) windows.
- no browser address: from v13 to 2026-11-10 the address bar was read through UI
  Automation, which makes Chromium turn on its accessibility tree (a cost inside the
  browser). Nothing used it but a copy button in the timeline, so it was removed and the
  stored addresses deleted (v14); the window title is enough.
- idle: `GetLastInputInfo` vs `GetTickCount` (32-bit wrap handled). Away after 3 min without
  input (`config.AFK_AFTER_S`); the away period starts at the last input.

The same window (or status) read again within 30 s extends the open event; a longer gap
(sleep, app closed) starts a new one. Open events are written every 15 s and right before a
sync. Rows go to `collector_events`, kept 35 days (`config.RAW_RETENTION_S`), which lets
"다시 적용" and the app picker work on untracked apps too.

Nothing is recorded while paused or for windows matching an exclusion; adding an exclusion
purges matching raw rows, and "delete data" deletes raw rows too.

`sync.py` imports raw rows every 60 s into `window_events` / `afk_events` (one `sources` row
per kind, `bucket_id` `workhistory-window` / `workhistory-afk`, `bucket_created` `builtin`):

- Dedupe key = `(source, raw id)` (column `aw_id`, named from the ActivityWatch days).
- Upsert keeps `min(start)` and `max(end)`, so the growing open event is extended and
  re-syncing is idempotent.
- Per-source cursor; each sync re-reads events overlapping `cursor − 10 min` .. now, so a
  long event that started before the cursor is still extended.
- Privacy rules apply here (pause/deleted ranges, exclusions, "업무 앱만 기록").

## 5. Data model (schema v14)

```
sources        id, bucket_id, bucket_created, kind('window'|'afk'), hostname,
               cursor_ts, last_sync_at                 UNIQUE(bucket_id, bucket_created)
window_events  id, source_id, aw_id, start_ts, end_ts, app, title,
               project_id NULL (manual override)       UNIQUE(source_id, aw_id)
afk_events     id, source_id, aw_id, start_ts, end_ts, status
projects       id, name UNIQUE, color, next_action, archived, created_at
project_rules  id, project_id, field('app'|'title'), pattern, created_at
notes          id, day 'YYYY-MM-DD', project_id NULL, body, created_at, updated_at
exclusions     id, field('app'|'title'), pattern, created_at
blocked_ranges id, kind('pause'|'deleted'), start_ts, end_ts NULL
```

v2 (Phase 3):

```
sessions       id, day, start_ts, end_ts, active_s, title, title_edited,
               project_id, project_manual, note, excluded, locked, timestamps
session_days   day PK, version
meta           key PK, value
```

`sessions` and `session_days` are no longer written or read (2026-11-10: work records,
computed from the activity on demand, replaced sessions). Rows earlier versions left stay in
the database and go with 데이터 삭제.

v5 (results, see RESULTS_TRACKING.md):

```
watched_folders id, path UNIQUE, label, project_id, enabled, last_scan_at, last_error
file_state      (folder_id, path) PK, size, mtime_ns, blob_id     -- scan cache
changes         id, folder_id, commit_id, ts, baseline, files_*/lines_* totals
change_files    id, change_id, path, kind('A'|'M'|'D'), is_text, lines_added, lines_removed,
                old_blob, new_blob
```

v8 (projects for parts of a folder, see RESULTS_TRACKING.md): `folder_areas`
(folder_id, subpath, project_id NULL = "no project", created_at) holds what the user confirmed;
`changes.project_id` / `change_files.project_id` are derived from it and the folder's project
(`snapshots.attribute_folder`, re-run when either changes). Queries use these, not the folder's project.

v9 (collector, §4): `collector_events` (id, kind 'window'|'afk', start_ts, end_ts,
app, title, status), the raw events `sync.py` imports from on Windows; pruned after 35 days.

v7 added a `usage_events` counter table that v10 drops again.

v6 added an `ai_endpoints` table (AI server registrations) that v11 drops again.

v2 added a `daily_reviews` table (the template daily review tab) that v12 drops again:
the AI 업무일지 covers it.

v13: `collector_events.path` and `window_events.path` (browser address). v14: both columns
dropped, the recorded addresses with them (see §4).

v4 (AI): `reports` (saved daily/weekly drafts), `ai_runs` (token/latency log,
no content).

v3 (record only work apps):

```
track_rules    id, app_pattern, title_pattern   (either may be empty, not both)
meta           'track_mode' = 'all' | 'selected'
sessions       + offwork_s   (no longer used)
```

Timestamps are stored as UTC epoch seconds (REAL). Day boundaries use the OS
local timezone.

## 6. Key rules

- **Idle:** activity time = window time − AFK `afk` periods (where the AFK
  watcher reported both states, `not-afk` wins). Time with no AFK data counts
  as active, so a crashed AFK watcher never hides real work; if a day has no
  AFK data at all the timeline shows a warning. `LockApp.exe` (Windows lock
  screen) is always idle. Overlapping window events are never double-counted.
- **Pause / deleted ranges:** events that *start* inside a blocked range are
  dropped at import; blocked time is also subtracted at query time, which
  handles events that began before a pause and were extended by heartbeats.
- **What to record:** in `selected` mode, a window is imported with app and
  title only if it matches a track rule (case-insensitive substrings: app
  contains X and, if given, title contains Y). Other windows are stored with
  empty app and title (`OFFWORK_APP`), i.e. anonymous "업무 외" time. The
  lock screen is kept as-is because it is idle time. The upsert replaces
  app/title on re-import, so resetting the window cursors
  (`reset_window_cursors`) re-applies the current rules from the collector's
  raw events. Totals and stats report 업무 외 time separately; it never
  gets a project or a work record. The app picker reads app names from the raw
  events and does not store the list.
- **Project classification:** manual assignment > rule with the longest
  matching pattern (case-insensitive substring) > unassigned.
- **Work records** (`work.py`, 2026-11-10; they replace work sessions): time is
  not cut into sessions. A window goes with a work folder only when its path is
  under that folder, otherwise by the kind of app:
  1. its path is the work-folder file its title names (`locate.file_paths`, the
     same path the timeline shows when the window is clicked: IDE and editor
     titles name the open file), or a work folder's path written in the title
     itself (terminals: `PS C:\app\project-a>`);
  2. with nested work folders the deepest one wins;
  3. names alone never count (a folder called "api" in some title, a relative
     path such as "./chart" whose base is unknown);
  4. anything else: browsers → 웹, messengers/mail/meetings → 소통, the rest →
     기타 (`categories.py`). Browsers and messengers are never put with a folder.
  Demo scenario, last week: gov24-minwon-api 12시간 33분, poc-k8s 1시간 19분,
  웹 6시간 20분, 소통 4시간 28분, 기타 11시간 50분 (its terminals show commands,
  not paths).
  Nothing is guessed from what came before or after: a window between two
  stretches of project-a is not project-a unless it shows it. A record is one
  (day, work folder) or (day, 웹/소통/기타): seconds added up per window however
  often the user switched back and forth, spans of the day (a gap over
  `WORK_SPAN_GAP_S` = 10 min starts a new span; display only) and window titles
  by time. "업무 외" is a total only. Records are computed from the activity when
  asked (dashboard, AI report, AI에게 묻기); nothing is stored, so there is
  nothing to regroup or keep in step with edits.
- **Timeline:** one view, per app (`service.app_summary`): the day's activities
  added up per app (an app used again later joins its earlier time), most
  recently used app first, window titles by total time; clicking a window shows
  its file path (a work-folder file named in the title) to copy. The timeline shows no project totals,
  project pickers or notes (the API still returns them).
- **Next action:** each project may keep a user-written "다음 할 일" (API only
  since 2026-11-05). AI reports suggest 차주 계획 / 다음 할 일 only from the period's
  records, each citing one, marked "(제안)" until the user confirms it (2026-11-12).
- **Tabs:** 대시보드 (2026-11-10), 타임라인, 보고서, 검색, 설정. The template daily review
  and the 이어서 하기 tab were removed (2026-11-05: the AI 업무일지 and "AI에게 묻기" cover
  them); work folders are in 설정.
- **Search:** SQLite `LIKE` substring search. Korean words are often 2
  characters and not whitespace-separated, so token-based FTS would miss them;
  personal-scale data (≈10⁵–10⁶ rows) is fast enough with `LIKE`. FTS5
  `trigram` is available as an upgrade path.

## 7. Portability

Windows only (decision 2026-10-28; until then the app also ran on macOS/Linux with
ActivityWatch). Windows-specific pieces: the collector (`collector.py`, Win32 via ctypes),
the start-at-logon shortcut (`autostart.py`, WScript.Shell), the data folder (`%LOCALAPPDATA%`), the launcher
(`start-workhistory.cmd`, portable zip), exclusive port binding (`server._ExclusiveServer`) and the
single-instance mutex (`instance.py`). Port: 5730, else the next free one up to 5739 (since
2026-10-30); one app per database is a named mutex, not the port, and a second launch asks each
candidate port for the app with its database (`/api/status` db_path) and opens that one.
The launchers start `pythonw.exe` (no console, since 2026-10-30: a closed console window used to
stop the recording); the app then logs to `%LOCALAPPDATA%\WorkHistory\workhistory.log` (1 MB,
one backup, no event content), shows startup errors in a message box, and stops only through
"앱 종료" (`POST /api/quit`, guarded like every write by the `X-Work-History` header).
`apps.py` still knows a few macOS/Linux app names; they are harmless.

## 8. Security

- HTTP server binds `127.0.0.1` only.
- `Host` header must be `127.0.0.1:<port>` or `localhost:<port>` (blocks DNS
  rebinding).
- Mutating requests require `Content-Type: application/json` and an
  `X-Work-History: 1` header, which forces a CORS pre-flight that the server
  never approves → other websites cannot write to the API.
- Requests to `/api/` that a browser marks as started by another site
  (`Sec-Fetch-Site: cross-site|same-site`, or a foreign `Origin`) are refused,
  GETs included: some GETs have side effects (the Claude Code status check
  starts processes).
- Logs contain counts and errors only, never titles or note text.
- The MCP server is internal (the app starts it for one question with a Claude
  Code model) and refuses any other caller; its instructions still say that
  window titles, paths and diffs are untrusted text, never instructions.

## 9. Licensing

| Component | License | How we use it | Obligation |
|---|---|---|---|
| pywebview (optional) | BSD-3-Clause | Optional desktop window | Keep notice if distributed. |
| Python, SQLite | PSF / public domain | Runtime | Notice for Python if bundled. |
| dulwich | Apache-2.0 (dual GPL, used under Apache) | Folder snapshots | Notice. |
| llama.cpp / ggml, LLVM OpenMP | MIT / Apache-2.0 WITH LLVM-exception | Local AI (`llm/bin`) | License texts in `llm/`. |
| Qwen3.5 models | Apache-2.0 | Local AI (`llm/models`) | License text in `llm/`. |
| Microsoft Visual C++ runtime (`msvcp140`, `vcruntime140`, `vcruntime140_1`) | Visual Studio redistributable files | Needed by llama.cpp; shipped app-local in `llm/bin` so a bare Windows runs the local AI (2026-10-30) | Listed in THIRD_PARTY_NOTICES. |

ActivityWatch (MPL-2.0; its Windows tray bundle contains GPL PyQt6) is no longer used
(2026-10-28); no ActivityWatch code was ever copied.

## 10. Collector scope

- Window titles and app names only (no browser addresses since 2026-11-10). Editors and
  IDEs don't expose the open file, so its path is resolved at query time from the title's
  file name and the work folders' file list (`locate.file_paths`, unique matches only; the
  title's project name breaks ties). Explorer folders are not read.
- Away = 3 minutes without keyboard/mouse input; reading without input counts as idle.
- Protected processes show as `unknown` (their exe name can't be read); elevated (admin)
  windows are read.
- The collector runs inside the app: while the app is closed, no window activity is
  recorded (file changes are caught up at the next scan).
