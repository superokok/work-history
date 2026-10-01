"""Template-based daily review text (pure functions, no AI).

Every section header says whether its content is inferred from activity data
or written by the user.
"""

import re
from datetime import date, datetime
from typing import Optional

from .config import REVIEW_MIN_SESSION_S

FOLLOWUP_PATTERN = re.compile(r"TODO|FIXME|확인|해야|필요|다음에|내일|나중에|\?", re.I)
WEEKDAYS = "월화수목금토일"


def duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes}분"
    return f"{minutes // 60}시간 {minutes % 60}분"


def hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _project_name(projects: dict[int, dict], project_id: Optional[int]) -> str:
    return projects[project_id]["name"] if project_id in projects else "미분류"


def followups(notes: list[dict], sessions: list[dict], projects: dict[int, dict]) -> list[str]:
    """Candidates the user may want to follow up on, never invented content."""
    items: list[str] = []
    for n in notes:
        for line in n["body"].splitlines():
            if FOLLOWUP_PATTERN.search(line):
                items.append(f"(메모) {line.strip()}")
    for s in sessions:
        for line in s["note"].splitlines():
            if FOLLOWUP_PATTERN.search(line):
                items.append(f"(세션 {hhmm(s['start'])}) {line.strip()}")
    worked = {s["project_id"] for s in sessions if s["project_id"] in projects}
    for pid in sorted(worked, key=lambda p: projects[p]["name"]):
        if projects[pid]["next_action"].strip():
            items.append(f"(다음 할 일) [{projects[pid]['name']}] {projects[pid]['next_action'].strip()}")
    return items


def build_review(day: date, by_project: list[dict], sessions: list[dict], notes: list[dict],
                 projects: dict[int, dict], offwork_s: float = 0) -> str:
    """`sessions` must leave out excluded and "업무 외" sessions."""
    lines = [f"# {day.isoformat()} ({WEEKDAYS[day.weekday()]}) 작업 리뷰", ""]

    lines.append("## 프로젝트별 시간 [자동 집계]")
    if by_project or offwork_s:
        for p in by_project:
            lines.append(f"- {_project_name(projects, p['project_id'])}: {duration(p['seconds'])}")
        work = sum(p["seconds"] for p in by_project)
        if offwork_s:
            lines.append(f"- 업무 외 (기록 대상이 아닌 앱): {duration(offwork_s)}")
            lines.append(f"- 합계: 업무 {duration(work)} / 전체 {duration(work + offwork_s)}")
        else:
            lines.append(f"- 합계: {duration(work)}")
    else:
        lines.append("- 기록된 활동 없음")
    lines.append("")

    lines.append("## 주요 작업 세션 [자동 추정 — 실제 작업 내용은 직접 확인하세요]")
    major = [s for s in sessions if s["active_s"] >= REVIEW_MIN_SESSION_S] or sessions
    for s in major:
        lines.append(f"- {hhmm(s['start'])}–{hhmm(s['end'])} ({duration(s['active_s'])}) "
                     f"[{_project_name(projects, s['project_id'])}] {s['title']}")
        for line in s["note"].splitlines():
            lines.append(f"  - 세션 메모: {line}")
    if not major:
        lines.append("- 없음")
    lines.append("")

    last_by_project: dict[Optional[int], dict] = {}
    for s in sessions:
        if s["project_id"] in projects:
            last_by_project[s["project_id"]] = s
    if last_by_project:
        lines.append("## 프로젝트별 마지막 작업 [자동 추정]")
        for pid, s in last_by_project.items():
            lines.append(f"- {projects[pid]['name']}: {hhmm(s['end'])}까지 — {s['title']}")
        lines.append("")

    lines.append("## 메모 [직접 작성]")
    for n in notes:
        prefix = f"[{projects[n['project_id']]['name']}] " if n["project_id"] in projects else ""
        lines.append(f"- {prefix}{n['body']}")
    if not notes:
        lines.append("- 없음")
    lines.append("")

    lines.append("## 후속 확인 후보 [메모·다음 할 일에서 자동 추출]")
    lines.extend(f"- {item}" for item in followups(notes, sessions, projects) or ["없음"])
    lines.append("")

    lines.append("## 오늘 한 일 [직접 작성]")
    lines.append("- ")
    return "\n".join(lines) + "\n"
