"""Snapshots of the user's work folders in hidden Git repositories.

Each registered folder gets a bare repository under `<data dir>/snapshots/`.
A scan reads the folder (never writes to it), stores changed files as Git
blobs, builds the tree and commits it when it differs from the last snapshot.
Every such commit becomes a row in `changes` with per-file line counts.

Git implementation: dulwich (pure Python), used under its Apache-2.0 option.
See docs/RESULTS_TRACKING.md for the rules and their reasons.
"""

import difflib
import fnmatch
import logging
import os
import shutil
import sqlite3
import stat
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from dulwich.diff_tree import tree_changes
from dulwich.objects import Blob, Commit, Tree
from dulwich.repo import Repo

from .config import SNAPSHOT_INTERVAL_S
from .db import Database
from .extract import extract_text
from .store import Invalid, NotFound

log = logging.getLogger(__name__)

SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "build", "dist", "target", "out", "bin", "obj",
             ".gradle", ".idea", ".vs", ".vscode", "__pycache__", ".venv", "venv", ".mypy_cache",
             ".pytest_cache", ".next", ".nuxt", "coverage"}
# Never stored, not even in the local snapshot.
SECRET_PATTERNS = (".env", ".env.*", "*.pem", "*.key", "*.pfx", "*.p12", "*.jks", "*.keystore",
                   "id_rsa*", "id_ed25519*", "id_ecdsa*", "*credential*", "*secret*", "*.kdbx")
SKIP_PATTERNS = ("*.exe", "*.dll", "*.so", "*.dylib", "*.class", "*.jar", "*.war", "*.ear", "*.zip",
                 "*.7z", "*.rar", "*.tar", "*.gz", "*.pyc", "*.pyo", "*.o", "*.obj", "*.pdb", "*.iso",
                 "*.mp4", "*.mov", "*.avi", "*.log", "*.tmp", "~$*", "*.lock", "Thumbs.db", "desktop.ini", ".DS_Store")
MAX_FILE_BYTES = 5 * 1024 * 1024
EXACT_DIFF_MAX_LINES = 5000
FILE_MODE = 0o100644
DIR_MODE = 0o040000
REF = b"refs/heads/main"
AUTHOR = "WorkHistory <workhistory@localhost>".encode()


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    lower = name.lower()
    return any(fnmatch.fnmatchcase(lower, p.lower()) for p in patterns)


def is_secret(name: str) -> bool:
    return _matches(name, SECRET_PATTERNS)


def is_skipped(name: str) -> bool:
    return _matches(name, SKIP_PATTERNS)


@dataclass
class ScanResult:
    folder_id: int
    files: int = 0
    change_id: Optional[int] = None
    baseline: bool = False
    error: Optional[str] = None


def repo_path(repos_dir: Path, folder_id: int) -> Path:
    return Path(repos_dir) / f"{folder_id}.git"


