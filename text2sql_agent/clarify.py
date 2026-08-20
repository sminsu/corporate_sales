"""Mid-run clarification questions the agent asks instead of guessing.

The workflow already pauses when a required value is missing.  What it used to
hand the user was a bare form field: a parameter name and an empty text box.
This module turns each of those pauses into a real question that carries its
own answer options, plus the reason the agent stopped there.

Every option is built deterministically — from the business calendar, the
semantic layer's value semantics, or the routing candidates the agent just
scored.  The small in-house model never invents the choices, and an answer that
matches one of them is bound back without an LLM call at all.  Free-text
answers keep working exactly as before.

The serialized shape is a superset of the legacy ``missing_params`` entry
(``name``/``label``/``description``/``type``), so older clients keep rendering
a text field while new ones render the options.
"""

from __future__ import annotations

import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable

from .time_policy import kst_today


KIND_CHOICE = "choice"
KIND_TEXT = "text"

# Where the answer is applied once it comes back.
APPLY_PARAM = "param"
APPLY_DOMAIN = "domain"

_COMPACT_RE = re.compile(r"[^0-9A-Za-z가-힣]")
_INDEX_RE = re.compile(r"^\s*(\d{1,2})\s*(?:번|번째|\.|\))?\s*$")


def _compact(text: Any) -> str:
    return _COMPACT_RE.sub("", str(text or "")).lower()


@dataclass(frozen=True)
class Choice:
    """One selectable answer."""

    value: str
    label: str
    hint: str = ""
    # An imperative line handed to the SQL prompt when this option is chosen.
    directive: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"value": self.value, "label": self.label, "hint": self.hint}


@dataclass(frozen=True)
class Clarification:
    """A question the agent stops to ask, with its own answer options."""

    name: str
    question: str
    kind: str = KIND_CHOICE
    reason: str = ""
    choices: tuple[Choice, ...] = ()
    allow_free_text: bool = True
    free_text_label: str = "직접 입력"
    free_text_hint: str = ""
    type: str = "string"
    applies_to: str = APPLY_PARAM
    description: str = ""
    examples: tuple[str, ...] = ()

    def to_param(self) -> dict[str, Any]:
        """Serialize as a ``missing_params`` entry, legacy keys included."""
        return {
            # --- legacy contract: every existing client reads these four ---
            "name": self.name,
            "label": self.question,
            "description": self.description or self.reason,
            "type": self.type,
            # --- interactive contract ---
            "kind": self.kind if self.choices else KIND_TEXT,
            "question": self.question,
            "reason": self.reason,
            "options": [choice.to_dict() for choice in self.choices],
            "allow_free_text": bool(self.allow_free_text or not self.choices),
            "free_text_label": self.free_text_label,
            "free_text_hint": self.free_text_hint,
            "applies_to": self.applies_to,
            "examples": list(self.examples),
            "directives": {choice.value: choice.directive for choice in self.choices if choice.directive},
        }


def to_params(clarifications: Iterable[Clarification]) -> list[dict[str, Any]]:
    return [item.to_param() for item in clarifications]


# ---------------------------------------------------------------------------
# Answer binding — deterministic, no model call
# ---------------------------------------------------------------------------

def options_of(param: Any) -> list[dict[str, Any]]:
    if not isinstance(param, dict):
        return []
    return [option for option in (param.get("options") or []) if isinstance(option, dict)]


def resolve_answer(param: Any, raw: Any) -> str:
    """Bind a user reply to one of the param's options, or return "".

    Matching is exact value, exact label, compacted label, or the option's
    1-based position ("2", "2번").  Anything else is free text and is left for
    the existing rule/LLM parsing path.
    """
    options = options_of(param)
    answer = str(raw or "").strip()
    if not options or not answer:
        return ""
    compact = _compact(answer)
    for option in options:
        if answer == str(option.get("value") or ""):
            return str(option.get("value") or "")
    for option in options:
        if compact and compact == _compact(option.get("label")):
            return str(option.get("value") or "")
    for option in options:
        value = str(option.get("value") or "")
        if compact and value and compact == _compact(value):
            return value
    index_match = _INDEX_RE.match(answer)
    if index_match:
        index = int(index_match.group(1)) - 1
        # A bare number is only a pick when it cannot be the value itself
        # (기준년월 "202608" must never be read as an option index).
        if 0 <= index < len(options) and len(answer.strip()) <= 3:
            return str(options[index].get("value") or "")
    return ""


