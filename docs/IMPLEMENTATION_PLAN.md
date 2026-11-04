# Implementation Plan

## Phase 1 — Inspect and plan ✅

- Environment inspected, licenses checked, stack chosen, data model defined.
  See ARCHITECTURE.md.

## Phase 2 — Smallest end-to-end slice ✅ (2026-10-01)

1. `db.py` — schema v1 + migration runner (`PRAGMA user_version`).
2. `sync.py` — import window/idle events, upsert with dedupe, exclusions and
   blocked (pause/deleted) ranges at ingest, per-source cursor.
3. `normalize.py` + `classify.py` — idle subtraction, day clipping, rule
   matching (pure functions, unit-tested).
4. `service.py` + `server.py` — timeline, projects/rules, manual assignment,
   notes, search, exclusions, pause, delete.
5. `static/` — Korean UI: timeline with filters, project assignment, notes,
   search, settings.
6. `demo.py` — separate demo DB with clearly labeled sample data.

Verified end to end on real data: sync → timeline → rule classification →
manual override → note → search.

## Phase 3 — Sessions, daily review, resume ✅ (2026-10-01)

1. Migration v2: `sessions`, `session_days`, `meta`, `daily_reviews`.
2. Deterministic grouping (`sessions.py`): 10 min gap / 3 min project run /
   10 min app run; auto titles. Thresholds in `config.py`.
3. Edits (title, project, note, exclude) follow a growing session; merge/split
   lock the range; edited sessions are frozen rather than dropped on regroup.
4. Timeline shows session cards (expand for window rows, split at a row,
   merge with next); totals per project come from sessions.
5. Daily review tab: template draft, editable and saved per day, inferred vs.
   user-written sections labelled; follow-up candidates from notes only.
6. Resume tab: projects by recency, next action (editable), last session and
   last window, recent sessions, notes.
7. Search covers session titles/notes and saved reviews.

## Phase 4 — Results, AI and validation ✅ (2026-10-01 ~ 10-27)

- Work-folder snapshots as the primary record (RESULTS_TRACKING.md).
- AI report drafts and "AI에게 묻기" with verified citations and the honesty
  rules in code (AI.md).
- Automated tests cover normalization, time ranges/day boundaries, idle
  handling, session grouping and edit preservation, project rules,
  persistence and migrations, search and filters, duplicate/trimmed events,
  privacy exclusions, pause/deletion, AI reference checks and HTTP security
  (`python -m unittest discover -s tests -t .`).

## Phase 5 — Ready for other PCs ✅ (2026-10-28 ~ 11-04)

- Window activity recorded by the app itself (`collector.py`); Windows only.
- Local AI on the PC (llama.cpp + Qwen3.5). The
  closed-network package (`--offline`, models inside) is the release; Claude
  Code stays in the source for development and quality comparison.
- Runs without a console window (`pythonw`), stops with "앱 종료" in the app; log file
  and message box instead of a console.
- Port 5730, else the next free one up to 5739; one app per database by a named mutex.
- Repository and zip kept free of the developer PC's traces (`tools/clean_check.py`,
  `tests/test_clean.py`, checked before every zip).

## Phase 6 — Fewer tabs (2026-11-05)

- Removed the template daily review tab (`review.py`, `daily_reviews` dropped in
  schema v12) and the 이어서 하기 tab; the AI 업무일지 and "AI에게 묻기" cover them.
- Projects, keyword rules and work folders moved into 설정 (was 개인정보); each
  project card edits its "다음 할 일".
- Tabs: 타임라인, 보고서, 검색, 설정.

## Out of scope

Installer, tray icon, multi-device. (The browser address is read since 2026-11-05.)
