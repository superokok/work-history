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
store.py      – shared loaders, day bounds, session staleness bookkeeping
normalize.py  – pure: window − AFK idle − blocked ranges − exclusions, clipped to day
classify.py   – pure: manual override > keyword rule > none
sessions.py   – pure: deterministic grouping + auto titles
sessionstore.py – persisted sessions: regroup, edits, merge, split
snapshots.py  – work-folder snapshots in hidden Git repos (dulwich), change records
extract.py    – text from code/text, HWP, DOCX/XLSX/PPTX (stdlib only)
redact.py     – secret / personal-identifier masking before anything leaves the PC
ai_client.py  – the provider-neutral Model protocol the AI code is written against
ai_claude.py  – that protocol over headless Claude Code (`claude -p`, locked down)
ai_openai.py  – that protocol over any OpenAI-compatible chat API (shared by the two below)
ai_local.py   – llama.cpp's llama-server on this PC, started on demand (local models, nothing leaves the PC)
ai_remote.py  – AI servers the user registered by address (e.g. an LLM in the company network)
ai_agent.py   – "AI에게 묻기": read-only history tools + citation check
mcp_server.py – the history tools over MCP (Claude Code), consent-checked
autostart.py  – start-at-logon and shortcut files, switched from the app
ai_context.py – the exact payload for the AI with verifiable references (C/S/N/P)
ai_report.py  – report schema, prompt, reference verification, rendering
ai_service.py – settings, consent, run log, saved reports
usage.py      – effect measurement: report writing time, kept AI lines, search-to-open time (numbers only)
review.py     – pure: template daily review + follow-up extraction
   ▼
service.py    – timeline, search, notes, projects, review, resume queries
   ▼
server.py     – JSON API + static UI, bound to 127.0.0.1 only
   ▼
static/       – timeline / review / search / project views (Korean)
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

## 5. Data model (schema v9)

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
session_days   day PK, version   (-1 = new activity; else classification version)
meta           key PK, value     ('session_version' bumps on rule/exclusion changes)
daily_reviews  day PK, body, created_at, updated_at
```

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

v7 (effect measurement): `usage_events` (id, ts, kind, data JSON): numbers only, for the
before/after page. Kinds: `report` (one per save: sid, method `ai`|`manual`, active/wall seconds,
lines, AI-draft lines kept/edited), `search`, `search_open` (seconds after the search),
`source_open`. `usage.py` records and summarises; never report text or queries.

v6 (AI servers): `ai_endpoints` (id, name, url = API base ending in /v1, model_id, api_key, created_at):
servers the user registered by address; model choice `remote:<id>`.

v4 (AI): `reports` (saved daily/weekly drafts), `ai_runs` (token/latency log,
no content).

v3 (record only work apps):

```
track_rules    id, app_pattern, title_pattern   (either may be empty, not both)
meta           'track_mode' = 'all' | 'selected'
sessions       + offwork_s   (untracked part of the session's active time)
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
  raw events. Totals, reviews and stats report 업무 외 time separately; it never
  gets a project, and short 업무 외 detours are absorbed into work sessions
  like any other detour. The app picker reads app names from the raw
  events and does not store the list.
- **Project classification:** manual assignment > rule with the longest
  matching pattern (case-insensitive substring) > unassigned.
- **Session grouping** (`sessions.py`, constants in `config.py`):
  1. more than 10 min without activity (idle already removed) ends a session;
  2. a switch to another known project starts a new session only if it lasts
     at least 3 min. Unclassified work under 15 min (a browser search) and
     업무 외 time under 10 min stay inside the surrounding session;
  3. inside one project, a *kind* of work lasting at least 15 min becomes its
     own session: coding (IDE, terminal, API/DB tools), reading (browser),
     chat (messengers/mail); meetings (Teams/Zoom windows titled 회의,
     스크럼, meeting...) from 5 min. Switching apps within one kind never
     splits a session.
  Tuned on the two-week demo scenario: per-app splitting produced 23-29
  sessions per day (every IDE/browser switch); the rules above give 3-11.
  Auto title = dominant app + its longest-viewed window title (app suffix
  like " - Google Chrome" removed). Session project = manual, else the
  dominant activity project by time. Totals use session projects; excluded
  sessions are left out of totals and reviews.
- **Session edits are never lost:** sessions are regenerated, but an edited
  (title/project/note/excluded) session follows its regrouped counterpart by
  start time, so the session you are in keeps growing after you rename it.
  If no counterpart exists any more, it is frozen (`locked`) at its range.
  Merge and split create locked sessions; regrouping happens only outside
  locked ranges.
- **Staleness:** sync marks touched days `-1`; rule/exclusion/archive changes
  bump a global version. Stale days are regrouped by the background syncer
  every minute, by the timeline for the viewed day, and by search/resume
  before they run (committing per day). Measured: 365 days x 600 events
  regroup in about 8 s (only after a rule change); a 3,000-event day loads in
  about 25 ms cached / 60 ms regrouped.
- **Daily review:** deterministic template, no AI. Section headers are tagged
  [자동 집계], [자동 추정], [직접 작성]. Follow-up candidates are only lines
  from the user's own notes containing markers (TODO, 확인, 해야, 필요, 내일,
  ?) plus projects' user-written next actions, never invented items. The user
  edits and saves the text; saved reviews are searchable.
