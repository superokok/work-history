"""AI work report drafts (daily 업무일지 / weekly 주간보고) with verified citations."""

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from .ai_context import Context
from .review import duration

KINDS = {"daily": "업무일지", "weekly": "주간보고"}
SECTION_LABELS = {
    "daily": ("오늘 한 일", "다음 할 일", "이슈"),
    "weekly": ("금주 실적", "차주 계획", "이슈"),
}
# A project with at least this much recorded work must appear in the report.
MIN_PROJECT_S_FOR_REPORT = 15 * 60

_ITEM = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "보고서 한 줄 (개조식, 명사형 종결)"},
        "refs": {"type": "array", "items": {"type": "string"},
                 "description": "이 항목의 근거가 된 자료의 ref 목록 (예: S12, N3, P1)"},
    },
    "required": ["text", "refs"],
}

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "기간 전체를 1~2문장으로 요약"},
        "projects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "자료에 있는 프로젝트 이름 그대로"},
                    "done": {"type": "array", "items": _ITEM, "description": "한 일 (실적)"},
                    "next": {"type": "array", "items": _ITEM, "description": "다음 계획"},
                    "issues": {"type": "array", "items": _ITEM, "description": "이슈·막힌 점"},
                },
                "required": ["project", "done", "next", "issues"],
            },
        },
    },
    "required": ["summary", "projects"],
}

SYSTEM = """당신은 소프트웨어 개발자의 업무 보고서 초안을 작성합니다.
입력 JSON은 이 개발자의 업무 기록입니다: 작업 폴더에서 기록한 파일 변경(C), 컴퓨터 활동에서 자동으로 정리한 세션(S), 본인이 쓴 메모(N), 프로젝트와 본인이 적은 다음 할 일(P).

반드시 지킬 것:
- 입력 자료에 있는 사실만 씁니다. 자료에 없는 작업, 결과, 수치, 원인을 지어내지 않습니다.
- 모든 항목의 refs에는 그 항목의 근거가 된 ref를 넣습니다.
- 파일 변경(C)은 실제로 만들거나 고친 결과물이므로 실적의 가장 좋은 근거입니다. diff_excerpt와 파일 이름, 같은 시간대의 세션과 메모를 함께 보고 무엇을 바꿨는지 업무 언어로 요약합니다. 코드나 경로를 길게 옮기지 말고 필요하면 클래스나 파일 이름만 괄호로 붙입니다.
- 세션 제목과 창 제목은 무엇을 보고 작업했는지를 보여줄 뿐, 끝냈다는 증거가 아닙니다. 파일 변경(C)이 있으면 '추가', '수정', '작성'처럼 쓸 수 있지만, '완료', '해결' 같은 단정은 본인의 메모(user_note, N)나 본인이 직접 쓴 세션 제목(title_by가 user)에 근거가 있을 때만 씁니다. 그 외에는 '~ 작업', '~ 검토', '~ 분석'처럼 활동으로 씁니다.
- 다음 계획(next)은 본인이 적은 다음 할 일(P의 user_next_action)이나 메모(N, user_note)에 근거가 있을 때만 쓰고, 그 ref를 넣습니다. 근거가 없으면 빈 배열로 둡니다.
- 이슈(issues)는 메모에 적힌 문제나 막힌 점만 씁니다. 없으면 빈 배열로 둡니다.
- 프로젝트별로 묶고, 프로젝트 이름은 자료에 있는 이름을 그대로 씁니다. 프로젝트가 없는 세션은 '미분류'로 묶습니다.
- 같은 일을 여러 세션에서 했으면 한 항목으로 합치고 ref를 모두 넣습니다.
- 요약(summary)에도 같은 규칙을 적용합니다. 항목에서 '완료', '해결'이라고 쓸 근거가 없는 일은 요약에서도 '진행', '작업'으로 씁니다.
- 한국어 업무 보고 문체로, 한 항목은 한 줄로 간결하게 씁니다.
- 메신저, 메일, 문서 열람처럼 업무 내용을 알 수 없는 짧은 활동은 실적으로 쓰지 않습니다."""


@dataclass
class Item:
    text: str
    refs: list[str]
    flag: Optional[str] = None  # None | 'no_evidence' | 'unconfirmed_plan'


