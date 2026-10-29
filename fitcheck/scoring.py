"""적합도 점수 — 규칙 기반 가중 평균

적합도 = Σ(가중치 × 충족 정도) / Σ(가중치)

충족 정도 (요건마다 5단계)
  직접 구현 1.0 · 팀에서 담당 0.85 · 사용 경험 0.6 · 조금 만져봄 0.3 · 없음 0
  근거 프로젝트가 2개 이상이면 +0.1 (최대 1.0)
  strength가 없으면 status로 대신: matched 1.0, partial 0.5, missing 0

가중치
  필수 3 · 우대 1 · 핵심 요건(공고 제목이나 기술 스택에도 나옴) 1.5배
  소프트 스킬은 프로젝트로 증명하기 어려워 점수에서 제외하고 따로 표시

점수에 넣을 요건이 하나도 없으면 담당업무를 요건처럼 매칭한 결과로 대신하고,
그것도 없으면 None (라벨은 안정권까지만, filters.fit_band).

이 수치들(5단계 값, 가중치, 보너스)은 근거 없는 초기값이다. 라벨 경계와 함께 보정 대상이다
(DESIGN_DECISIONS D5).
"""

from __future__ import annotations

import re

from .schemas import GapReport, Requirement, RequirementMatch, ScoreItem

EXCLUDED_CATEGORIES = {"soft"}

# 학력, 전공 요건은 자격 필터(filters.check_eligibility)에서 판정한다. 매칭과 점수에 또 넣으면 두 번 세게 된다.
# '졸업작품' 같은 낱말은 학력 요건이 아니다.
_EDU_PATTERN = re.compile(r"(전공|학위|학사|석사|박사|대졸|초대졸|고졸|(?<!작품)졸업(?!작품)|학력)")


def is_education_requirement(r: Requirement) -> bool:
    return r.category in ("skill", "domain", "etc") and bool(_EDU_PATTERN.search(r.text))


STRENGTH_CREDIT = {"직접 구현": 1.0, "팀에서 담당": 0.85, "사용 경험": 0.6, "조금 만져봄": 0.3, "없음": 0.0}
STATUS_FALLBACK = {"matched": ("직접 구현", 1.0), "partial": ("조금 만져봄", 0.5), "missing": ("없음", 0.0)}
MULTI_EVIDENCE_BONUS = 0.1

WEIGHT_REQUIRED = 3.0
WEIGHT_PREFERRED = 1.0
CORE_MULTIPLIER = 1.5


def credit_of(m: RequirementMatch) -> tuple[str, float]:
    if m.strength is None:
        label, c = STATUS_FALLBACK[m.status]
    else:
        label, c = m.strength, STRENGTH_CREDIT[m.strength]
    if c > 0 and len(set(m.evidence_project_ids)) >= 2:
        c = min(1.0, c + MULTI_EVIDENCE_BONUS)
    return label, c


def weight_of(m: RequirementMatch) -> float:
    w = WEIGHT_REQUIRED if m.is_required else WEIGHT_PREFERRED
    return w * (CORE_MULTIPLIER if m.requirement.core else 1.0)


def _items(matches: list[RequirementMatch]) -> list[ScoreItem]:
    out = []
    for m in matches:
        if m.requirement.category in EXCLUDED_CATEGORIES or is_education_requirement(m.requirement):
            continue
        label, c = credit_of(m)
        out.append(
            ScoreItem(
                text=m.requirement.text,
                is_required=m.is_required,
                weight=weight_of(m),
                credit=c,
                strength=label,
                evidence_project_ids=m.evidence_project_ids,
            )
        )
    return out


def _score(items: list[ScoreItem]) -> float | None:
    total = sum(i.weight for i in items)
    if total == 0:
        return None
    return round(sum(i.weight * i.credit for i in items) / total, 4)


def _met(items: list[ScoreItem]) -> tuple[int, int]:
    """'충족'으로 세는 기준: 사용 경험(0.6) 이상"""
    return sum(i.credit >= 0.6 for i in items), len(items)


def build_gap_report(
    required: list[RequirementMatch],
    preferred: list[RequirementMatch],
    responsibilities: list[RequirementMatch] | None = None,
) -> GapReport:
    """responsibilities: 담당업무를 요건처럼 매칭한 결과 (필수와 우대가 모두 비었을 때만 매칭기가 만든다)"""
    soft = [m.requirement.text for m in required + preferred if m.requirement.category in EXCLUDED_CATEGORIES]
    edu = [m.requirement.text for m in required + preferred if is_education_requirement(m.requirement)]
    req_items, pref_items = _items(required), _items(preferred)

    items, basis = req_items + pref_items, "required"
    if not req_items:
        basis = "preferred" if pref_items else "responsibilities"
    if not items:
        items = _items(responsibilities or [])
        if not items:
            basis = "none"

    req_simple = round(sum(i.credit for i in req_items) / len(req_items), 4) if req_items else None
    strengths = [i.text for i in req_items + pref_items if i.credit >= 0.6]
    gaps = [i.text for i in req_items if i.credit == 0]
    return GapReport(
        fit_score=_score(items),
        breakdown=items,
        required_met=_met(req_items),
        preferred_met=_met(pref_items),
        required_coverage=req_simple,
        coverage_basis=basis,
        preferred_coverage=_score(pref_items),
        soft_requirements=soft,
        excluded_requirements=edu,
        strengths=strengths,
        gaps=gaps,
    )


def percent(score: float | None) -> str:
    """화면 표시: 정수 %. 반올림만 하고 다른 보정은 하지 않는다."""
    return "–" if score is None else f"{round(score * 100)}%"
