# Privacy

## What is collected

By the app itself while it runs (Windows), on this computer only:

- Foreground application executable name (e.g. `Code.exe`)
- Foreground window title (e.g. `UserController.java - gov24-api - Visual Studio Code`)
- Start time and duration of each window focus period
- AFK (idle) / not-AFK status periods

## What is never collected

Keystrokes, clipboard, passwords, screenshots, screen recordings, OCR,
document/file contents, page contents, browser addresses, browsing history, network
traffic, microphone or camera. (Browser addresses were read from v13 until 2026-11-10;
the update deletes the ones recorded.) File paths are not collected: the timeline looks a file name from a window
title up in the registered work folders' file list (already recorded) to show its path.

## Where data lives

- App database: `%LOCALAPPDATA%\WorkHistory\workhistory.db` (override with
  `--db`). Demo data uses a separate `demo.db`.
- The collector keeps its raw window/idle events in the same app database
  (`collector_events`) for 35 days, so tracking rules can be re-applied; older rows are
  deleted automatically. Nothing is recorded while paused or for excluded windows.
- The app's data folder is readable only by your account, SYSTEM and
  administrators (default `%LOCALAPPDATA%` ACL). The DB is not encrypted;
  use BitLocker/device encryption against theft.
- Nothing is uploaded. Window activity is read inside the app; no other program or
  network service is involved.
- The app's web server listens on `127.0.0.1` only and rejects requests with a
  foreign `Host` header or without the app's custom header on writes. On
  Windows it binds the port exclusively (`SO_EXCLUSIVEADDRUSE`), so no other
  program can bind the same port and intercept requests.

## Work-folder snapshots

- Registered work folders are snapshotted into hidden Git repositories under
  `%LOCALAPPDATA%\WorkHistory\workhistory-snapshots\`. This is a local,
  versioned **copy of file contents**, protected like the rest of the data
  folder (your account only). Nothing is uploaded.
- Secret files are never copied: `.env*`, `*.pem`, `*.key`, `*.pfx`, `*.p12`,
  `*.jks`, `*.keystore`, `id_rsa*`, `id_ed25519*`, `*credential*`,
  `*secret*`, `*.kdbx`, `.netrc`, `.npmrc`, `.pypirc`, `.pgpass`,
  `.htpasswd`, `*.ppk`, `*.ovpn`, `kubeconfig*`, `*.tfstate*`, `*.gpg`.
  Build outputs, archives and files over 5 MB are skipped, and symlinks or
  junctions are not followed. The folder and its own `.git` are only ever read.
- Removing a folder deletes its change records and its snapshot repository.
  "Delete all" removes every snapshot repository. Deleting a date range
  removes the change records; the commits stay in the snapshot repository
  until the folder is removed.
- Before any text is sent to an AI model (diff excerpts, window titles, notes
  in "AI에게 묻기") it is passed through `redact.py`, which masks
  private keys, JWT/Bearer tokens, AWS/Google/Slack/GitHub/`sk-` keys,
  passwords in URLs and `.netrc` lines, `password/token/secret/api_key = value` pairs,
  Korean resident registration numbers, card numbers and long random tokens.

## Controls

| Control | Effect |
|---|---|
| **Record only work apps** (기록 대상: 업무 앱만 기록) | Only windows matching a rule (app name contains X, optionally title contains Y) are imported with app and title. All other windows are stored as anonymous **업무 외** time: no app name, no title. The raw events keep them for 35 days so the app picker can list them and "최근 30일에 다시 적용" can re-import with the current rules. |
| **Pause / resume** (일시정지) | Nothing is recorded while paused; paused time is removed from timelines. |
| **Exclusion list** (제외 목록) | Windows whose app or title contains a listed keyword (case-insensitive) are never recorded or imported; adding a rule also purges existing matching events, raw ones included. |
| **Delete data** (데이터 삭제) | Deletes imported and raw events and notes (and session rows older versions kept) for a date range or everything. Deleted ranges are not re-imported. |

## Sensitive data in window titles

Window titles can contain email subjects, chat names, document names, customer
names, or ticket titles. Add keywords for sensitive apps or titles (e.g.
`KeePass`, `1Password`, `인터넷뱅킹`, `카카오톡`) to the exclusion list before
long-term use.

## Logging

Logs record counts, timings and error types only. Window titles, app names
from events, note text, and search queries are not logged. Started without a console
(the normal way), the app writes them to `%LOCALAPPDATA%\WorkHistory\workhistory.log`
(at most 1 MB plus one older file); started from a console, they go to the console.

## Start at logon and shortcuts

Off by default. 설정 → 실행 creates or removes only files with the app's
own name: `Startup\작업 기록.lnk` and `Desktop\작업 기록.lnk`. Not available in demo mode.

## Optional AI

Off until the user picks a model in 설정 → AI 설정 (the app asks for
consent then) and runs a report or a question. With a "내 PC" model the
records go only to the local model program the app started on this PC
(127.0.0.1, random key); nothing leaves the PC. "사용 안 함" turns it off again. "보낼 내용 보기" shows the exact
report payload. Locally the app logs token counts and timings only, never
prompts or answers.

The released (closed-network) zip has no other choice. A development install
with the `claude` extra also lists Claude Code models (for quality
comparison): with one of those, and only for that request, redacted work
records are sent through Claude Code to Anthropic.
