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
from collections import Counter, defaultdict
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
                   "id_rsa*", "id_ed25519*", "id_ecdsa*", "*credential*", "*secret*", "*.kdbx",
                   ".netrc", "_netrc", ".npmrc", ".pypirc", ".pgpass", ".htpasswd", "*.ppk", "*.ovpn",
                   "kubeconfig*", "*.tfstate", "*.tfstate.*", "*.gpg")
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


def _is_link(entry: os.DirEntry) -> bool:
    """Symlinks and Windows junctions: they can lead outside the watched folder.

    os.scandir does not count a junction as a symlink, so it gets its own check. Other
    reparse points (e.g. OneDrive placeholder files) are ordinary files and stay included."""
    if entry.is_symlink():
        return True
    tag = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None)
    if tag is None:
        return False  # not Windows
    return getattr(os.lstat(entry.path), "st_reparse_tag", 0) == tag


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
                if _is_link(entry):
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
    resolve = _resolver(conn, folder_id)
    projects = []
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
        project_id = resolve(path)[0]
        projects.append(project_id)
        conn.execute(
            "INSERT INTO change_files (change_id, path, kind, is_text, lines_added, lines_removed, old_blob, "
            "new_blob, project_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (change_id, path, kind, 1 if is_text else 0, added, removed,
             old_blob.decode() if old_blob else None, new_blob.decode() if new_blob else None, project_id))
    conn.execute(
        "UPDATE changes SET files_added = ?, files_modified = ?, files_deleted = ?, lines_added = ?, "
        "lines_removed = ?, project_id = ? WHERE id = ?",
        (totals["files_added"], totals["files_modified"], totals["files_deleted"], totals["lines_added"],
         totals["lines_removed"], _dominant(projects, resolve("")[0]), change_id))
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
    undecided = _undecided_files(conn, folder_id)
    return {**dict(row), "unassigned_changes": len({r["change_id"] for r in undecided}),
            "unassigned_files": len(undecided),
            "areas": conn.execute("SELECT COUNT(*) FROM folder_areas WHERE folder_id = ?", (folder_id,)).fetchone()[0]}


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
        attribute_folder(conn, folder_id)
    if "enabled" in data:
        conn.execute("UPDATE watched_folders SET enabled = ? WHERE id = ?", (1 if data["enabled"] else 0, folder_id))
    return get_folder(conn, folder_id)


def remove_folder(conn: sqlite3.Connection, folder_id: int, repos_dir: Path) -> None:
    """Forget a folder: its change records and its snapshot repository."""
    get_folder(conn, folder_id)
    conn.execute("DELETE FROM watched_folders WHERE id = ?", (folder_id,))
    shutil.rmtree(repo_path(repos_dir, folder_id), ignore_errors=True)


# ---------------------------------------------------------------- projects for parts of a folder
#
# The app never decides on its own: it lists the sub-folders whose changes have no project yet,
# pre-selects a project whose name equals the folder name, and only what the user confirms is saved.

def _resolver(conn: sqlite3.Connection, folder_id: int):
    """path -> (project id, decided). The deepest confirmed sub-folder wins, then the folder's own project."""
    row = conn.execute("SELECT project_id FROM watched_folders WHERE id = ?", (folder_id,)).fetchone()
    default = row["project_id"] if row else None
    areas = sorted(((r["subpath"].casefold(), r["project_id"]) for r in conn.execute(
        "SELECT subpath, project_id FROM folder_areas WHERE folder_id = ?", (folder_id,))),
        key=lambda a: -len(a[0]))

    def resolve(path: str) -> tuple[Optional[int], bool]:
        p = path.casefold()
        for sub, pid in areas:
            if p == sub or p.startswith(sub + "/"):
                return pid, True
        return default, default is not None
    return resolve


def _dominant(projects: list[Optional[int]], default: Optional[int]) -> Optional[int]:
    """A change's project: the one most of its files belong to (files without one don't count)."""
    counts = Counter(p for p in projects if p is not None)
    return counts.most_common(1)[0][0] if counts else (None if projects else default)


def attribute_folder(conn: sqlite3.Connection, folder_id: int) -> None:
    """Recompute the project of every recorded change of a folder after its projects changed."""
    resolve = _resolver(conn, folder_id)
    by_change: dict[int, list] = defaultdict(list)
    files = []
    for r in conn.execute("SELECT cf.id, cf.change_id, cf.path FROM change_files cf "
                          "JOIN changes c ON c.id = cf.change_id WHERE c.folder_id = ?", (folder_id,)):
        pid = resolve(r["path"])[0]
        files.append((pid, r["id"]))
        by_change[r["change_id"]].append(pid)
    conn.executemany("UPDATE change_files SET project_id = ? WHERE id = ?", files)
    default = resolve("")[0]
    conn.executemany("UPDATE changes SET project_id = ? WHERE id = ?", [
        (_dominant(by_change.get(r["id"], []), default), r["id"])
        for r in conn.execute("SELECT id FROM changes WHERE folder_id = ?", (folder_id,)).fetchall()])


