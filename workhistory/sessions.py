"""Deterministic grouping of activities into work sessions (pure functions).

1. A gap of more than SESSION_GAP_S without activity (idle time is already
   removed) ends a session.
2. Within that, a switch to another known project starts a new session only
   if it lasts MIN_PROJECT_RUN_S. Unclassified work (UNASSIGNED_RUN_S) and
   업무 외 time (OFFWORK_RUN_S) shorter than their limits stay inside the
   surrounding session: looking something up is part of the work.
3. Within one project, a kind of work that lasts MIN_KIND_RUN_S becomes its
   own session: coding (IDE, terminal, API/DB tools), reading (browser),
   chat (messengers, mail) and meetings (MIN_MEETING_RUN_S). Switching apps
   inside the same kind of work never splits a session.
"""

import re
from collections import defaultdict
from typing import Callable, Hashable, Optional, Sequence, Union

from .classify import OFFWORK_APP
from .config import (MIN_KIND_RUN_S, MIN_MEETING_RUN_S, MIN_PROJECT_RUN_S, OFFWORK_RUN_S,
                     SESSION_GAP_S, UNASSIGNED_RUN_S)
from .normalize import Activity

# App names differ by OS: Windows reports the executable ("idea64.exe"), macOS the
# application name ("IntelliJ IDEA", "zoom.us"), Linux the X11 class ("jetbrains-idea").
# All lookups use app_key(): case-folded, without ".exe" / ".app".
APP_LABELS = {
    "idea64": "IntelliJ IDEA", "idea": "IntelliJ IDEA", "intellij idea": "IntelliJ IDEA",
    "jetbrains-idea": "IntelliJ IDEA", "code": "VS Code", "visual studio code": "VS Code",
    "chrome": "Chrome", "google chrome": "Chrome", "google-chrome": "Chrome", "msedge": "Edge",
    "microsoft edge": "Edge", "firefox": "Firefox", "safari": "Safari", "whale": "Whale",
    "windowsterminal": "Terminal", "terminal": "Terminal", "iterm2": "iTerm2",
    "gnome-terminal-server": "Terminal", "konsole": "Terminal", "explorer": "탐색기", "finder": "Finder",
    "slack": "Slack", "kakaotalk": "카카오톡", "postman": "Postman", "devenv": "Visual Studio",
    "pycharm64": "PyCharm", "pycharm": "PyCharm", "jetbrains-pycharm": "PyCharm",
    "datagrip64": "DataGrip", "datagrip": "DataGrip", "dbeaver": "DBeaver", "notion": "Notion",
    "teams": "Teams", "ms-teams": "Teams", "microsoft teams": "Teams", "teams-for-linux": "Teams",
    "outlook": "Outlook", "microsoft outlook": "Outlook", "zoom": "Zoom", "zoom.us": "Zoom",
}
OFFWORK_LABEL = "업무 외"
CODE_APPS = {"idea64", "idea", "intellij idea", "jetbrains-idea", "code", "visual studio code", "pycharm64",
             "pycharm", "jetbrains-pycharm", "devenv", "eclipse", "webstorm64", "webstorm", "datagrip64",
             "datagrip", "dbeaver", "postman", "insomnia", "windowsterminal", "cmd", "powershell", "pwsh",
             "terminal", "iterm2", "gnome-terminal-server", "konsole", "alacritty", "kitty", "xterm",
             "sourcetree", "githubdesktop", "github desktop", "sqldeveloper64", "sqldeveloper",
             "notepad++", "xcode", "android studio", "studio64"}
BROWSER_APPS = {"chrome", "google chrome", "google-chrome", "chromium", "msedge", "microsoft edge",
                "firefox", "safari", "whale", "naver whale"}
CHAT_APPS = {"slack", "kakaotalk", "teams", "ms-teams", "microsoft teams", "microsoft teams (work or school)",
             "teams-for-linux", "outlook", "olk", "microsoft outlook", "mail", "thunderbird"}
MEETING_APPS = {"zoom", "zoom.us", "webex", "cisco webex meetings"}
MEETING_TITLE = re.compile(r"회의|미팅|스크럼|통화|meeting|call", re.I)
TITLE_SEPARATOR = re.compile(r"\s+[-–—|]\s+")
MAX_TITLE_LEN = 90


def app_key(app: str) -> str:
    return re.sub(r"\.(exe|app)$", "", (app or "").strip(), flags=re.I).casefold()


def app_label(app: str) -> str:
    return APP_LABELS.get(app_key(app)) or re.sub(r"\.(exe|app)$", "", app, flags=re.I)


def clean_title(title: str, app: str) -> str:
    """Drop a trailing " - Google Chrome"-style app suffix from a window title."""
    title = title.strip()
    separators = list(TITLE_SEPARATOR.finditer(title))
    if separators:
        last = separators[-1]
        tail = title[last.end():].casefold()
        base = app_key(app)
        if (base and base in tail) or app_label(app).casefold() in tail:
            return title[:last.start()].strip()
    return title


