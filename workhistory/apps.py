"""App names, labels and window titles (pure functions)."""

import re

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


def duration(seconds: float) -> str:
    """'45분' / '2시간 5분' for reports."""
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes}분"
    return f"{minutes // 60}시간 {minutes % 60}분"