def bind_option_answers(missing_params: Iterable[Any], reply: Any) -> dict[str, str]:
    """Resolve a single natural reply against every option-bearing question."""
    bound: dict[str, str] = {}
    for param in missing_params or []:
        if not isinstance(param, dict):
            continue
        name = str(param.get("name") or "")
        if not name:
            continue
        value = resolve_answer(param, reply)
        if value:
            bound[name] = value
    return bound


def directive_for(param: Any, value: Any) -> str:
    if not isinstance(param, dict):
        return ""
    directives = param.get("directives")
    if isinstance(directives, dict):
        directive = str(directives.get(str(value)) or "")
        if directive:
            return directive
    for option in options_of(param):
        if str(option.get("value") or "") == str(value):
            label = str(option.get("label") or option.get("value") or "")
            return f"{param.get('question') or param.get('label') or param.get('name')} → {label}"
    return ""


def apply_answers(state: dict[str, Any], missing_params: Iterable[Any], answers: dict[str, Any]) -> dict[str, Any]:
    """Route each answered question to the part of the state it controls.

    Returns the parameter answers that still belong in ``user_provided_params``;
    routing answers (a chosen domain) are written straight onto the state so the
    resumed run re-enters with the user's decision already made.
    """
    remaining = dict(answers or {})
    directives = [str(item) for item in (state.get("clarification_directives") or []) if item]
    for param in missing_params or []:
        if not isinstance(param, dict):
            continue
        name = str(param.get("name") or "")
        if not name or name not in remaining:
            continue
        value = remaining[name]
        if value in (None, ""):
            continue
        directive = directive_for(param, value)
        if directive and directive not in directives:
            directives.append(directive)
        if str(param.get("applies_to") or APPLY_PARAM) == APPLY_DOMAIN:
            state["selected_domain"] = str(value)
            # The stored context belongs to the domain the agent had guessed.
            state["domain_context"] = ""
            trace = str(state.get("domain_routing_trace") or "")
            state["domain_routing_trace"] = f"{trace}\nuser_selected_domain={value}".strip()
            remaining.pop(name, None)
    if directives:
        state["clarification_directives"] = directives
    return remaining


def directives_prompt(directives: Iterable[Any]) -> str:
    lines = [f"- {item}" for item in directives or [] if item]
    if not lines:
        return ""
    return (
        "\n## 사용자가 확정한 해석\n"
        + "\n".join(lines)
        + "\n위 해석은 사용자가 직접 선택한 값이므로 다르게 해석하지 마세요.\n"
    )


# ---------------------------------------------------------------------------
# Period questions
# ---------------------------------------------------------------------------

_TIME_PARAM_RE = re.compile(r"(?:년월|기간|시작|종료|기준일|기준년|날짜)")


def _ym(year: int, month: int) -> str:
    while month <= 0:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    return f"{year:04d}{month:02d}"


def _shift_ym(today: date, months: int) -> str:
    return _ym(today.year, today.month + months)


def _month_end(yyyymm: str) -> str:
    year, month = int(yyyymm[:4]), int(yyyymm[4:6])
    return f"{yyyymm}{monthrange(year, month)[1]:02d}"