def _open_repo(path: Path) -> Repo:
    if path.exists():
        return Repo(str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    return Repo.init_bare(str(path), mkdir=True)


def _walk(root: Path) -> Iterator[tuple[str, os.DirEntry]]:
    """(posix relative path, entry) of every file that may be snapshotted."""
    stack = [(root, "")]
    while stack:
        directory, prefix = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue  # unreadable directory: skip, try again next scan
        for entry in entries:
            name = entry.name
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if name.lower() not in SKIP_DIRS:
                        stack.append((Path(entry.path), f"{prefix}{name}/"))
                elif entry.is_file(follow_symlinks=False):
                    if not is_secret(name) and not is_skipped(name):
                        yield prefix + name, entry
            except OSError:
                continue


def _build_tree(store: Any, files: dict[str, bytes]) -> bytes:
    """Nested Git trees for {posix path: blob id}; returns the root tree id."""
    root: dict = {}
    for path, blob_id in files.items():
        node = root
        parts = path.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = blob_id

    def write(node: dict) -> bytes:
        tree = Tree()
        for name, value in node.items():
            if isinstance(value, dict):
                tree.add(name.encode("utf-8"), DIR_MODE, write(value))
            else:
                tree.add(name.encode("utf-8"), FILE_MODE, value)
        if tree.id not in store:
            store.add_object(tree)
        return tree.id

    return write(root)


def _text(repo: Repo, blob_id: Optional[bytes], path: str) -> Optional[str]:
    if not blob_id:
        return None
    return extract_text(path, repo[blob_id].data)


def _line_counts(old: str, new: str) -> tuple[int, int]:
    a, b = old.splitlines(), new.splitlines()
    if len(a) > EXACT_DIFF_MAX_LINES or len(b) > EXACT_DIFF_MAX_LINES:
        # Fast approximation for very large files: unmatched lines in each direction.
        ca, cb = Counter(a), Counter(b)
        return sum((cb - ca).values()), sum((ca - cb).values())
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


def scan(conn: sqlite3.Connection, repos_dir: Path, folder_id: int, now: Optional[float] = None) -> ScanResult:
    """Snapshot one folder; records a change when anything differs."""
    now = time.time() if now is None else now
    folder = conn.execute("SELECT * FROM watched_folders WHERE id = ?", (folder_id,)).fetchone()
    if folder is None:
        raise NotFound("폴더를 찾을 수 없습니다")
    result = ScanResult(folder_id)
    root = Path(folder["path"])
    if not root.is_dir():
        result.error = "폴더가 없거나 접근할 수 없습니다"
        conn.execute("UPDATE watched_folders SET last_scan_at = ?, last_error = ? WHERE id = ?",
                     (now, result.error, folder_id))
        return result

    cache = {r["path"]: (r["size"], r["mtime_ns"], r["blob_id"]) for r in conn.execute(
        "SELECT path, size, mtime_ns, blob_id FROM file_state WHERE folder_id = ?", (folder_id,))}
    repo = _open_repo(repo_path(repos_dir, folder_id))
    try:
        store = repo.object_store
        current: dict[str, bytes] = {}
        state: list[tuple] = []
        for rel, entry in _walk(root):
            try:
                st = entry.stat()
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_FILE_BYTES:
                continue
            cached = cache.get(rel)
            if cached and cached[0] == st.st_size and cached[1] == st.st_mtime_ns and cached[2].encode() in store:
                blob_id = cached[2].encode()
            else:
                try:
                    data = Path(entry.path).read_bytes()
                except OSError:
                    # Locked or vanished: keep the previous version instead of recording a deletion.
                    if cached:
                        current[rel] = cached[2].encode()
                        state.append((folder_id, rel, cached[0], cached[1], cached[2]))
                    continue
                blob_id = add_blob(repo, data)
            current[rel] = blob_id
            state.append((folder_id, rel, st.st_size, st.st_mtime_ns, blob_id.decode()))
        result.files = len(current)

        result.change_id, result.baseline = commit_snapshot(conn, repo, folder_id, current, now)
    finally:
        repo.close()

    conn.execute("DELETE FROM file_state WHERE folder_id = ?", (folder_id,))
    conn.executemany("INSERT INTO file_state (folder_id, path, size, mtime_ns, blob_id) VALUES (?, ?, ?, ?, ?)",
                     state)
    conn.execute("UPDATE watched_folders SET last_scan_at = ?, last_error = NULL WHERE id = ?", (now, folder_id))
    return result


def add_blob(repo: Repo, data: bytes) -> bytes:
    blob = Blob.from_string(data)
    if blob.id not in repo.object_store:
        repo.object_store.add_object(blob)
    return blob.id


def commit_snapshot(conn: sqlite3.Connection, repo: Repo, folder_id: int, files: dict[str, bytes],
                    now: float) -> tuple[Optional[int], bool]:
    """Commit {path: blob id} if it differs from the last snapshot.

    Returns (change id or None, whether it was the folder's first snapshot).
    """
    tree_id = _build_tree(repo.object_store, files)
    parent = repo.refs[REF] if REF in repo.refs else None
    parent_tree = repo[parent].tree if parent else None
    if tree_id == parent_tree:
        return None, False
    commit = Commit()
    commit.tree = tree_id
    commit.parents = [parent] if parent else []
    commit.author = commit.committer = AUTHOR
    commit.author_time = commit.commit_time = int(now)
    commit.author_timezone = commit.commit_timezone = int(time.localtime(now).tm_gmtoff)
    commit.encoding = b"UTF-8"
    commit.message = f"snapshot {time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}".encode()
    repo.object_store.add_object(commit)
    repo.refs[REF] = commit.id
    change_id = _record_change(conn, repo, folder_id, commit.id.decode(), now, parent_tree, tree_id,
                               baseline=parent is None)
    return change_id, parent is None


def _record_change(conn: sqlite3.Connection, repo: Repo, folder_id: int, commit_id: str, now: float,
                   old_tree: Optional[bytes], new_tree: bytes, baseline: bool) -> int:
    change_id = conn.execute("INSERT INTO changes (folder_id, commit_id, ts, baseline) VALUES (?, ?, ?, ?)",
                             (folder_id, commit_id, now, 1 if baseline else 0)).lastrowid
    totals = Counter()
    for ch in tree_changes(repo.object_store, old_tree, new_tree):
        old_path = ch.old.path.decode("utf-8") if ch.old and ch.old.path else None
        new_path = ch.new.path.decode("utf-8") if ch.new and ch.new.path else None
        old_blob = ch.old.sha if ch.old and ch.old.sha else None
        new_blob = ch.new.sha if ch.new and ch.new.sha else None
        path = new_path or old_path
        kind = {"add": "A", "delete": "D"}.get(ch.type, "M")
        added = removed = None
        if baseline:
            is_text = extract_text(path, repo[new_blob].data) is not None if new_blob else False
        else:
            old_text, new_text = _text(repo, old_blob, path), _text(repo, new_blob, path)
            is_text = old_text is not None or new_text is not None
            if is_text:
                added, removed = _line_counts(old_text or "", new_text or "")
                totals["lines_added"] += added
                totals["lines_removed"] += removed
        totals[{"A": "files_added", "M": "files_modified", "D": "files_deleted"}[kind]] += 1
        conn.execute(
            "INSERT INTO change_files (change_id, path, kind, is_text, lines_added, lines_removed, old_blob, "
            "new_blob) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (change_id, path, kind, 1 if is_text else 0, added, removed,
             old_blob.decode() if old_blob else None, new_blob.decode() if new_blob else None))
    conn.execute(
        "UPDATE changes SET files_added = ?, files_modified = ?, files_deleted = ?, lines_added = ?, "
        "lines_removed = ? WHERE id = ?",
        (totals["files_added"], totals["files_modified"], totals["files_deleted"], totals["lines_added"],
         totals["lines_removed"], change_id))
    return change_id


