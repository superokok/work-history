# Architecture

## 1. Environment findings (2026-10-01)

- Windows 11 Pro. Available: Python 3.12.5 (SQLite 3.45.3, FTS5 + trigram),
  uv 0.9, Node 22.14, Java 17, git. **No Rust** (Tauri would need extra setup).
- ActivityWatch was not installed. The official portable build
  `activitywatch-v0.13.2-windows-x86_64.zip` was extracted to
  `%LOCALAPPDATA%\ActivityWatch\activitywatch\` for development.
- Project directory was empty; no existing stack to follow.

## 2. Stack decision

| Option | Pros | Cons |
|---|---|---|
| **Python stdlib backend + plain HTML/JS UI** (chosen) | Zero required runtime deps (`http.server`, `sqlite3`, `urllib`); same language as ActivityWatch; trivial to test; no build step | Not a "native" window by default |
| Electron + Node | Native-feeling window out of the box | ~100 MB+ runtime, `better-sqlite3` native rebuilds, packaging toolchain |
| Tauri | Small binary | Requires Rust toolchain (not installed) |

**Chosen:** Python 3.12, standard library only, serving a vanilla HTML/JS UI on
`127.0.0.1`. The UI opens in the default browser; if the optional `pywebview`
package (BSD-3-Clause, uses Edge WebView2) is installed, it opens in its own
desktop window instead. No frontend build step.

## 3. Pipeline

```
ActivityWatch (separate process, 127.0.0.1:5600)
   │  REST: /api/0/info, /api/0/buckets/, /api/0/buckets/{id}/events
   ▼
awclient.py   – thin urllib client, typed errors when AW is unreachable
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
ai_agent.py   – "AI에게 묻기": read-only history tools + citation check
mcp_server.py – the history tools over MCP (Claude Code), consent-checked
claude_link.py – registers the MCP server with Claude Code from the app
autostart.py  – start-at-logon and shortcut files, switched from the app
ai_context.py – the exact payload for the AI with verifiable references (C/S/N/P)
ai_report.py  – report schema, prompt, reference verification, rendering
ai_service.py – settings, consent, run log, saved reports
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

## 4. ActivityWatch integration

Verified against upstream source (`aw-core` peewee storage and
`aw-server-rust` datastore):

- Buckets: `aw-watcher-window_<host>` (type `currentwindow`, data
  `{app, title}`) and `aw-watcher-afk_<host>` (type `afkstatus`, data
  `{status: "afk"|"not-afk"}`). Only buckets whose `hostname` matches
  `/api/0/info` are imported.
- Events: `{id, timestamp (ISO-8601 UTC), duration (s), data}`; returned
  newest-first.
- A range query returns events **overlapping** the range, **trimmed** to it.
- Heartbeats extend the most recent event in place (same `id`, longer duration).

Consequences for sync:

- Dedupe key = `(source, aw_id)` where `source` = bucket id + bucket `created`
  timestamp (a recreated bucket restarts ids and becomes a new source).
- Upsert keeps `min(start)` and `max(end)` → handles both trimming at chunk
  boundaries and heartbeat extension. Re-syncing is idempotent.
- Per-source cursor; each sync re-reads from `cursor − 10 min` to now in ≤1-day
  chunks, committing per chunk. First sync back-fills 30 days
  (`--backfill-days`). Because AW returns *overlapping* events, a long-running
  event that started before the cursor is still re-read and extended.
- The client bypasses system/env proxies so activity data never leaves the
  machine via a proxy.
- Verified against real ActivityWatch v0.13.2 (Python `aw-server`) on
  2026-10-01. Note: AW stamps bucket `created` with local time but a `+00:00`
  suffix; we only use it as an identity string.
- AW unreachable → sync reports status, stored history remains usable.

Not used: AW's query language (`/api/0/query`). We store raw events locally so
history survives AW reinstalls and so the idle/exclusion logic is testable.

## 5. Data model (schema v5)

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
  (`reset_window_cursors`) re-applies the current rules from ActivityWatch's
  raw data. Totals, reviews and stats report 업무 외 time separately; it never
  gets a project, and short 업무 외 detours are absorbed into work sessions
  like any other detour. The app picker reads app names live from
  ActivityWatch and does not store them.
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

The app is OS-independent (decision 2026-10-01): pure Python plus dulwich
and the optional MCP SDK; AI runs through the Claude Code CLI. OS-specific pieces are confined to:
per-OS data folder (`config.default_data_dir`), launchers
(`start-workhistory.cmd` / `.sh`), app-name tables for session grouping
(Windows executables, macOS application names, Linux X11 classes in
`sessions.py`), idle apps (`config.IDLE_APPS`), and exclusive port binding on
Windows (`server._ExclusiveServer`). The full test suite runs on Windows and
in a Linux container (Python 3.11, with and without the AI SDK).

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
- The MCP server's instructions tell Claude Code that window titles, paths and
  diffs are untrusted text and never instructions (prompt-injection defence for
  the user's interactive Claude Code; the app's own headless runs have no tools).
- Known, accepted (local-only): no per-launch token against other accounts on
  the same PC; `--trace`/`--demo --db` can bypass the MCP consent flag for a
  local caller who could read the database anyway. See
  docs/history/2026-10-21-02-eval-security-review.md.

## 9. Licensing

| Component | License | How we use it | Obligation |
|---|---|---|---|
| ActivityWatch (`activitywatch`, `aw-server`, `aw-server-rust`, `aw-core`, `aw-client`, `aw-watcher-window`, `aw-watcher-afk`) | MPL-2.0 | Separate process, REST API only; **no AW code copied** | None for our code. MPL-2.0 permits commercial use; it is file-level copyleft and applies only if we modify/distribute MPL files. |
| AW Windows bundle (`aw-qt` tray) | contains **PyQt6** (GPL-3 or commercial) | Installed separately by the user | Do **not** redistribute the AW bundle inside a commercial product without legal review. Future option: ship only `aw-server-rust` + watchers (audit their deps first) or write our own small collector. |
| pywebview (optional) | BSD-3-Clause | Optional desktop window | Keep notice if distributed. |
| Python, SQLite | PSF / public domain | Runtime | Notice for Python if bundled. |

"ActivityWatch" is the upstream project's name; don't use it in our product
name or branding.

## 10. Known collector limitations

- Window titles only; no URLs (that needs `aw-watcher-web` browser extension)
  and no file paths unless the editor puts them in the title.
- AFK detection uses ActivityWatch's default 3-minute threshold; reading
  without input counts as idle.
- Pausing in our app does not stop ActivityWatch's own recording (it has no
  pause API in v0.13.2) — see PRIVACY.md.
- Elevated (admin) windows may report as `unknown` to a non-elevated watcher.
