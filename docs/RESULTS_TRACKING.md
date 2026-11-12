# Results-based work history (작업 결과물 기록)

## Why (decision 2026-10-01)

Work is increasingly done *with AI*, and in 폐쇄망 environments results are
carried in by USB. Window-focus time then says little about what was done
("ChatGPT, 3 hours"), and work is split across PCs. The unit of record moves
from **window time** to **results: file changes**. Window tracking stays as
an optional, secondary signal.

Target setting: a Windows PC inside a 폐쇄망. The LLM runs on the PC itself
(llama.cpp + Qwen3.5, shipped in the zip), behind a provider interface (see AI.md).

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
5. AI reports and questions cite changes (`C<id>`) next to work records (`W`),
   with the same reference verification as before.

## Which project a change belongs to (decision 2026-10-28)

People often register one parent folder (`workspace`, a file-server document
folder) that holds several projects. The app **suggests, the user confirms**;
it never assigns a project on a guess, because a silently wrong project ends
up in a report and is hard to notice, while an unassigned change stays visible.

- The folder's own project (optional) is the default for all its files.
- For a folder without one, the projects tab shows "프로젝트가 정해지지 않은
  파일 변경 N건" and lists the sub-folders those changes are in. A project whose
  name equals the sub-folder name (ignoring case, spaces, `-`, `_`, `.`) is
  pre-selected, but nothing is saved until the user presses 확인.
- "안쪽 폴더로 나누기" lists the next level (for `2026/기획팀/...` layouts).
- "프로젝트 없음 (묻지 않기)" is a decision too: that sub-folder is not asked again.
- The deepest confirmed sub-folder wins (`folder_areas`), then the folder's project.
  Each changed file gets a project; a change (one snapshot) takes the project
  most of its files belong to. Search and "AI에게 묻기" use the per-file project.
- Changing a folder's project or a sub-folder confirmation re-applies to past
  changes. Deleting a project makes its sub-folders unassigned again.

## Git implementation and licensing

- **dulwich 1.2.15** (pure-Python Git), used under its **Apache-2.0** option
  (`Apache-2.0 OR GPL-2.0-or-later`); dependency urllib3 is MIT. No git.exe is
  bundled, so there is no GPL component. Snapshots are standard Git
  repositories that any Git tool can read.
- Objects are written directly (blobs → trees → commit); no index or working
  tree is involved, so the user's folder is only ever read.

## Scanning rules

- Change detection by `(size, mtime_ns)` per file; only changed files are read
  and hashed. A file whose size and modified time did not change is not opened
  at all (not even its stored copy), and when no file changed, no tree is built
  and nothing is written: a scan that finds nothing new only lists the folder.
- New objects are written as one pack file per scan (at most 64 MB each)
  instead of one loose file per object, with fast compression (zlib level 1).
- The snapshot thread runs in Windows background mode (lower CPU, disk and
  memory priority), so a long scan gives way to the programs in use.
- Measured on a development laptop with a 36,535-file folder: the first scan
  (baseline) about 90 s in the background, every later scan with no changes
  about 3 s (before 2026-11-10: 235 s and 28 s). A 1,500-file folder: 6 s and
  0.4 s.
- Skipped directories: `.git`, `node_modules`, `build`, `dist`, `target`,
  `out`, `bin`, `obj`, `.gradle`, `.idea`, `.vs`, `.vscode`, `__pycache__`,
  `.venv`, `venv`.
- **Never stored** (secrets): `.env*`, `*.pem`, `*.key`, `*.pfx`, `*.p12`,
  `*.jks`, `*.keystore`, `id_rsa*`, `id_ed25519*`, `*credentials*`,
  `*secret*`, `*.kdbx`, `.netrc`, `.npmrc`, `.pypirc`, `.pgpass`,
  `.htpasswd`, `*.ppk`, `*.ovpn`, `kubeconfig*`, `*.tfstate*`, `*.gpg`.
- Symlinks and Windows directory junctions are not followed, so a link
  inside a work folder cannot pull in files from elsewhere.
- Skipped: binary build outputs, archives and logs (`*.exe`, `*.dll`,
  `*.class`, `*.jar`, `*.war`, `*.zip`, `*.7z`, `*.pyc`, `*.log`, ...).
- Files over 5 MB (2026-11-12): kept without their content, as a small
  stand-in with the size and modified time (`snapshots.large_stand_in`). Adding,
  changing and deleting them is recorded as for any file; a change is noticed
  by size or modified time. Big files already in a folder when this started (modified before
  `meta.large_files_since`, the first scan that kept them) are added to the snapshot
  without being listed as work.
- The first snapshot (baseline) is not work: its files are recorded without
  reading them back to tell text from binary.
- Text files: line diffs, in UTF-8, CP949 or UTF-16 with a byte order mark.
  Documents (`.hwp`, `.hwpx`, `.docx`, `.xlsx`, `.pptx`): extracted text is
  diffed (paragraphs; a table cell or text box inside a paragraph is its own
  line). A document that would unpack to more than 50 MB is treated as binary
  (decompression-bomb guard).
- No text to compare, with the reason in the change view: images (`png`,
  `jpg`, `gif`, `bmp`, `webp`) show before and after side by side, served by
  `/api/change-files/<id>/image` to this PC's own page only (never SVG, which
  can carry scripts; it is diffed as text); PDF is never read as text (its bytes
  can look like text); old binary Office files (`doc`, `xls`, `ppt`) and other
  binary files say so. The AI gets only the file name of these.

## Data protection

- Snapshots are a local, versioned copy of the registered folders. Removing a
  folder from the list or "delete all" removes its snapshot repository.
- Before anything is sent to an LLM, diff excerpts pass a secret-redaction
  step (key/token/password patterns) and are size-limited. Whole file texts
  are redacted before diffing, so multi-line private keys are masked too.

## Notes

- Snapshot granularity is the scan interval (default 5 min); several saves
  within one interval become one change.
- Renames are recorded as delete + add.
- One snapshot can touch two projects' sub-folders; the timeline shows it under
  the project most of its files belong to (the AI context marks the other files).