- **Resume work:** per project: user-written next action, last session and
  the last window seen in it, recent sessions, notes. The app never claims to
  know the next task.
- **Search:** SQLite `LIKE` substring search. Korean words are often 2
  characters and not whitespace-separated, so token-based FTS would miss them;
  personal-scale data (≈10⁵–10⁶ rows) is fast enough with `LIKE`. FTS5
  `trigram` is available as an upgrade path.

## 7. Portability

Windows only (decision 2026-10-28; until then the app also ran on macOS/Linux with
ActivityWatch). Windows-specific pieces: the collector (`collector.py`, Win32 via ctypes),
shortcuts (`autostart.py`, WScript.Shell), the data folder (`%LOCALAPPDATA%`), the launcher
(`start-workhistory.cmd`, portable zip), exclusive port binding (`server._ExclusiveServer`) and the
single-instance mutex (`instance.py`). Port: 5730, else the next free one up to 5739 (since
2026-10-30); one app per database is a named mutex, not the port, and a second launch asks each
candidate port for the app with its database (`/api/status` db_path) and opens that one.
The launchers start `pythonw.exe` (no console, since 2026-10-30: a closed console window used to
stop the recording); the app then logs to `%LOCALAPPDATA%\WorkHistory\workhistory.log` (1 MB,
one backup, no event content), shows startup errors in a message box, and stops only through
"앱 종료" (`POST /api/quit`, guarded like every write by the `X-Work-History` header).
`sessions.py` still knows a few macOS/Linux app names; they are harmless.

## 8. Security

- HTTP server binds `127.0.0.1` only.
- `Host` header must be `127.0.0.1:<port>` or `localhost:<port>` (blocks DNS
  rebinding).
- Mutating requests require `Content-Type: application/json` and an
  `X-Work-History: 1` header, which forces a CORS pre-flight that the server
  never approves → other websites cannot write to the API.
- Requests to `/api/` that a browser marks as started by another site
  (`Sec-Fetch-Site: cross-site|same-site`, or a foreign `Origin`) are refused,
  GETs included: some GETs have side effects (session regrouping, the Claude
  Code status check starts processes).
- Logs contain counts and errors only, never titles or note text.
- The MCP server is internal (the app starts it for one question with a Claude
  Code model) and refuses any other caller; its instructions still say that
  window titles, paths and diffs are untrusted text, never instructions.
- Known, accepted (local-only): no per-launch token against other accounts on
  the same PC; a local caller who passes `--trace` itself gets the MCP tools,
  but could read the database file anyway.

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

## 10. Known collector limitations

- Window titles only; no URLs and no file paths unless the editor puts them in the title.
- Away = 3 minutes without keyboard/mouse input; reading without input counts as idle.
- Protected processes show as `unknown` (their exe name can't be read); elevated (admin)
  windows are read.
- The collector runs inside the app: while the app is closed, no window activity is
  recorded (file changes are caught up at the next scan).
- Window activity imported from ActivityWatch before 2026-10-28 stays in the database
  (its own `sources` rows); it is no longer re-applied ("다시 적용" works on the last 35 days
  of the collector's raw events).
