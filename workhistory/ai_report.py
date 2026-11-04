"""AI work report drafts (daily 업무일지 / weekly 주간보고) with verified citations."""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .ai_context import Context
from .sessions import duration

KINDS = {"daily": "업무일지", "weekly": "주간보고"}
SECTION_LABELS = {
    "daily": ("오늘 한 일", "다음 할 일", "이슈"),
    "weekly": ("금주 실적", "차주 계획", "이슈"),
}
# Words that say something is finished. File changes and sessions show work, not that it is done:
# only the user's own words (session titles they wrote) may back such a claim.
DONE_WORDS = re.compile(r"완료|종료|마무리|마쳤|끝냈|끝났|끝냄|완성(?!도)|"
                        r"해결(?!\s*(?:방안|방법|책|을\s*위|하기\s*위|해야|필요))|"
                        r"\b(?:resolved|closed|done)\b", re.I)

_ITEM = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "보고서 한 줄 (개조식, 명사형 종결)"},
        "refs": {"type": "array", "items": {"type": "string"},
                 "description": "이 항목의 근거가 된 자료의 ref 목록 (예: S12, C7, N3)"},
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
                    "next": {"type": "array", "items": _ITEM,
                             "description": "다음 계획: 기록에서 이어지는 다음 단계 제안"},
                    "issues": {"type": "array", "items": _ITEM,
                               "description": "이슈·막힌 점: 본인이 쓴 세션 제목에 적힌 것만, 없으면 빈 배열"},
                },
                "required": ["project", "done", "next", "issues"],
            },
        },
    },
    "required": ["summary", "projects"],
}

SYSTEM = """당신은 소프트웨어 개발자의 업무 보고서 초안을 작성합니다.
입력 JSON은 이 개발자의 업무 기록입니다: 작업 폴더에서 기록한 파일 변경(C)과 컴퓨터 활동(앱과 창 제목)에서 자동으로 정리한 세션(S). 프로젝트 분류나 메모는 없습니다. 일을 묶는 것은 당신이 합니다.

업무 묶기:
- 같은 일을 한 기록끼리 '업무' 하나로 묶습니다. 이슈 번호(예: GOV-1418), MR 번호(예: !312), 파일 변경의 작업 폴더(folder), 창 제목의 저장소·파일 이름처럼 기록에 드러난 단서로 묶습니다.
- 업무 이름(project)은 기록에 나온 이슈 번호나 저장소·주제를 써서 짧게 짓습니다 (예: 'GOV-1418 리프레시 토큰 재발급 버그', 'poc-k8s Helm 배포'). 기록에 없는 이름이나 회사 프로젝트명을 지어내지 않습니다.
- 어디에도 묶기 어려운 업무 활동은 '기타'로 모읍니다. 같은 업무를 여러 묶음으로 나누지 않습니다.
- 업무 묶음은 일을 많이 한 순서로 씁니다.

반드시 지킬 것:
- 입력 자료에 있는 사실만 씁니다. 자료에 없는 작업, 결과, 수치, 원인을 지어내지 않습니다.
- 모든 항목의 refs에는 그 항목의 근거가 된 ref를 넣습니다. 그 업무에 쓴 세션(S)도 빠짐없이 넣습니다 (업무별 시간 계산에 씁니다).
- 파일 변경(C)은 실제로 만들거나 고친 결과물이므로 실적의 가장 좋은 근거입니다. diff_excerpt와 파일 이름, 같은 시간대의 세션을 함께 보고 무엇을 바꿨는지 업무 언어로 요약합니다. 코드나 경로를 길게 옮기지 말고 필요하면 클래스나 파일 이름만 괄호로 붙입니다.
- 한 일(done)을 먼저 씁니다. 기간 안의 파일 변경(C)은 하나도 빠짐없이 어느 업무의 한 일(done) 항목 refs에 넣습니다. 고친 내용은 이슈가 아니라 한 일입니다.
- 세션 제목과 창 제목은 무엇을 보고 작업했는지를 보여줄 뿐, 끝냈다는 증거가 아닙니다. 파일 변경(C)이 있으면 '추가', '수정', '작성'처럼 쓰고, 창 제목에 보이는 사실(예: 'MR !312 등록')은 그대로 쓸 수 있습니다. '완료', '해결', '종료' 같은 단정은 본인이 직접 쓴 세션 제목(title_by가 user)에 그렇게 적혀 있을 때만 씁니다. 그 외에는 '~ 작업', '~ 검토', '~ 분석', '~ 진행'처럼 활동으로 씁니다.
- 다음 계획(next): 기록에서 자연스럽게 이어지는 다음 단계(예: 올린 MR의 리뷰 반영, 검토 중인 작업의 적용)를 업무마다 1~2개까지 제안하고 근거 ref를 넣습니다. 이런 제안은 사용자에게 'AI 제안'으로 표시됩니다. 기록과 관계없는 새 일을 지어내지 않습니다.
- 이슈(issues)는 빈 배열로 둡니다. 예외는 본인이 직접 쓴 세션 제목(title_by가 user)에 문제나 막힌 점이 적힌 경우뿐입니다. 고친 버그나 작업 주제는 이슈가 아닙니다.
- 같은 일을 여러 세션에서 했으면 한 항목으로 합치고 ref를 모두 넣습니다.
- 요약(summary)에도 같은 규칙을 적용합니다. 항목에서 '완료', '해결'이라고 쓸 근거가 없는 일은 요약에서도 '진행', '작업'으로 씁니다.
- 한국어 업무 보고 문체로, 한 항목은 한 줄로 간결하게 씁니다 (명사형 종결).
- 메신저, 메일, 회의, 문서 열람처럼 업무 내용을 알 수 없는 짧은 활동은 실적으로 쓰지 않습니다."""


