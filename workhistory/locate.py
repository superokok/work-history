"""Which file a window has open: a file in a work folder, found from the window title.

Editors and IDEs don't tell Windows which file they have open and their titles hold
only the file name, so the name in the title is looked up in the files of the
registered work folders (query time, nothing new is recorded). Browser addresses are
not read (2026-11-10): the window title is enough.
"""

import re
import sqlite3
from collections import defaultdict
from pathlib import Path

_FILE_NAME = re.compile(r"[\w.\-]+\.[A-Za-z0-9]{1,8}", re.UNICODE)
# A relative path ending in a file name, as Eclipse titles show it: "project-a/src/main/App.java".
_REL_PATH = re.compile(r"[\w.\-]+(?:[/\\][\w.\-]+)+\.[A-Za-z0-9]{1,8}", re.UNICODE)
# Title parts: "name.xlsx - Excel", "name.hwp [Z:\docs\] - 한글", "workspace - project-a/App.java - Eclipse IDE".
_PARTS = re.compile(r"\s+[-–—|]\s+|\s*[\[\]]\s*")
_WHOLE_NAME = re.compile(r"[^\\/:*?\"<>|]+\.[A-Za-z0-9]{1,8}", re.UNICODE)
# Office titles leave the extension out when Windows hides extensions: "교육자료 - PowerPoint".
_OFFICE_EXTS = {"powerpoint": ("pptx", "ppt", "pptm"), "word": ("docx", "doc", "docm"),
                "excel": ("xlsx", "xls", "xlsm")}


def _names(title: str) -> list[str]:
    """File names a title may name, best first: whole title parts (names with spaces, such as
    "주간 보고 v2.hwpx"), then the name-like words in it, then an Office title's name with its
    usual extensions."""
    parts = [p.strip() for p in _PARTS.split(title) if p and p.strip()]
    names = [p for p in parts if _WHOLE_NAME.fullmatch(p)]
    names += [n for n in _FILE_NAME.findall(title) if n not in names]
    if len(parts) >= 2 and parts[-1].casefold() in _OFFICE_EXTS:
        names += [f"{parts[0]}.{ext}" for ext in _OFFICE_EXTS[parts[-1].casefold()]]
    return [n for n in names if not n.replace(".", "").isdigit()]


def _norm(path: str) -> str:
    return path.replace("/", "\\").casefold()


def file_paths(conn: sqlite3.Connection, titles: list[str]) -> dict[str, str]:
    """Window title → full path of the work-folder file it names, where that is unambiguous.

    A name found in several places is narrowed down by a relative path in the title that
    ends in it (Eclipse: "project-a/src/main/App.java", the end of the file's full path),
    else by the other words of the title (IDE titles usually start with the project folder's name).
    """
    wanted: dict[str, list[str]] = {}
    for title in titles:
        names = _names(title or "")
        if names:
            wanted[title] = names
    if not wanted:
        return {}
    by_name: dict[str, list[str]] = defaultdict(list)
    needed = {n.casefold() for names in wanted.values() for n in names}
    for r in conn.execute("SELECT f.path AS root, s.path AS rel FROM file_state s "
                          "JOIN watched_folders f ON f.id = s.folder_id"):
        name = r["rel"].rsplit("/", 1)[-1]
        if name.casefold() in needed:
            by_name[name.casefold()].append(str(Path(r["root"], *r["rel"].split("/"))))
    out = {}
    for title, names in wanted.items():
        words = [w.casefold() for w in re.split(r"\s+[-–—|·]\s+|\s+", title) if len(w) > 2]
        rels = [_norm(r) for r in _REL_PATH.findall(title)]
        for name in names:
            found = by_name.get(name.casefold(), [])
            if len(found) > 1:
                tail = [r for r in rels if r.rsplit("\\", 1)[-1] == name.casefold()]
                found = [p for p in found if any(_norm(p).endswith("\\" + r) for r in tail)] or found
            if len(found) > 1:  # copies of a project elsewhere end the same way: the title's other words decide
                found = [p for p in found if any(w in p.casefold() for w in words if w != name.casefold())]
            if len(found) == 1:
                out[title] = found[0]
                break
    return out