def period_choices(name: str, *, today: date | None = None) -> tuple[Choice, ...]:
    """Concrete date options for a required period parameter.

    The label carries the business wording and the value carries the literal
    the SQL template needs, so the user never has to remember YYYYMM.
    """
    today = today or kst_today()
    compact = _compact(name)
    if "기간시작" in compact or compact.endswith("시작"):
        return (
            Choice(_shift_ym(today, -2), "최근 3개월 시작", f"{_shift_ym(today, -2)} ~ {_shift_ym(today, 0)}"),
            Choice(_shift_ym(today, -5), "최근 6개월 시작", f"{_shift_ym(today, -5)} ~ {_shift_ym(today, 0)}"),
            Choice(_ym(today.year, 1), "올해 1월", "연초 누적 기준"),
            Choice(_ym(today.year - 1, 1), "작년 1월", "전년 누적 기준"),
        )
    if "기간종료" in compact or compact.endswith("종료"):
        return (
            Choice(_shift_ym(today, 0), "이번 달", "실행 시점 기준월"),
            Choice(_shift_ym(today, -1), "전월", "마감된 직전 월"),
            Choice(_ym(today.year - 1, 12), "작년 12월", "전년 말 기준"),
        )
    if "기준년월일" in compact or "기준일" in compact or "날짜" in compact:
        return (
            Choice(_month_end(_shift_ym(today, -1)), "전월 말일", "월 마감 기준"),
            Choice(f"{today.year}{today.month:02d}{today.day:02d}", "오늘", "실행 시점"),
            Choice(_month_end(_shift_ym(today, 0)), "이번 달 말일", "당월 말 기준"),
        )
    if "기준년월" in compact or compact.endswith("년월") or "월" in compact:
        return (
            Choice(_shift_ym(today, -1), "전월", "마감된 직전 월"),
            Choice(_shift_ym(today, 0), "이번 달", "실행 시점 기준월"),
            Choice(_shift_ym(today, -2), "2개월 전", f"{_shift_ym(today, -2)}"),
            Choice(_ym(today.year - 1, today.month), "작년 동월", "전년 동월 비교 기준"),
        )
    if "기준년" in compact or compact.endswith("년") or "연도" in compact:
        return (
            Choice(str(today.year), "올해", "실행 시점 연도"),
            Choice(str(today.year - 1), "작년", "직전 연도"),
        )
    return ()


def _period_hint(name: str) -> str:
    compact = _compact(name)
    if "기준년월일" in compact or "기준일" in compact or "날짜" in compact:
        return "예: 2026년 3월 15일 / 20260315"
    if "년월" in compact or "기간" in compact:
        return "예: 2026년 3월 / 202603"
    if "년" in compact or "연도" in compact:
        return "예: 2026"
    return ""


def is_period_param(name: str) -> bool:
    return bool(_TIME_PARAM_RE.search(str(name or "")))


def period_clarification(
    name: str,
    *,
    description: str = "",
    param_type: str = "string",
    today: date | None = None,
) -> Clarification:
    choices = period_choices(name, today=today)
    return Clarification(
        name=name,
        question=f"{name}을(를) 어느 기준으로 조회할까요?",
        reason=description or "질문에 기간이 드러나지 않아 조회 기준을 확정할 수 없습니다.",
        choices=choices,
        type=param_type,
        free_text_hint=_period_hint(name),
        description=description,
    )


def value_clarification(
    name: str,
    *,
    description: str = "",
    param_type: str = "string",
) -> Clarification:
    """A required value with no deterministic option set — ask as free text."""
    return Clarification(
        name=name,
        question=f"{name} 값을 입력해주세요.",
        kind=KIND_TEXT,
        reason=description or "조회에 반드시 필요한 값이라 임의로 채우지 않았습니다.",
        choices=(),
        type=param_type,
        description=description,
    )


def upgrade_param(param: Any, *, today: date | None = None) -> dict[str, Any]:
    """Give a legacy ``missing_params`` entry a question and options."""
    if not isinstance(param, dict):
        return value_clarification(str(param or "")).to_param()
    if param.get("options") or param.get("kind"):
        return dict(param)
    name = str(param.get("name") or param.get("label") or "")
    description = str(param.get("description") or "")
    param_type = str(param.get("type") or "string")
    if is_period_param(name) and period_choices(name, today=today):
        upgraded = period_clarification(
            name, description=description, param_type=param_type, today=today
        ).to_param()
    else:
        upgraded = value_clarification(
            name, description=description, param_type=param_type
        ).to_param()
    # A caller-supplied label is more specific than the generated question.
    label = str(param.get("label") or "")
    if label and label != name:
        upgraded["label"] = label
        upgraded["question"] = label
    return upgraded


