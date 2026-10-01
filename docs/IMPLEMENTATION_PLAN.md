# Implementation Plan

## Phase 1 — Inspect and plan ✅

- Environment inspected, ActivityWatch obtained (portable v0.13.2), licenses
  checked, stack chosen, data model defined. See ARCHITECTURE.md.
- Blockers: none. ActivityWatch must be running for live data; the app degrades
  to "stored history only" when it is not.

## Phase 2 — Smallest end-to-end slice ✅ (2026-10-01)

1. `db.py` — schema v1 + migration runner (`PRAGMA user_version`).
2. `awclient.py` + `sync.py` — pull window/AFK buckets, upsert with dedupe,
   exclusions and blocked (pause/deleted) ranges at ingest, per-source cursor.
3. `normalize.py` + `classify.py` — idle subtraction, day clipping, rule
   matching (pure functions, unit-tested).
4. `service.py` + `server.py` — timeline, projects/rules, manual assignment,
   notes, search, exclusions, pause, delete.
5. `static/` — Korean UI: timeline with filters, project assignment, notes,
   search, settings.
6. `demo.py` — separate demo DB with clearly labeled sample data.
7. Verify end-to-end against real ActivityWatch.

Status: done. 65 unit/integration tests pass (normalization, idle handling,
time ranges, rules, persistence/migrations, search/filters, duplicate and
trimmed events, heartbeat extension, pause, exclusions, deletion, HTTP
security). End-to-end verified against ActivityWatch v0.13.2 on this machine:
sync → timeline → rule classification → manual override → note → search.
Not yet verified: the optional `--window` pywebview shell (not installed).

## Phase 3 — Sessions, daily review, resume ✅ (2026-10-01)

1. Migration v2: `sessions`, `session_days`, `meta`, `daily_reviews`.
2. Deterministic grouping (`sessions.py`): 10 min gap / 3 min project run /
   10 min app run; auto titles. Constants only; make them configurable only
   if real use shows a need.
3. Edits (title, project, note, exclude) follow a growing session; merge/split
   lock the range; edited sessions are frozen rather than dropped on regroup.
4. Timeline shows session cards (expand for window rows, split at a row,
   merge with next); totals per project come from sessions.
5. Daily review tab: template draft, editable and saved per day, inferred vs.
   user-written sections labelled; follow-up candidates from notes only.
6. Resume tab: projects by recency, next action (editable), last session and
   last window, recent sessions, notes.
7. Search covers session titles/notes and saved reviews.

Verified: unit + integration tests, demo UI walkthrough (rename, note, merge,
split, review, resume), and grouping on real ActivityWatch data.

## Phase 4 — Validate

Automated tests (105, `python -m unittest discover -s tests -t .`) cover every
area the spec lists: normalization, time ranges/day boundaries, idle handling,
session grouping and edit preservation, project rules, persistence and
migrations, search and filters, duplicate/trimmed events, privacy exclusions,
pause/deletion, HTTP security.

Setup for real use (2026-10-01): ActivityWatch autostarts at login
(user Startup folder), desktop shortcut → `start-workhistory.cmd`, single
instance (exclusive port bind on Windows; a second launch reopens the browser),
faster reconnect while ActivityWatch is still starting, demo DB removed, and
`python -m workhistory.stats` for correction rates.

Remaining validation is human: about a week of real use, following
[VALIDATION.md](VALIDATION.md), against the questions in PRODUCT.md. Things to watch:
- Are sessions too coarse or too fine? (tune `SESSION_GAP_S`,
  `MIN_PROJECT_RUN_S`, `UNASSIGNED_RUN_S`, `MIN_KIND_RUN_S` in `config.py`)
- Do auto titles help, or are they always renamed?
- Is the follow-up keyword list useful or noisy?

## Explicitly deferred

Packaging into an installer, app tray icon, browser URL watcher, FTS5,
weekly summary view, AI summaries, multi-device.