def file_diff(repos_dir: Path, conn: sqlite3.Connection, file_id: int, context: int = 3) -> dict:
    """Unified diff of one changed file (document files: diff of extracted text)."""
    row = conn.execute(
        "SELECT cf.*, c.folder_id FROM change_files cf JOIN changes c ON c.id = cf.change_id WHERE cf.id = ?",
        (file_id,)).fetchone()
    if row is None:
        raise NotFound("변경 파일을 찾을 수 없습니다")
    repo = Repo(str(repo_path(repos_dir, row["folder_id"])))
    try:
        old = _text(repo, row["old_blob"].encode() if row["old_blob"] else None, row["path"])
        new = _text(repo, row["new_blob"].encode() if row["new_blob"] else None, row["path"])
    finally:
        repo.close()
    if old is None and new is None:
        return {"path": row["path"], "kind": row["kind"], "text": False, "diff": ""}
    diff = "\n".join(difflib.unified_diff((old or "").splitlines(), (new or "").splitlines(),
                                          f"a/{row['path']}", f"b/{row['path']}", n=context, lineterm=""))
    return {"path": row["path"], "kind": row["kind"], "text": True, "diff": diff}


# ---------------------------------------------------------------- folder management

def _normalize(path: Any) -> Path:
    if not isinstance(path, str) or not path.strip():
        raise Invalid("폴더 경로를 입력하세요")
    p = Path(path.strip().strip('"')).expanduser()
    if not p.is_absolute():
        raise Invalid("전체 경로를 입력하세요 (예: C:\\work\\gov24-api)")
    try:
        p = p.resolve(strict=True)
    except OSError:
        raise Invalid("폴더가 없거나 접근할 수 없습니다") from None
    if not p.is_dir():
        raise Invalid("폴더가 아닙니다")
    if p.parent == p:
        raise Invalid("드라이브 전체는 등록할 수 없습니다. 작업 폴더를 지정하세요")
    return p


