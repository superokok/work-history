# Privacy

## What is collected

Via ActivityWatch, on this computer only:

- Foreground application executable name (e.g. `Code.exe`)
- Foreground window title (e.g. `UserController.java - gov24-api - Visual Studio Code`)
- Start time and duration of each window focus period
- AFK (idle) / not-AFK status periods

## What is never collected

Keystrokes, clipboard, passwords, screenshots, screen recordings, OCR,
document/file contents, browser URLs, network traffic, microphone or camera.

## Where data lives

- App database: `%LOCALAPPDATA%\WorkHistory\workhistory.db` (override with
  `--db`). Demo data uses a separate `demo.db`.
- ActivityWatch keeps **its own** database under
  `%LOCALAPPDATA%\activitywatch\`. Our app reads from it; it does not control
  it.
- ActivityWatch listens on `127.0.0.1:5600` only (verified with `netstat` on
  2026-10-01); keep its `host` setting at the default. It has no
  authentication, so programs running under your account can read it; the
  same is true of our app's API and DB file.
- The app's data folder is readable only by your account, SYSTEM and
  administrators (default `%LOCALAPPDATA%` ACL). The DB is not encrypted;
  use BitLocker/device encryption against theft.
- Nothing is uploaded. The app makes no network requests other than to
  ActivityWatch on `127.0.0.1:5600`.
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
  until the folder is removed (known limitation).
- Before any text is sent to an AI model (diff excerpts, notes, window titles,
  session titles, next actions) it is passed through `redact.py`, which masks
  private keys, JWT/Bearer tokens, AWS/Google/Slack/GitHub/`sk-` keys,
  passwords in URLs and `.netrc` lines, `password/token/secret/api_key = value` pairs,
  Korean resident registration numbers, card numbers and long random tokens.

## Controls

| Control | Effect in our DB | Effect in ActivityWatch |
|---|---|---|
| **Record only work apps** (기록 대상: 업무 앱만 기록) | Only windows matching a rule (app name contains X, optionally title contains Y) are imported with app and title. All other windows are stored as anonymous **업무 외** time: no app name, no title. "최근 30일에 다시 적용" re-imports from ActivityWatch with the current rules. | None. AW records every window; it has no allow-list option. |
| **Pause / resume** (일시정지) | Events that start during a pause are discarded at import; paused time is removed from timelines. | **None.** AW keeps recording. To stop AW too, quit it from the tray icon. |
| **Exclusion list** (제외 목록) | Events whose app or title contains a listed keyword (case-insensitive) are never imported; adding a rule also purges existing matching events. | None. AW stores them. For AW-side exclusion, set `exclude_titles` in AW's `aw-watcher-window` config (check your AW version supports it). |
| **Delete data** (데이터 삭제) | Deletes imported events, notes, sessions and daily reviews for a date range or everything. | None. Delete AW buckets from the AW web UI (`http://localhost:5600`) if desired. Deleted ranges are not re-imported unless you reset the sync cursor. |

## Sensitive data in window titles

Window titles can contain email subjects, chat names, document names, customer
names, or ticket titles. Add keywords for sensitive apps or titles (e.g.
`KeePass`, `1Password`, `인터넷뱅킹`, `카카오톡`) to the exclusion list before
long-term use.

## Logging

Logs record counts, timings and error types only. Window titles, app names
from events, note text, and search queries are not logged.

## Claude Code connection (MCP)

Off by default. When enabled in 개인정보 → Claude Code 연결, Claude Code may
call the app's read-only tools; their results (redacted, recording rules
applied) are sent to the model provider of the Claude account in use. Turning
the switch off withdraws consent (the server then refuses every call) and
removes the app's registration from Claude Code. The app changes Claude Code's
user configuration only through this switch, only for its own server name, and
never edits Claude Code's files directly.

## Start at logon and shortcuts

Off by default. 개인정보 → 실행 creates or removes only files with the app's
own name: Windows `Startup\작업 기록.lnk` and `Desktop\작업 기록.lnk`, macOS
`~/Library/LaunchAgents/com.workhistory.app.plist`, Linux
`~/.config/autostart/workhistory.desktop` and
`~/.local/share/applications/workhistory.desktop`. Not available in demo mode.

## Optional AI

Off until the user picks a model in 개인정보 → AI 설정 (the app asks for
consent then) and runs a report or a question. With a "내 PC" model the
records go only to the local model program the app started on this PC
(127.0.0.1, random key); nothing leaves the PC. With a Claude Code model,
and only for that request, redacted work records are sent through Claude
Code to Anthropic. "사용 안 함" turns it off again. "보낼 내용 보기" shows the exact
report payload. The app stores no API key. Locally it logs token counts and
timings only, never prompts or answers.