def upgrade_params(params: Iterable[Any], *, today: date | None = None) -> list[dict[str, Any]]:
    return [upgrade_param(param, today=today) for param in params or []]


# ---------------------------------------------------------------------------
# Routing questions
# ---------------------------------------------------------------------------

def classification_axis_clarification(label: str, attributes: list[dict[str, Any]]) -> Clarification:
    """Ask which classification axis a value label belongs to.

    "기타" exists as a code in several attributes; picking one silently is the
    difference between an 업종 filter and a 카드구분 filter.
    """
    choices = tuple(
        Choice(
            value=str(attribute.get("korean_name") or attribute.get("name") or ""),
            label=str(attribute.get("korean_name") or attribute.get("name") or ""),
            hint=str(attribute.get("name") or ""),
            directive=f"'{label}'은(는) {attribute.get('korean_name') or attribute.get('name')} 기준 코드값으로 해석한다.",
        )
        for attribute in attributes
        if attribute.get("korean_name") or attribute.get("name")
    )
    return Clarification(
        name=f"{label}분류축",
        question=f"'{label}'은(는) 어느 분류 기준의 값인가요?",
        reason=f"'{label}' 라벨이 여러 분류 축에 동시에 존재해 필터 대상을 확정할 수 없습니다.",
        choices=choices,
        allow_free_text=False,
    )


def target_name_clarification() -> Clarification:
    return Clarification(
        name="대상명",
        question="어떤 대상을 조회할까요? 기업명·가맹점명·고객명을 알려주세요.",
        kind=KIND_TEXT,
        reason="질문이 특정 대상을 가리키는데 이름이 없어 전체 조회로 넓히지 않았습니다.",
        choices=(),
        free_text_hint="예: 케이비카드 / 우리동네마트 / 123-45-67890",
        examples=("케이비카드", "우리동네마트"),
        description="조회 대상명(기업명/가맹점명/고객명)",
    )


def domain_clarification(candidates: list[dict[str, Any]], *, top_k: int = 3) -> Clarification | None:
    """Ask which business domain to route to when the scores are a near tie."""
    named = [
        candidate
        for candidate in candidates or []
        if isinstance(candidate, dict) and str(candidate.get("domain") or "")
    ]
    if len(named) < 2:
        return None
    choices = tuple(
        Choice(
            value=str(candidate.get("domain") or ""),
            label=str(candidate.get("domain") or ""),
            hint=_domain_hint(candidate),
            directive=f"조회 도메인은 {candidate.get('domain')}으로 확정한다.",
        )
        for candidate in named[:top_k]
    )
    return Clarification(
        name="조회도메인",
        question="어느 업무 영역의 데이터로 조회할까요?",
        reason=(
            "라우팅 점수가 "
            + " · ".join(f"{choice.label}" for choice in choices)
            + " 사이에서 비슷해 데이터 출처를 단정할 수 없습니다."
        ),
        choices=choices,
        allow_free_text=False,
        applies_to=APPLY_DOMAIN,
    )


def _domain_hint(candidate: dict[str, Any]) -> str:
    try:
        score = float(candidate.get("score"))
    except (TypeError, ValueError):
        return ""
    return f"라우팅 점수 {score:.2f}"


def needs_domain_clarification(candidates: list[dict[str, Any]], *, margin_ratio: float, min_score: float) -> bool:
    """True when the top two domains are too close to pick without asking."""
    named = [
        candidate
        for candidate in candidates or []
        if isinstance(candidate, dict) and str(candidate.get("domain") or "")
    ]
    if len(named) < 2:
        return False
    try:
        top = float(named[0].get("score"))
        second = float(named[1].get("score"))
    except (TypeError, ValueError):
        return False
    if top <= 0:
        return False
    # A confident top score is routed without a question even if the runner-up
    # is close; only genuinely weak, tied routing is worth a turn.
    if top >= min_score:
        return False
    return (top - second) <= top * margin_ratio
