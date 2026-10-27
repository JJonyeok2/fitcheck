"""충족률 계산 — 규칙 기반

규칙
- 소프트 스킬('커뮤니케이션 능력' 등)은 프로젝트 근거로 검증하기 어려워 충족률에서 제외하고 따로 표시한다.
- matched = 1, partial = 0.5, missing = 0
- 계산 기준은 필수요건 → (없으면) 우대사항 → (없으면) 담당업무 순으로 대체한다.
- 셋 다 없으면 None. 라벨 판정은 안정권까지만 준다 (filters.fit_band).
"""

from __future__ import annotations

from .schemas import GapReport, RequirementMatch

EXCLUDED_CATEGORIES = {"soft"}
STATUS_SCORE = {"matched": 1.0, "partial": 0.5, "missing": 0.0}


def _coverage(matches: list[RequirementMatch]) -> float | None:
    scored = [m for m in matches if m.requirement.category not in EXCLUDED_CATEGORIES]
    if not scored:
        return None
    return round(sum(STATUS_SCORE[m.status] for m in scored) / len(scored), 4)


def build_gap_report(
    required: list[RequirementMatch],
    preferred: list[RequirementMatch],
    responsibilities: list[RequirementMatch] | None = None,
) -> GapReport:
    """responsibilities: 담당업무를 요건처럼 매칭한 결과 (필수/우대가 모두 비었을 때만 매칭기가 만든다)"""
    soft = [m.requirement.text for m in required + preferred if m.requirement.category in EXCLUDED_CATEGORIES]
    preferred_cov = _coverage(preferred)

    for basis, matches in (("required", required), ("preferred", preferred), ("responsibilities", responsibilities or [])):
        cov = _coverage(matches)
        if cov is not None:
            break
    else:
        basis, cov = "none", None

    strengths = [m.requirement.text for m in required + preferred if m.status == "matched"]
    gaps = [m.requirement.text for m in required if m.status == "missing" and m.requirement.category not in EXCLUDED_CATEGORIES]
    return GapReport(
        required_coverage=cov,
        coverage_basis=basis,
        preferred_coverage=preferred_cov,
        soft_requirements=soft,
        strengths=strengths,
        gaps=gaps,
    )