def add_folder(conn: sqlite3.Connection, data: dict, repos_dir: Path) -> dict:
    path = _normalize(data.get("path"))
    if Path(repos_dir).resolve() in (path, *path.parents) or path in Path(repos_dir).resolve().parents:
        raise Invalid("작업 기록 앱의 데이터 폴더는 등록할 수 없습니다")
    for r in conn.execute("SELECT path FROM watched_folders"):
        other = Path(r["path"])
        if other == path or other in path.parents or path in other.parents:
            raise Invalid(f"이미 등록된 폴더와 겹칩니다: {other}")
    label = (data.get("label") or "").strip() or path.name
    project_id = data.get("project_id") or None
    if project_id is not None and conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
        raise NotFound("프로젝트를 찾을 수 없습니다")
    folder_id = conn.execute(
        "INSERT INTO watched_folders (path, label, project_id, created_at) VALUES (?, ?, ?, ?)",
        (str(path), label[:100], project_id, time.time())).lastrowid
    return get_folder(conn, folder_id)


def get_folder(conn: sqlite3.Connection, folder_id: int) -> dict:
    row = conn.execute(
        "SELECT f.*, (SELECT COUNT(*) FROM file_state s WHERE s.folder_id = f.id) AS files, "
        "(SELECT MAX(ts) FROM changes c WHERE c.folder_id = f.id AND c.baseline = 0) AS last_change_at "
        "FROM watched_folders f WHERE f.id = ?", (folder_id,)).fetchone()
    if row is None:
        raise NotFound("폴더를 찾을 수 없습니다")
    return dict(row)


def list_folders(conn: sqlite3.Connection) -> list[dict]:
    return [get_folder(conn, r["id"]) for r in conn.execute("SELECT id FROM watched_folders ORDER BY label")]


def update_folder(conn: sqlite3.Connection, folder_id: int, data: dict) -> dict:
    get_folder(conn, folder_id)
    if "label" in data:
        label = str(data["label"] or "").strip()
        if not label:
            raise Invalid("이름을 입력하세요")
        conn.execute("UPDATE watched_folders SET label = ? WHERE id = ?", (label[:100], folder_id))
    if "project_id" in data:
        pid = data["project_id"] or None
        if pid is not None and conn.execute("SELECT 1 FROM projects WHERE id = ?", (pid,)).fetchone() is None:
            raise NotFound("프로젝트를 찾을 수 없습니다")
        conn.execute("UPDATE watched_folders SET project_id = ? WHERE id = ?", (pid, folder_id))
    if "enabled" in data:
        conn.execute("UPDATE watched_folders SET enabled = ? WHERE id = ?", (1 if data["enabled"] else 0, folder_id))
    return get_folder(conn, folder_id)


def remove_folder(conn: sqlite3.Connection, folder_id: int, repos_dir: Path) -> None:
    """Forget a folder: its change records and its snapshot repository."""
    get_folder(conn, folder_id)
    conn.execute("DELETE FROM watched_folders WHERE id = ?", (folder_id,))
    shutil.rmtree(repo_path(repos_dir, folder_id), ignore_errors=True)


# ---------------------------------------------------------------- background

class Snapshotter:
    """Scans every enabled folder periodically on a background thread."""

    def __init__(self, db: Database, repos_dir: Path, interval_s: float = SNAPSHOT_INTERVAL_S):
        self.db = db
        self.repos_dir = Path(repos_dir)
        self.interval_s = interval_s
        self.last_results: list[ScanResult] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()

    def run_now(self) -> list[ScanResult]:
        with self._lock:
            results = []
            with self.db.connect() as conn:
                ids = [r["id"] for r in conn.execute("SELECT id FROM watched_folders WHERE enabled = 1")]
            for folder_id in ids:
                try:
                    with self.db.connect() as conn:
                        results.append(scan(conn, self.repos_dir, folder_id))
                except Exception as e:  # one bad folder must not stop the others; never log file content
                    log.exception("snapshot failed for folder %s", folder_id)
                    results.append(ScanResult(folder_id, error=f"기록 오류: {type(e).__name__}"))
            self.last_results = results
            return results

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_now()
            self._wake.wait(self.interval_s)
            self._wake.clear()

    def start(self) -> None:
        threading.Thread(target=self._loop, name="snapshots", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
