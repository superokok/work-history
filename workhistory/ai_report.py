"""AI work report drafts (daily 업무일지 / weekly 주간보고) with verified citations.

The model writes what was done, from the records; plans (차주 계획 / 다음 할 일) are
the user's to write, so that section is left empty in the draft."""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .ai_context import Context, dumps
from .apps import duration

KINDS = {"daily": "업무일지", "weekly": "주간보고"}
SECTION_LABELS = {
    "daily": ("오늘 한 일", "다음 할 일"),
    "weekly": ("금주 실적", "차주 계획"),
}
# Whether a piece of work is finished is the user's call (the records show work, not that it is
# done): every line of what was done starts as 진행 중, and the user marks it 완료 in the report.
IN_PROGRESS = "진행 중"
STATUS_SUFFIX = re.compile(r"\s*\((진행 중|완료)\)\s*$")

_ITEM = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "보고서 한 줄 (개조식, 명사형 종결)"},
        "refs": {"type": "array", "items": {"type": "string"},
                 "description": "이 항목의 근거가 된 자료의 ref 목록 (예: W3, C7)"},
    },
    "required": ["text", "refs"],
}

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "기간 전체를 1~2문장으로 요약"},
        "projects": {
            "type": "array",
            "description": "업무 묶음 목록 (기록에서 직접 묶음)",
            "items": {
                "type": "object",
                "properties": {
                    "project": {"type": "string",
                                "description": "업무 이름: 기록에 나온 이슈 번호·저장소·주제로 짧게, 묶기 어려우면 '기타'"},
                    "done": {"type": "array", "items": _ITEM, "description": "한 일 (실적)"},
                },
                "required": ["project", "done"],
            },
        },
    },
    "required": ["summary", "projects"],
}

SYSTEM = """당신은 업무 보고서 초안을 작성합니다.
입력 JSON은 이 사람의 업무 기록입니다.
- work(W): 활동 시간. folder가 있으면 그 작업 폴더의 파일을 다룬 시간(창 제목으로 확인한 것), kind가 있으면 작업 폴더 밖의 활동(웹·소통·기타)입니다. 하루치 자료에서는 date와 times(그 일을 한 시간대)가, 여러 날 자료에서는 기간 합계와 days(요일별 분)가 있습니다. windows는 오래 본 창입니다.
- changes(C): 작업 폴더에서 기록한 파일 변경. C 하나는 한 작업 폴더에서 이어서 바꾼 것을 묶은 것입니다 (time이 '09:10-11:40'처럼 범위일 수 있음).
프로젝트 분류나 메모는 없습니다. 일을 묶는 것은 당신이 합니다.

업무 묶기:
- 같은 일을 한 기록끼리 '업무' 하나로 묶습니다. 작업 폴더(folder), 이슈 번호(예: GOV-1418), MR 번호(예: !312), 창 제목의 파일 이름처럼 기록에 드러난 단서로 묶습니다.
- 보통 작업 폴더 하나가 업무 하나입니다. 한 폴더 안에서 이슈 번호나 바뀐 내용이 뚜렷이 다른 일을 했으면 나눌 수 있고, 그때는 나눈 묶음마다 그 폴더의 W를 넣습니다.
- 업무 이름(project)은 기록에 나온 이슈 번호나 저장소·주제를 써서 짧게 짓습니다 (예: 'GOV-1418 리프레시 토큰 재발급 버그', 'poc-k8s Helm 배포'). 기록에 없는 이름이나 회사 프로젝트명을 지어내지 않습니다.
- 이슈 번호와 MR 번호는 그 묶음이 인용한 자료(refs)의 창 제목·파일 이름·diff에 실제로 나온 것만 씁니다. 비슷한 주제라도 다른 일의 번호를 가져다 붙이지 않습니다. 번호가 드러나지 않으면 번호 없이 주제로 이름을 짓습니다.
- 웹·소통·기타(kind) 기록은 창 제목에 업무 단서가 있을 때만 그 업무 항목의 근거로 씁니다 (예: 'MR !318 …' 창은 그 MR의 업무). 내용을 알 수 없는 메신저, 메일, 검색, 문서 열람은 실적으로 쓰지 않습니다.
- 어디에도 묶기 어려운 업무 활동은 '기타'로 모읍니다. 같은 업무를 여러 묶음으로 나누지 않습니다.
- 업무 묶음은 일을 많이 한 순서로 씁니다.

반드시 지킬 것:
- 입력 자료에 있는 사실만 씁니다. 자료에 없는 작업, 결과, 수치, 원인을 지어내지 않습니다.
- 모든 항목의 refs에는 그 항목의 근거가 된 ref를 넣습니다. 업무 묶음마다 그 일을 한 작업 폴더의 W를 빠짐없이 넣습니다 (업무별 시간 계산에 씁니다).
- 파일 변경(C)은 실제로 만들거나 고친 결과물이므로 실적의 가장 좋은 근거입니다. diff_excerpt와 파일 이름, 같은 날 그 폴더의 W 창 제목을 함께 보고 무엇을 바꿨는지 업무 언어로 요약합니다. 코드나 경로를 길게 옮기지 말고 필요하면 클래스나 파일 이름만 괄호로 붙입니다.
- 한 일(done)을 먼저 씁니다. 기간 안의 파일 변경(C)은 하나도 빠짐없이 어느 업무의 한 일(done) 항목 refs에 넣습니다.
- 창 제목은 무엇을 보고 작업했는지를 보여줄 뿐, 끝냈다는 증거가 아닙니다. 파일 변경(C)이 있으면 '추가', '수정', '작성'처럼 쓰고, 창 제목에 보이는 사실(예: 'MR !312 등록')은 그대로 쓸 수 있습니다. '완료', '해결', '종료' 같은 단정은 쓰지 않고 '~ 작업', '~ 검토', '~ 분석'처럼 한 일로 씁니다. 끝났는지는 사용자가 보고서에서 직접 표시합니다.
- 한 일만 씁니다. 다음 계획은 사용자가 직접 쓰므로 제안하지 않습니다.
- 같은 일을 여러 날 했으면 한 항목으로 합치고 ref를 모두 넣습니다. 여러 날 자료에서는 파일 변경(C)의 date로 어느 날 무엇을 했는지 알 수 있습니다.
- 요약(summary)에도 같은 규칙을 적용합니다. '완료', '해결' 대신 '진행', '작업'으로 씁니다. 항목 끝에 '(진행 중)' 같은 상태를 붙이지 않습니다 (앱이 붙입니다).
- 한국어 업무 보고 문체로, 한 항목은 한 줄로 간결하게 씁니다 (명사형 종결)."""

