"""Nothing from the developer's own PC may ship: not in the zip, not in the repository.

Looks for traces of the machine that builds or tests (home folder path, user and
computer name, git identity) and for files that only hold someone's own records
(databases, logs, history bundles). Used by tools/build_portable.py on the package
before it is zipped, and by tests/test_clean.py on every file git tracks.
Stdlib only: the build runs with a plain system Python.
"""

import os
import re
import zipfile
from pathlib import Path
from typing import Iterable

# Files that are someone's own data, never part of the product.
DATA_FILES = re.compile(r"\.(db|db-wal|db-shm|db-journal|sqlite3?|log|bundle|jsonl|gguf-part)$", re.I)
# Compiled third-party binaries carry their own builders' paths; they are not ours to scan.
BINARY = re.compile(r"\.(exe|dll|pyd|so|gguf|zip|png|jpe?g|gif|ico|pdf)$", re.I)
# Office files: their text and their document properties (author, last saved by) are checked.
OFFICE = re.compile(r"\.(pptx|docx|xlsx)$", re.I)
OFFICE_PEOPLE = re.compile(r"<(dc:creator|cp:lastModifiedBy|Manager|Company)>([^<]+)</", re.I)
MIN_LEN = 4  # shorter names (e.g. a 3-letter user name) are only checked as path parts


def _git_identity(root: Path) -> set[str]:
    """user.name / user.email from the repository's and the user's git config."""
    found = set()
    for cfg in (root / ".git" / "config", Path.home() / ".gitconfig"):
        try:
            text = cfg.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found.update(m.group(2).strip() for m in re.finditer(r"^\s*(name|email)\s*=\s*(.+)$", text, re.M))
    return found


def markers(root: Path) -> list[str]:
    """Strings that would identify this PC or its user."""
    home = Path.home()
    found = {str(home), home.as_posix(), str(root.resolve()), root.resolve().as_posix(),
             "Users\\" + home.name, "Users/" + home.name}
    for var in ("USERNAME", "COMPUTERNAME", "USERDOMAIN"):
        value = os.environ.get(var, "")
        if len(value) >= MIN_LEN:
            found.add(value)
    found.update(v for v in _git_identity(root) if len(v) >= MIN_LEN)
    generic = {"user", "admin", "administrator", "workgroup", "github", "users"}
    return sorted(m for m in found if m and m.casefold() not in generic)


def scan(files: Iterable[Path], base: Path, marks: list[str]) -> list[str]:
    """Problems as "relative/path: reason"; empty when clean."""
    patterns = [re.compile(re.escape(m), re.I) for m in marks]
    problems = []
    for f in files:
        rel = f.relative_to(base).as_posix()
        if DATA_FILES.search(f.name):
            problems.append(f"{rel}: personal data file")
            continue
        if BINARY.search(f.name):
            continue
        if OFFICE.search(f.name):
            with zipfile.ZipFile(f) as z:
                parts = [z.read(n).decode("utf-8", errors="replace") for n in z.namelist()
                         if n.endswith((".xml", ".rels"))]
            for part in parts:
                for tag, who in OFFICE_PEOPLE.findall(part):
                    problems.append(f"{rel}: document property {tag} = {who.strip()!r}")
            text = "\n".join(parts)
        else:
            text = f.read_bytes().decode("utf-8", errors="replace")
        for m, p in zip(marks, patterns):
            if p.search(text):
                problems.append(f"{rel}: contains {m!r}")
    return problems
