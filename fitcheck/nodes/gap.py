"""보완 포인트 노드 (TASKS T10, AGENT_DESIGN 7장)

코드가 계산한 라벨, 적합도, 부족 요건을 LLM이 2문장 이내로 풀어 쓴다. LLM은 해석만 한다 (DESIGN_DECISIONS D1).

재검사 (코드). 하나라도 어기면 LLM 문장을 버리고 코드가 만든 기본 문장을 쓴다.
- 숫자: 넘겨준 계산 결과에 없는 숫자가 나오면 안 된다 (42%를 45%로, '6주면' 같은 지어낸 기간)
- 라벨: 지금 라벨 말고 다른 라벨이 나오면 안 된다 ('불합격권' 안의 '합격권'은 같은 라벨로 본다)
- 예측: '합격 가능성', '확률', '떨어질' 같은 표현이 나오면 안 된다 (라벨은 합격 확률이 아니다)
- 길이: 2문장 이내, 비어 있으면 안 된다

LLM을 부르지 않는 경우
- 계산할 요건이 없을 때, 보완할 것(보류 조건, 부족하거나 약한 요건)이 없을 때 → 기본 문장

보완 포인트는 보조 정보다. LLM 오류나 호출 상한 초과가 나도 분석을 멈추지 않고 기본 문장을 쓴다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from ..budget import BudgetExceeded, LLMBudget
from ..llm import BaseLLM, LLMError, Purpose
from ..schemas import EligibilityResult, FitBand, GapReport, ScoreItem
from ..scoring import percent

PROMPT_VERSION = "gap-v1"
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "summarize_gap.md"

MAX_SENTENCES = 2
MAX_CHARS = 300
WEAK_CREDIT = 0.6  # 이보다 낮으면 근거가 약한 요건 (scoring._met의 '충족' 기준과 같다)
TOP_N = 2

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_SENTENCE_END = re.compile(r"[.!?。](?=\s|$)")
# 라벨은 합격 확률이 아니다 (schemas.FitBand). '불합격권'은 걸리지 않게 '합격' 뒤에 가능, 확률만 본다
_PREDICTION = re.compile(r"합격\s*(?:가능|확률)|확률|붙을|떨어질")
# 긴 라벨부터 지워야 '불합격권' 안의 '합격권'을 따로 세지 않는다
_LABELS = sorted((b.value for b in FitBand), key=len, reverse=True)


class SummaryOut(BaseModel):
    summary: str


@dataclass
class GapSummary:
    text: str
    source: Literal["llm", "rule"]
    reason: str = ""  # 기본 문장을 쓴 이유 (디버깅, 평가용)


# ──────────────────────────────────────────────
# 코드가 고르는 보완 대상
# ──────────────────────────────────────────────


def _holds(e: EligibilityResult) -> list[str]:
    return [f"{c.kind} {c.label}" + (f" ({c.detail})" if c.detail else "") for c in e.checks if c.result == "hold"]


def _by_weight(items: list[ScoreItem]) -> list[ScoreItem]:
    """가중치가 큰 것(필수, 핵심)부터. 같은 가중치면 공고 순서"""
    return sorted(items, key=lambda i: -i.weight)


def _missing_required(g: GapReport) -> list[ScoreItem]:
    return _by_weight([i for i in g.breakdown if i.is_required and i.credit == 0])


def _weak(g: GapReport) -> list[ScoreItem]:
    return _by_weight([i for i in g.breakdown if 0 < i.credit < WEAK_CREDIT])


def _missing_preferred(g: GapReport) -> list[ScoreItem]:
    return [i for i in g.breakdown if not i.is_required and i.credit == 0]


def _has_work(g: GapReport, e: EligibilityResult) -> bool:
    return bool(_holds(e) or _missing_required(g) or _weak(g) or _missing_preferred(g))


# ──────────────────────────────────────────────
# 기본 문장 (코드)
# ──────────────────────────────────────────────


def _headline(g: GapReport, band: FitBand) -> str:
    if g.fit_score is None:
        return f"{band.value}, 공고에 비교할 요건이 없어 적합도는 계산하지 않았어요."
    done, total = g.required_met
    met = f", 필수 {done}/{total}개 충족" if total else ""
    return f"{band.value}, 적합도 {percent(g.fit_score)}{met}."


def _to_fill(g: GapReport) -> list[str]:
    """근거가 없는 필수 요건, 그다음 근거가 약한 요건. 각각 중요한 순"""
    return [i.text for i in _missing_required(g)] + [f"{i.text}({i.strength})" for i in _weak(g)]


def default_summary(g: GapReport, band: FitBand, e: EligibilityResult) -> str:
    head = _headline(g, band)
    holds = _holds(e)
    if holds:
        return f"{head} 먼저 확인할 조건: {', '.join(holds[:TOP_N])}."
    fill = _to_fill(g)
    if fill:
        return f"{head} 먼저 보완할 요건: {', '.join(fill[:TOP_N])}."
    pref = _missing_preferred(g)
    if pref:
        return f"{head} 남은 우대 요건: {', '.join(i.text for i in pref[:TOP_N])}."
    if g.fit_score is None:
        return head
    return f"{head} 필수 요건을 모두 프로젝트 근거로 채웠어요."


# ──────────────────────────────────────────────
# LLM 입력과 재검사
# ──────────────────────────────────────────────


def _facts(g: GapReport, band: FitBand, e: EligibilityResult) -> str:
    lines = [f"라벨: {band.value}"]
    if g.fit_score is not None:
        lines.append(f"적합도: {percent(g.fit_score)}")
    if g.required_met[1]:
        lines.append(f"필수 요건 충족: {g.required_met[0]}/{g.required_met[1]}")
    if g.preferred_met[1]:
        lines.append(f"우대 요건 충족: {g.preferred_met[0]}/{g.preferred_met[1]}")
    for title, values in (
        ("확인이 필요한 조건", _holds(e)),
        ("근거가 없는 필수 요건 (중요한 순)", [i.text for i in _missing_required(g)]),
        ("근거가 약한 요건", [f"{i.text} ({i.strength})" for i in _weak(g)]),
        ("근거가 없는 우대 요건", [i.text for i in _missing_preferred(g)]),
    ):
        if values:
            lines.append(f"{title}: " + "; ".join(values))
    return "\n".join(lines)


def _numbers(text: str) -> set[str]:
    return set(_NUMBER.findall(text))


def _labels(text: str) -> set[str]:
    found = set()
    for label in _LABELS:
        if label in text:
            found.add(label)
            text = text.replace(label, " ")
    return found


def _sentences(text: str) -> int:
    body = text.strip()
    ends = len(_SENTENCE_END.findall(body))
    return ends + (0 if _SENTENCE_END.search(body[-1:] + " ") else 1)


def check_summary(text: str, facts: str, band: FitBand) -> str:
    """문제가 없으면 빈 문자열, 있으면 이유"""
    t = text.strip()
    if not t:
        return "빈 문장"
    if len(t) > MAX_CHARS:
        return f"{MAX_CHARS}자 초과"
    extra = _numbers(t) - _numbers(facts)
    if extra:
        return f"넘겨주지 않은 숫자: {', '.join(sorted(extra))}"
    if _PREDICTION.search(t):
        return "합격 예측 표현"
    other = _labels(t) - {band.value}
    if other:
        return f"다른 라벨: {', '.join(sorted(other))}"
    if _sentences(t) > MAX_SENTENCES:
        return f"{MAX_SENTENCES}문장 초과"
    return ""


def summarize_gap(
    g: GapReport,
    band: FitBand,
    e: EligibilityResult,
    *,
    llm: BaseLLM,
    budget: LLMBudget | None = None,
    purpose: Purpose = "new_analysis",
) -> GapSummary:
    fallback = default_summary(g, band, e)
    if g.fit_score is None and not _holds(e):
        return GapSummary(fallback, "rule", "계산할 요건 없음")
    if not _has_work(g, e):
        return GapSummary(fallback, "rule", "보완할 것 없음")

    facts = _facts(g, band, e)
    prompt = PROMPT_PATH.read_text(encoding="utf-8").replace("{facts}", facts)
    try:
        out = llm.structured(prompt, SummaryOut, purpose=purpose, budget=budget)
    except (LLMError, BudgetExceeded) as err:
        return GapSummary(fallback, "rule", f"LLM 실패: {err}")

    text = out.summary.strip()
    problem = check_summary(text, facts, band)
    if problem:
        return GapSummary(fallback, "rule", problem)
    return GapSummary(text, "llm")