@dataclass
class Item:
    text: str
    refs: list[str]
    flag: Optional[str] = None  # 'no_evidence': none of its references is a record that was sent


@dataclass
class ProjectBlock:
    """One group of work in the report. The model names the groups; there are no projects."""
    project: str
    seconds: float
    done: list[Item] = field(default_factory=list)
    missing_from_ai: bool = False


@dataclass
class Report:
    kind: str
    start: str
    end: str
    summary: str
    projects: list[ProjectBlock]
    offwork_s: float
    dropped_refs: int = 0
    outside_s: dict[str, float] = field(default_factory=dict)  # time outside the work folders: 웹 / 소통 / 기타
    notes: list[str] = field(default_factory=list)  # how the draft was made, when not in one request


def prompt(kind: str, ctx: Context) -> str:
    return (f"{ctx.start.isoformat()} ~ {ctx.end.isoformat()} 기간의 {KINDS[kind]} 초안을 작성하세요.\n\n"
            + dumps(ctx.payload))


# A week too big for one request is written day by day (each day from its own records, with
# SYSTEM), then the daily drafts are merged into the week: every reference still points at a
# record that was sent, and the merged draft is checked like any other.
MERGE_SYSTEM = """당신은 주간보고 초안을 작성합니다.
입력은 같은 주의 날짜별 업무일지 초안입니다. 각 항목은 업무 기록에서 나온 한 일(text)과 그 근거(refs)입니다.

- 날짜별 업무 묶음 중 같은 일(같은 작업 폴더, 같은 이슈 번호나 주제)을 업무 하나로 합칩니다. 같은 업무를 여러 묶음으로 나누지 않습니다.
- 업무 이름(project)은 날짜별 이름 중 가장 맞는 것을 고르거나 그 표현을 합쳐 짓습니다. 입력에 없는 이름이나 번호를 지어내지 않습니다.
- 여러 날에 걸친 같은 일은 한 항목으로 합치고, 합친 항목들의 refs를 모두 넣습니다. 입력에 없는 내용, 결과, 수치를 더하지 않습니다.
- refs는 입력 항목의 refs만 씁니다. 입력에 있는 refs는 하나도 빠짐없이 결과의 어느 항목에 넣습니다 (업무별 시간 계산에 씁니다).
- '완료', '해결', '종료' 같은 단정은 쓰지 않고 한 일로 씁니다. 다음 계획은 쓰지 않습니다.
- 요약(summary)은 주 전체를 1~2문장으로 씁니다.
- 업무 묶음은 일을 많이 한 순서로, 한국어 업무 보고 문체로 한 항목은 한 줄로 씁니다 (명사형 종결)."""


def merge_prompt(start: str, end: str, days: list["Report"]) -> str:
    """The daily drafts as the merge step's input: groups and lines with their references."""
    data = {"period": {"from": start, "to": end}, "days": [
        {"date": r.start, "summary": r.summary,
         "work": [{"project": b.project, "done": [{"text": it.text, "refs": it.refs} for it in b.done]}
                  for b in r.projects if b.done]}
        for r in days]}
    return (f"{start} ~ {end} 주간보고 초안을 날짜별 업무일지 초안으로 작성하세요.\n\n"
            + json.dumps(data, ensure_ascii=False, indent=1))


def _items(raw: Any, valid: set[str]) -> tuple[list[Item], int]:
    items, dropped = [], 0
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        given = [str(r).strip() for r in entry.get("refs") or [] if str(r).strip()]
        refs = list(dict.fromkeys(r for r in given if r in valid))  # verified, de-duplicated
        dropped += len(given) - len([r for r in given if r in valid])
        items.append(Item(text, refs, None if refs else "no_evidence"))
    return items, dropped