def _dominant(activities: Sequence[Activity], key: Callable[[Activity], Hashable]):
    totals: dict = defaultdict(float)
    for a in activities:
        totals[key(a)] += a.duration
    # max() keeps the first key on ties; dicts preserve first-seen order.
    return max(totals, key=totals.get)


def offwork_seconds(activities: Sequence[Activity]) -> float:
    return sum(a.duration for a in activities if a.app == OFFWORK_APP)


def dominant_project(activities: Sequence[Activity]) -> Optional[int]:
    """By time, ignoring "업무 외" time (which never has a project)."""
    work = [a for a in activities if a.app != OFFWORK_APP]
    return _dominant(work, lambda a: a.project_id) if work else None


def auto_title(activities: Sequence[Activity]) -> str:
    if not activities:
        return ""
    work = [a for a in activities if a.app != OFFWORK_APP]
    if not work or offwork_seconds(activities) * 2 >= sum(a.duration for a in activities):
        return OFFWORK_LABEL
    activities = work
    app = _dominant(activities, lambda a: a.app)
    title = _dominant([a for a in activities if a.app == app], lambda a: a.title)
    cleaned = clean_title(title, app)
    text = f"{app_label(app)} · {cleaned}" if cleaned else app_label(app)
    return text if len(text) <= MAX_TITLE_LEN else text[:MAX_TITLE_LEN - 1] + "…"


def work_kind(a: Activity) -> str:
    """Coarse kind of work, so that e.g. IDE + terminal + DB tool stay one session."""
    app = app_key(a.app)
    if app in MEETING_APPS or (app in CHAT_APPS and MEETING_TITLE.search(a.title or "")):
        return "meeting"
    if app in CODE_APPS:
        return "code"
    if app in BROWSER_APPS:
        return "read"
    if app in CHAT_APPS:
        return "chat"
    return "other"


def _kind_run_minimum(kind: Hashable) -> float:
    return MIN_MEETING_RUN_S if kind == "meeting" else MIN_KIND_RUN_S


def split_by_gap(activities: Sequence[Activity], gap_s: float = SESSION_GAP_S) -> list[list[Activity]]:
    blocks: list[list[Activity]] = []
    for a in activities:
        if blocks and a.start - blocks[-1][-1].end <= gap_s:
            blocks[-1].append(a)
        else:
            blocks.append([a])
    return blocks


def smooth_runs(activities: Sequence[Activity], key: Callable[[Activity], Hashable],
                min_s: Union[float, Callable[[Hashable], float]]) -> list[list[Activity]]:
    """Split into runs of equal key, absorbing runs shorter than their minimum.

    The shortest too-short run is repeatedly merged into its longer neighbour,
    so a brief detour (Slack, a quick search) stays inside the surrounding
    work. `min_s` may depend on the run's key.
    """
    threshold = min_s if callable(min_s) else (lambda _k: min_s)
    runs: list[list] = []  # [key, activities, active seconds]
    for a in activities:
        k = key(a)
        if runs and runs[-1][0] == k:
            runs[-1][1].append(a)
            runs[-1][2] += a.duration
        else:
            runs.append([k, [a], a.duration])

    while len(runs) > 1:
        short = [j for j in range(len(runs)) if runs[j][2] < threshold(runs[j][0])]
        if not short:
            break
        i = min(short, key=lambda j: runs[j][2])
        if i == 0:
            j = 1
        elif i == len(runs) - 1:
            j = i - 1
        else:
            j = i - 1 if runs[i - 1][2] >= runs[i + 1][2] else i + 1
        lo, hi = min(i, j), max(i, j)
        runs[lo:hi + 1] = [[runs[j][0], runs[lo][1] + runs[hi][1], runs[lo][2] + runs[hi][2]]]
        # The absorbing run may now touch a run with the same key.
        for k in (lo, lo - 1):
            if 0 <= k < len(runs) - 1 and runs[k][0] == runs[k + 1][0]:
                runs[k:k + 2] = [[runs[k][0], runs[k][1] + runs[k + 1][1], runs[k][2] + runs[k + 1][2]]]
    return [r[1] for r in runs]


_OFFWORK = "offwork"  # project-level key for 업무 외 time


def _project_key(a: Activity) -> Hashable:
    return _OFFWORK if a.app == OFFWORK_APP else a.project_id


def _project_run_minimum(key: Hashable) -> float:
    if key is None:
        # Unclassified work (a browser search, a doc) usually belongs to the
        # project around it: only a long stretch stands on its own.
        return UNASSIGNED_RUN_S
    if key == _OFFWORK:
        return OFFWORK_RUN_S  # a short break does not end a session, like idle time
    return MIN_PROJECT_RUN_S


def group_activities(activities: Sequence[Activity]) -> list[list[Activity]]:
    """Activities (sorted, non-overlapping) → list of sessions."""
    sessions: list[list[Activity]] = []
    for block in split_by_gap(activities):
        for project_run in smooth_runs(block, _project_key, _project_run_minimum):
            sessions.extend(smooth_runs(project_run, work_kind, _kind_run_minimum))
    return sessions
