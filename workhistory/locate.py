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


def file_paths(conn: sqlite3.Connection, titles: list[str]) -> dict[str, str]:
    """Window title → full path of the work-folder file it names, where that is unambiguous.

    A name found in several places is narrowed down by the other words of the title
    (IDE titles usually start with the project folder's name).
    """
    wanted: dict[str, list[str]] = {}
    for title in titles:
        names = [n for n in _FILE_NAME.findall(title or "") if not n.replace(".", "").isdigit()]
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
        for name in names:
            found = by_name.get(name.casefold(), [])
            if len(found) > 1:
                found = [p for p in found if any(w in p.casefold() for w in words if w != name.casefold())]
            if len(found) == 1:
                out[title] = found[0]
                break
    return out