def validate(kind: str, ctx: Context, raw: Any) -> Report:
    """Turn the model's JSON into a Report whose references are all real."""
    if not isinstance(raw, dict):
        raw = {}
    blocks: dict[str, ProjectBlock] = {}
    dropped = 0
    for entry in raw.get("projects") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("project") or "기타").strip() or "기타"
        block = blocks.setdefault(name, ProjectBlock(name, 0.0))
        items, d = _items(entry.get("done"), ctx.refs)  # anything else the model sends is ignored
        block.done.extend(items)
        dropped += d
    # Time of a group = the work folder records it cites, never counted twice. A record cited by
    # several groups (one folder, two pieces of work) is shared day by day: a day goes to the groups
    # that cite a file change of that folder on that day, a day without such evidence evenly.
    cited = {name: list(dict.fromkeys(r for it in b.done for r in it.refs)) for name, b in blocks.items()}
    groups_of: dict[str, list[str]] = {}
    for name, refs in cited.items():
        for r in refs:
            groups_of.setdefault(r, []).append(name)
    changed_on = {name: {(ctx.ref_folder.get(r), ctx.sources[r]["day"]) for r in refs
                         if r.startswith("C") and r in ctx.sources}
                  for name, refs in cited.items()}
    seconds = {name: 0.0 for name in blocks}
    for r, names in groups_of.items():
        if r not in ctx.work_seconds:
            continue
        if len(names) == 1:
            seconds[names[0]] += ctx.work_seconds[r]
            continue
        folder = ctx.ref_folder.get(r)
        for day, sec in ctx.work_days.get(r, {}).items():
            share = [n for n in names if (folder, day) in changed_on[n]] or names
            for n in share:
                seconds[n] += sec / len(share)
    for name, block in blocks.items():
        block.seconds = seconds[name]
    # Never let a work folder's time or file changes silently disappear from the report.
    for folder, refs in ctx.folder_refs.items():
        if not set(groups_of) & set(refs):
            name = f"{folder} (작업 폴더)"
            blocks.setdefault(name, ProjectBlock(name, ctx.folder_seconds.get(folder, 0.0), missing_from_ai=True))
    ordered = sorted(blocks.values(), key=lambda b: (b.missing_from_ai, b.project == "기타", -b.seconds))
    summary = str(raw.get("summary") or "").strip()
    return Report(kind, ctx.start.isoformat(), ctx.end.isoformat(), summary, ordered, ctx.offwork_s, dropped,
                  dict(ctx.outside_s))


FLAG_TEXT = {"no_evidence": " (근거 없음 — 확인 필요)"}


def render_text(report: Report) -> str:
    """Plain text the user edits and pastes into the company report."""
    done_label, next_label = SECTION_LABELS[report.kind]
    period = report.start if report.start == report.end else f"{report.start} ~ {report.end}"
    lines = [f"# {KINDS[report.kind]} · {period}", ""]
    if report.summary:
        lines += [f"요약: {report.summary}", ""]
    for b in report.projects:
        lines.append(f"## {b.project}" + (f" ({duration(b.seconds)})" if b.seconds else ""))
        if b.missing_from_ai:
            lines += ["- (이 작업 폴더의 기록이 AI 초안에 빠졌습니다. 직접 작성하세요.)", ""]
            continue
        lines.append(f"[{done_label}]")
        if not b.done:
            lines.append("- 없음")
        for it in b.done:
            # What was done starts as 진행 중; the user marks it 완료 (a click on the saved report).
            lines.append(f"- {STATUS_SUFFIX.sub('', it.text)}{FLAG_TEXT.get(it.flag or '', '')} ({IN_PROGRESS})")
        lines += [f"[{next_label}]", "- ", ""]  # the user's plans
    outside = [f"{name} {duration(sec)}" for name, sec in report.outside_s.items() if sec >= 60]
    if outside:
        lines += [f"(작업 폴더 밖: {' · '.join(outside)})"]
    if report.offwork_s >= 60:
        lines += [f"(보고서에서 제외한 업무 외 시간: {duration(report.offwork_s)})"]
    if outside or report.offwork_s >= 60:
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _cited(report: Report) -> list[str]:
    return list(dict.fromkeys(r for b in report.projects for it in b.done for r in it.refs))


def to_dict(report: Report, ctx: Context) -> dict:
    """API shape: structured report plus where each reference points."""
    def item(it: Item) -> dict:
        return {"text": it.text, "refs": it.refs, "flag": it.flag}
    return {
        "kind": report.kind, "start": report.start, "end": report.end, "summary": report.summary,
        "projects": [{"project": b.project, "seconds": b.seconds, "missing_from_ai": b.missing_from_ai,
                      "done": [item(i) for i in b.done]} for b in report.projects],
        "offwork_s": report.offwork_s,
        "outside_s": report.outside_s,
        "dropped_refs": report.dropped_refs,
        "notes": report.notes,
        "sources": {ref: ctx.sources[ref] for ref in _cited(report) if ref in ctx.sources},
        "text": render_text(report),
    }