@dataclass
class Item:
    text: str
    refs: list[str]
    flag: Optional[str] = None  # None | 'no_evidence' | 'unconfirmed_plan' | 'unconfirmed_done'


@dataclass
class ProjectBlock:
    """One group of work in the report. The model names the groups; there are no projects."""
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
    summary_flag: Optional[str] = None  # 'unconfirmed_done' when the summary calls unbacked work finished


def prompt(kind: str, ctx: Context) -> str:
    return (f"{ctx.start.isoformat()} ~ {ctx.end.isoformat()} 기간의 {KINDS[kind]} 초안을 작성하세요.\n\n"
            + json.dumps(ctx.payload, ensure_ascii=False, indent=1))


ISSUE_KEY = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")  # GOV-1423, JIRA-12
_SENTENCE_END = re.compile(r"(?<=[.!?。])\s+|\n+|;\s*")


def claims_done(text: str) -> bool:
    return bool(DONE_WORDS.search(text))


def _sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_END.split(text) if s.strip()]


def _done_backed(text: str, words: list[str]) -> bool:
    """The user's own words (`words`) say that what `text` claims is finished.
    With issue keys in the claim, each key needs a sentence of the user's own that names it and says
    it is done: one note "GOV-1418 완료. GOV-1423 응답 3.2초" does not make GOV-1423 finished."""
    said_done = [s for w in words for s in _sentences(w) if claims_done(s)]
    keys = set(ISSUE_KEY.findall(text))
    if not keys:
        return bool(said_done)
    return all(any(key in s for s in said_done) for key in keys)


def _summary_backed(summary: str, user_words: dict[str, str]) -> bool:
    """Every finished claim in the summary (which has no references) is backed by some user's words."""
    words = list(user_words.values())
    for sentence in _sentences(summary):
        for m in DONE_WORDS.finditer(sentence):
            if not _done_backed(sentence[:m.end()], words):  # what the sentence calls done up to that word
                return False
    return True


def _items(raw: Any, valid: set[str], section: str,
           user_words: Optional[dict[str, str]] = None) -> tuple[list[Item], int]:
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
            # Plans are not in the records: every one is the AI's suggestion, unless a title the user
            # wrote says so.
            if not any(r in (user_words or {}) for r in refs):
                flag = "unconfirmed_plan"
        elif not refs:
            flag = "no_evidence"
        elif section == "done" and claims_done(text) and not _done_backed(
                text, [(user_words or {}).get(r, "") for r in refs]):
            flag = "unconfirmed_done"
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
        name = str(entry.get("project") or "기타").strip() or "기타"
        block = blocks.setdefault(name, ProjectBlock(name, 0.0))
        for section in ("done", "next", "issues"):
            items, d = _items(entry.get(section), ctx.refs, section, ctx.user_words)
            getattr(block, section).extend(items)
            dropped += d
    for block in blocks.values():
        # Time of a group = the sessions it cites (each counted once).
        cited = {r for items in (block.done, block.next, block.issues) for it in items for r in it.refs}
        block.seconds = sum(ctx.session_seconds.get(r, 0.0) for r in cited)
    # Never let a work folder's recorded file changes silently disappear from the report.
    cited_all = {r for b in blocks.values() for items in (b.done, b.next, b.issues) for it in items for r in it.refs}
    for folder, refs in ctx.folder_changes.items():
        if not cited_all.intersection(refs):
            blocks.setdefault(f"{folder} (작업 폴더)", ProjectBlock(f"{folder} (작업 폴더)", 0.0, missing_from_ai=True))
    ordered = sorted(blocks.values(), key=lambda b: (b.missing_from_ai, b.project == "기타", -b.seconds))
    summary = str(raw.get("summary") or "").strip()
    summary_flag = None if _summary_backed(summary, ctx.user_words) else "unconfirmed_done"
    return Report(kind, ctx.start.isoformat(), ctx.end.isoformat(), summary, ordered, ctx.offwork_s, dropped,
                  summary_flag)


def render_text(report: Report) -> str:
    """Plain text the user edits and pastes into the company report."""
    done_label, next_label, issue_label = SECTION_LABELS[report.kind]
    period = report.start if report.start == report.end else f"{report.start} ~ {report.end}"
    lines = [f"# {KINDS[report.kind]} · {period}", ""]
    if report.summary:
        flag = " (완료 근거 없음 — 확인 필요)" if report.summary_flag else ""
        lines += [f"요약: {report.summary}{flag}", ""]
    for b in report.projects:
        lines.append(f"## {b.project}" + (f" ({duration(b.seconds)})" if b.seconds else ""))
        if b.missing_from_ai:
            lines += ["- (이 작업 폴더의 파일 변경이 AI 초안에 빠졌습니다. 직접 작성하세요.)", ""]
            continue
        for label, items in ((done_label, b.done), (next_label, b.next), (issue_label, b.issues)):
            lines.append(f"[{label}]")
            if not items:
                lines.append("- 없음")
            for it in items:
                suffix = {"no_evidence": " (근거 없음 — 확인 필요)",
                          "unconfirmed_plan": " (AI 제안 — 확인 필요)",
                          "unconfirmed_done": " (완료 근거 없음 — 확인 필요)"}.get(it.flag or "", "")
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
        "summary_flag": report.summary_flag,
        "projects": [{"project": b.project, "seconds": b.seconds, "missing_from_ai": b.missing_from_ai,
                      "done": [item(i) for i in b.done], "next": [item(i) for i in b.next],
                      "issues": [item(i) for i in b.issues]} for b in report.projects],
        "offwork_s": report.offwork_s,
        "dropped_refs": report.dropped_refs,
        "sources": {ref: ctx.sources[ref] for ref in _cited(report) if ref in ctx.sources},
        "text": render_text(report),
    }