def attribute_all(conn: sqlite3.Connection) -> None:
    for r in conn.execute("SELECT id FROM watched_folders").fetchall():
        attribute_folder(conn, r["id"])


def _undecided_files(conn: sqlite3.Connection, folder_id: int) -> list[sqlite3.Row]:
    """Changed files (not the baseline) with no project and no confirmed "no project" either."""
    resolve = _resolver(conn, folder_id)
    return [r for r in conn.execute(
        "SELECT cf.change_id, cf.path, c.ts FROM change_files cf JOIN changes c ON c.id = cf.change_id "
        "WHERE c.folder_id = ? AND c.baseline = 0 AND cf.project_id IS NULL", (folder_id,))
        if not resolve(r["path"])[1]]


def _subpath(value: Any, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 500:
        raise Invalid("하위 폴더 경로가 올바르지 않습니다")
    parts = [p.strip() for p in value.replace("\\", "/").split("/") if p.strip()]
    if any(p in (".", "..") for p in parts) or not (parts or allow_empty):
        raise Invalid("하위 폴더 경로가 올바르지 않습니다")
    return "/".join(parts)


def _same_name(a: str, b: str) -> bool:
    norm = lambda s: "".join(ch for ch in s.casefold() if ch not in " -_.")  # noqa: E731
    return bool(norm(a)) and norm(a) == norm(b)


def list_areas(conn: sqlite3.Connection, folder_id: int, under: Any = "") -> dict:
    """Sub-folders one level below `under` whose changes have no project yet, each with a suggestion."""
    get_folder(conn, folder_id)
    under = _subpath(under, allow_empty=True)
    prefix = under.casefold() + "/" if under else ""
    projects = conn.execute("SELECT id, name FROM projects WHERE archived = 0 ORDER BY id").fetchall()
    groups: dict[str, dict] = {}
    loose_files, loose_changes = 0, set()
    for r in _undecided_files(conn, folder_id):
        if not r["path"].casefold().startswith(prefix):
            continue
        rest = r["path"][len(prefix):]
        if "/" not in rest:
            loose_files += 1
            loose_changes.add(r["change_id"])
            continue
        name = rest.split("/", 1)[0]
        g = groups.setdefault(name.casefold(), {"subpath": f"{under}/{name}" if under else name, "name": name,
                                                "files": 0, "changes": set(), "last_ts": 0})
        g["files"] += 1
        g["changes"].add(r["change_id"])
        g["last_ts"] = max(g["last_ts"], r["ts"])
    suggest = lambda name: next((p["id"] for p in projects if _same_name(p["name"], name)), None)  # noqa: E731
    out = [{**g, "changes": len(g["changes"]), "suggested_project_id": suggest(g["name"])}
           for g in sorted(groups.values(), key=lambda g: -g["last_ts"])]
    return {"under": under, "under_suggested_project_id": suggest(under.rsplit("/", 1)[-1]) if under else None,
            "groups": out, "loose_files": loose_files, "loose_changes": len(loose_changes),
            "areas": [dict(r) for r in conn.execute(
                "SELECT subpath, project_id FROM folder_areas WHERE folder_id = ? ORDER BY subpath COLLATE NOCASE",
                (folder_id,))]}


def set_area(conn: sqlite3.Connection, folder_id: int, data: dict) -> dict:
    """Confirm the project of a sub-folder (project_id null = "no project", so it is not asked again)."""
    get_folder(conn, folder_id)
    sub = _subpath(data.get("subpath"))
    pid = data.get("project_id")
    if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int) or conn.execute(
            "SELECT 1 FROM projects WHERE id = ?", (pid,)).fetchone() is None):
        raise NotFound("프로젝트를 찾을 수 없습니다")
    conn.execute("DELETE FROM folder_areas WHERE folder_id = ? AND subpath = ? COLLATE NOCASE", (folder_id, sub))
    conn.execute("INSERT INTO folder_areas (folder_id, subpath, project_id, created_at) VALUES (?, ?, ?, ?)",
                 (folder_id, sub, pid, time.time()))
    attribute_folder(conn, folder_id)
    return get_folder(conn, folder_id)


def remove_area(conn: sqlite3.Connection, folder_id: int, data: dict) -> dict:
    get_folder(conn, folder_id)
    sub = _subpath(data.get("subpath"))
    if conn.execute("DELETE FROM folder_areas WHERE folder_id = ? AND subpath = ? COLLATE NOCASE",
                    (folder_id, sub)).rowcount == 0:
        raise NotFound("지정한 하위 폴더가 없습니다")
    attribute_folder(conn, folder_id)
    return get_folder(conn, folder_id)


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