@dataclass
class ProjectBlock:
    project: str
    seconds: float
    done: list[Item] = field(default_factory=list)
    next: list[Item] = field(default_factory=list)
    issues: list[Item] = field(default_factory=list)
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


def prompt(kind: str, ctx: Context) -> str:
    return (f"{ctx.start.isoformat()} ~ {ctx.end.isoformat()} 기간의 {KINDS[kind]} 초안을 작성하세요.\n\n"
            + json.dumps(ctx.payload, ensure_ascii=False, indent=1))


def _items(raw: Any, valid: set[str], section: str) -> tuple[list[Item], int]:
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
        flag = None
        if section == "next":
            # A plan must come from the user's own words (notes or next actions).
            if not any(r[0] in ("N", "P") for r in refs):
                flag = "unconfirmed_plan"
        elif not refs:
            flag = "no_evidence"
        items.append(Item(text, refs, flag))
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
        name = str(entry.get("project") or "미분류").strip() or "미분류"
        block = blocks.setdefault(name, ProjectBlock(name, ctx.project_seconds.get(name, 0.0)))
        for section in ("done", "next", "issues"):
            items, d = _items(entry.get(section), ctx.refs, section)
            getattr(block, section).extend(items)
            dropped += d
    # Never let a named project with real recorded work (time or file changes) silently disappear.
    # 미분류 (meetings, chat, unclassified browsing) may legitimately be left out of a report.
    for name in set(ctx.project_seconds) | set(ctx.project_changes):
        seconds = ctx.project_seconds.get(name, 0.0)
        if name == "미분류":
            continue
        if name not in blocks and (seconds >= MIN_PROJECT_S_FOR_REPORT or ctx.project_changes.get(name)):
            blocks[name] = ProjectBlock(name, seconds, missing_from_ai=True)
    ordered = sorted(blocks.values(), key=lambda b: (b.project == "미분류", -b.seconds))
    return Report(kind, ctx.start.isoformat(), ctx.end.isoformat(),
                  str(raw.get("summary") or "").strip(), ordered, ctx.offwork_s, dropped)


def render_text(report: Report) -> str:
    """Plain text the user edits and pastes into the company report."""
    done_label, next_label, issue_label = SECTION_LABELS[report.kind]
    period = report.start if report.start == report.end else f"{report.start} ~ {report.end}"
    lines = [f"# {KINDS[report.kind]} · {period}", ""]
    if report.summary:
        lines += [f"요약: {report.summary}", ""]
    for b in report.projects:
        lines.append(f"## {b.project}" + (f" ({duration(b.seconds)})" if b.seconds else ""))
        if b.missing_from_ai:
            lines += ["- (AI 초안에 항목이 없습니다. 기록된 작업이 있으니 직접 작성하세요.)", ""]
            continue
        for label, items in ((done_label, b.done), (next_label, b.next), (issue_label, b.issues)):
            lines.append(f"[{label}]")
            if not items:
                lines.append("- 없음")
            for it in items:
                suffix = {"no_evidence": " (근거 없음 — 확인 필요)",
                          "unconfirmed_plan": " (AI 제안 — 확인 필요)"}.get(it.flag or "", "")
                lines.append(f"- {it.text}{suffix}")
        lines.append("")
    if report.offwork_s >= 60:
        lines += [f"(보고서에서 제외한 업무 외 시간: {duration(report.offwork_s)})", ""]
    return "\n".join(lines).rstrip() + "\n"


def _cited(report: Report) -> list[str]:
    return list(dict.fromkeys(r for b in report.projects for items in (b.done, b.next, b.issues)
                              for it in items for r in it.refs))


def to_dict(report: Report, ctx: Context) -> dict:
    """API shape: structured report plus where each reference points."""
    def item(it: Item) -> dict:
        return {"text": it.text, "refs": it.refs, "flag": it.flag}
    return {
        "kind": report.kind, "start": report.start, "end": report.end, "summary": report.summary,
        "projects": [{"project": b.project, "seconds": b.seconds, "missing_from_ai": b.missing_from_ai,
                      "done": [item(i) for i in b.done], "next": [item(i) for i in b.next],
                      "issues": [item(i) for i in b.issues]} for b in report.projects],
        "offwork_s": report.offwork_s,
        "dropped_refs": report.dropped_refs,
        "sources": {ref: ctx.sources[ref] for ref in _cited(report) if ref in ctx.sources},
        "text": render_text(report),
    }
