# Results-based work history (작업 결과물 기록)

## Why (decision 2026-10-01)

Work is increasingly done *with AI*, and in 폐쇄망 environments results are
carried in by USB. Window-focus time then says little about what was done
("ChatGPT, 3 hours"), and work is split across PCs. The unit of record moves
from **window time** to **results: file changes**. Window tracking stays as
an optional, secondary signal.

Target setting for the hackathon demo: a Windows PC inside a 폐쇄망. The LLM
is Claude Code for now (assumed reachable), behind a provider interface so a
폐쇄망 LLM server can replace it.

## What it does

1. The user registers work folders (source code, documents).
2. Every few minutes the app snapshots each folder into its **own hidden Git
   repository** (`%LOCALAPPDATA%\WorkHistory\snapshots\<id>.git`, bare). The
   user's own `.git` directories are skipped and never written to.
3. Each snapshot that differs from the previous one becomes a **change**:
   files added / modified / deleted with line counts, stored in SQLite for
   the timeline, search and AI.
4. Files copied in from a USB stick appear as additions/modifications like
   any other change, so the work trail continues across the air gap.
5. AI reports and questions cite changes (`C<id>`) next to sessions and notes,
   with the same reference verification as before.

## Git implementation and licensing

- **dulwich 1.2.15** (pure-Python Git), used under its **Apache-2.0** option
  (`Apache-2.0 OR GPL-2.0-or-later`); dependency urllib3 is MIT. No git.exe is
  bundled, so there is no GPL component. Snapshots are standard Git
  repositories that any Git tool can read.
- Objects are written directly (blobs → trees → commit); no index or working
  tree is involved, so the user's folder is only ever read.

## Scanning rules

- Change detection by `(size, mtime_ns)` per file; only changed files are read
  and hashed.
- Skipped directories: `.git`, `node_modules`, `build`, `dist`, `target`,
  `out`, `bin`, `obj`, `.gradle`, `.idea`, `.vs`, `.vscode`, `__pycache__`,
  `.venv`, `venv`.
- **Never stored** (secrets): `.env*`, `*.pem`, `*.key`, `*.pfx`, `*.p12`,
  `*.jks`, `*.keystore`, `id_rsa*`, `id_ed25519*`, `*credentials*`,
  `*secret*`.
- Skipped: files over 5 MB and binary build outputs (`*.exe`, `*.dll`,
  `*.class`, `*.jar`, `*.war`, `*.zip`, `*.7z`, `*.pyc`, ...).
- Text files: line diffs. Documents (`.hwp`, `.docx`, `.xlsx`, `.pptx`):
  extracted text is diffed; extraction results are cached by blob id.

## Data protection

- Snapshots are a local, versioned copy of the registered folders. Removing a
  folder from the list or "delete all" removes its snapshot repository.
- Before anything is sent to an LLM, diff excerpts pass a secret-redaction
  step (key/token/password patterns) and are size-limited.
- The free-tier guard from AI.md applies unchanged.

## Limitations (to document honestly)

- Deleting a date range cannot surgically remove history from a Git
  repository; it removes the change records and their text, and the snapshot
  repository is rewritten without those commits (later step).
- Snapshot granularity is the scan interval (default 5 min); several saves
  within one interval become one change.
- Renames are recorded as delete + add.
