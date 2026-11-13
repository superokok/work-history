"""What kind of activity a window was: development, documents, chat, web, ... (pure functions).

Rules only, no AI: the app name decides it. It is an inference and the dashboard
labels it so.
"""

from .classify import OFFWORK_APP
from .apps import BROWSER_APPS, CHAT_APPS, CODE_APPS, MEETING_APPS, app_key

# Display order, labels and colors (the dashboard and its legend use them as they are).
CATEGORIES = [
    ("dev", "개발", "#2a78d6"),
    ("doc", "문서", "#1baf7a"),
    ("comm", "소통·회의", "#eda100"),
    ("web", "웹", "#6250d6"),
    ("other", "기타", "#a3a19a"),
    ("offwork", "업무 외", "#d8d6cf"),
]
KEYS = [key for key, _, _ in CATEGORIES]
CATEGORIES_LABELS = [(key, label) for key, label, _ in CATEGORIES]
WORK = set(KEYS) - {"offwork"}  # 업무 외 = apps the user does not record as work (기록 대상)

DEV_APPS = CODE_APPS | {"claude", "cursor", "windsurf", "sublime_text", "vim", "gvim", "nvim-qt",
                        "wsl", "bash", "mintty", "putty", "mobaxterm", "winscp", "filezilla",
                        "burpsuite", "fiddler", "wireshark", "charles", "httpie", "dbvis", "ssms",
                        "heidisql", "navicat", "toad", "mysqlworkbench", "pgadmin4", "robo3t", "studio3t"}
DOC_APPS = {"hwp", "hword", "hcell", "hshow", "hanword", "winword", "excel", "powerpnt", "onenote",
            "acrord32", "acrobat", "foxitpdfreader", "foxit reader", "notepad", "wordpad", "obsidian",
            "notion", "typora", "soffice", "soffice.bin", "swriter", "scalc", "simpress", "mspub",
            "visio", "drawio", "draw.io", "figma", "mspaint", "snippingtool"}
COMM_APPS = CHAT_APPS | MEETING_APPS | {"discord", "telegram", "line", "whatsapp", "webex", "gotomeeting",
                                        "skype", "lync", "nateon", "mattermost", "jandi", "dooray", "flow"}


# Apps the user named (naming.py): their name -> kind. Set from the user's rules when activity is read.
_RENAMED: dict[str, str] = {}


def set_renamed(kinds: dict[str, str]) -> None:
    global _RENAMED
    _RENAMED = dict(kinds)


def category(app: str, title: str = "") -> str:
    """Category key for one window (see CATEGORIES): the user's kind for an app they named, else by app."""
    if app == OFFWORK_APP:
        return "offwork"
    return _RENAMED.get(app_key(app)) or default_category(app)


def default_category(app: str) -> str:
    """The kind of an app by its name alone (the lists above)."""
    if app == OFFWORK_APP:
        return "offwork"
    key = app_key(app)
    if key in DEV_APPS:
        return "dev"
    if key in DOC_APPS:
        return "doc"
    if key in COMM_APPS:
        return "comm"
    if key in BROWSER_APPS:
        return "web"
    return "other"
