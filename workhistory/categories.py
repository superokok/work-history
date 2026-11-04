"""What kind of activity a window was: development, documents, browsing, ... (pure functions).

Rules only, no AI: the app name decides most of it; for browsers the window title
tells work pages (issue trackers, code hosting, wikis, internal systems) from
general surfing. It is an inference and the dashboard labels it so.
"""

import re

from .classify import OFFWORK_APP
from .sessions import BROWSER_APPS, CHAT_APPS, CODE_APPS, MEETING_APPS, app_key

# Display order, labels and colors (the dashboard and its legend use them as they are).
CATEGORIES = [
    ("dev", "개발", "#2a78d6"),
    ("doc", "문서", "#1baf7a"),
    ("comm", "소통·회의", "#eda100"),
    ("web_work", "업무 웹", "#6250d6"),
    ("web", "웹 (기타)", "#7fa7d9"),
    ("surf", "인터넷 서핑", "#e87ba4"),
    ("other", "기타", "#a3a19a"),
    ("offwork", "업무 외", "#d8d6cf"),
]
KEYS = [key for key, _, _ in CATEGORIES]
WORK = {"dev", "doc", "comm", "web_work"}  # counted as work time on the dashboard

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

WORK_TITLE = re.compile(
    r"jira|confluence|gitlab|github|bitbucket|gitea|jenkins|sonar|nexus|harbor|argo|grafana|kibana|"
    r"prometheus|swagger|postman|localhost|127\.0\.0\.1|stack ?overflow|\bmdn\b|docs\.|documentation|"
    r"\bapi\b|wiki|위키|redmine|trello|asana|notion|figma|slack|teams|outlook|webmail|메일|그룹웨어|"
    r"결재|인트라넷|intranet|\berp\b|groupware|google docs|문서|스프레드시트|sheets|slides|drive|"
    r"\baws\b|azure|\bgcp\b|console|chatgpt|claude|gemini|copilot|ai studio|pypi|\bnpm\b|maven|docker|"
    r"kubernetes|\bk8s\b|\bdb\b|\bsql\b|오류|에러|error|exception|tutorial|가이드|guide|manual|매뉴얼",
    re.I)
SURF_TITLE = re.compile(
    r"youtube|유튜브|netflix|넷플릭스|twitch|치지직|아프리카|instagram|인스타그램|facebook|페이스북|"
    r"twitter|트위터|x\.com|tiktok|틱톡|reddit|웹툰|webtoon|네이버 뉴스|다음 뉴스|뉴스|news|"
    r"쿠팡|coupang|11번가|g마켓|gmarket|옥션|auction|무신사|쇼핑|shopping|aliexpress|알리익스프레스|"
    r"amazon|네이버 스포츠|스포츠|sports|연예|entertain|게임|game|steam|블로그|blog|카페|"
    r"부동산|주식|증권|코인|bitcoin|업비트|여행|travel|맛집|날씨|weather",
    re.I)


def category(app: str, title: str) -> str:
    """Category key for one window (see CATEGORIES)."""
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
        title = title or ""
        if WORK_TITLE.search(title):
            return "web_work"
        if SURF_TITLE.search(title):
            return "surf"
        return "web"
    return "other"
