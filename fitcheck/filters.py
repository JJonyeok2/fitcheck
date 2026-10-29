"""자격 필터와 우선순위 — 규칙 기반 (LLM 사용 안 함)

자격 필터: 지원 자체가 불가능한 공고를 걸러낸다
  (마감, 경력, 학력, 졸업, 병역, 필수 자격증, 어학, 근무지, 고용형태).
  - hard(탈락): 명확히 충족 불가
  - soft(보류): '이에 준하는 능력', '관련 자격증', 취득 예정 자격증처럼 사람이 판단할 여지가 있는 것
우선순위: 통과한 공고 중 '내 학력 기준'에 정확히 맞는 공고를 먼저 보여준다.
정렬 키: (라벨, 학력 우선순위, 필수요건 충족률 내림차순)
"""

from __future__ import annotations

import re
from datetime import date

from .schemas import (
    BAND_ORDER,
    DEGREE_ORDER,
    IT_CATEGORIES,
    PROFICIENCY_ORDER,
    ConditionCheck,
    CriticalField,
    EducationTier,
    EligibilityResult,
    FitBand,
    GapReport,
    JobPosting,
    LanguageReq,
    TestScore,
    UserConstraints,
)


def check_supported(posting: JobPosting) -> tuple[str, str]:
    """IT 직무 전용 관문. 자격 필터보다 먼저, 파싱 직후에 실행한다.
    반환: ("supported" | "unsupported" | "verify", 안내 문구)
      - unsupported: 비IT 직무 → 분석, 저장 없이 종료
      - verify     : 분류가 불확실 → 사람 확인 후 다시 판정
    """
    if CriticalField.job_category in posting.meta.low_confidence_fields:
        return "verify", "직무 분류가 확실하지 않아요. IT 직무인지 확인해 주세요."
    if posting.job_category in IT_CATEGORIES:
        return "supported", ""
    return "unsupported", f"IT 직무 공고만 분석할 수 있어요. (이 공고: {posting.job_category})"


DEADLINE_SOON_DAYS = 3


def check_eligibility(
    posting: JobPosting,
    me: UserConstraints,
    today: date | None = None,
    overrides: set[str] | frozenset[str] = frozenset(),
) -> EligibilityResult:
    """조건별 판정을 모두 기록한다 (통과 포함). UI의 조건 칩이 이 목록을 그대로 그린다.

    overrides: 사용자가 직접 '인정됨'으로 바꾼 보류 조건 id. 해당 보류는 통과로 바뀐다.
    """
    today = today or date.today()
    checks: list[ConditionCheck] = []

    def add(kind: str, label: str, result: str, detail: str = "") -> None:
        checks.append(ConditionCheck(kind=kind, label=label, result=result, detail=detail))

    # 마감
    if posting.deadline:
        left = (posting.deadline - today).days
        if left < 0:
            add("마감", f"{posting.deadline:%m/%d} 마감", "fail", "마감된 공고")
        else:
            add("마감", f"D-{left}" if left else "오늘 마감", "pass", "마감 임박" if left <= DEADLINE_SOON_DAYS else "")
    elif posting.rolling_deadline:
        add("마감", "채용시 마감", "pass", "언제든 닫힐 수 있음")

    # 경력
    exp = posting.experience
    if exp.preferred_only or exp.level != "경력":
        add("경력", exp.level if exp.level != "무관" else "경력 무관", "pass")
    elif exp.min_years is None:
        add("경력", "경력직", "hold", "요구 연수가 명시되지 않음")
    elif exp.min_years > me.experience_years:
        add("경력", f"경력 {exp.min_years}년 이상", "fail", f"내 경력 {me.experience_years:g}년")
    else:
        add("경력", f"경력 {exp.min_years}년 이상", "pass")

    # 학력
    edu = posting.education
    edu_label = (
        "학력 무관" if edu.min_degree == "무관" else f"{edu.min_degree} 이상" + (" 우대" if edu.preferred_only else "")
    )
    if not edu.preferred_only and DEGREE_ORDER[edu.min_degree] > DEGREE_ORDER[me.degree]:
        add("학력", edu_label, "fail", f"내 학력 {me.degree}")
    else:
        add("학력", edu_label, "pass")
    if edu.majors:
        ok = any(major_matches(m, me.major) for m in edu.majors)
        add("학력", f"전공 {', '.join(edu.majors)}", "pass" if ok else "hold", "" if ok else "관련 전공 인정 여부 확인")

    # 졸업 요건
    grad = posting.graduation
    if grad.target == "졸업예정자":
        if me.graduated:
            add("졸업", "졸업예정자 한정", "fail", "이미 졸업")
        elif grad.expected_by and me.graduation_date and me.graduation_date > grad.expected_by:
            add("졸업", f"{grad.expected_by:%Y.%m} 졸업예정", "fail", f"내 졸업 예정 {me.graduation_date:%Y.%m}")
        else:
            add("졸업", "졸업예정자", "pass")
    elif grad.target == "기졸업자" and not me.graduated:
        add("졸업", "기졸업자 한정", "fail", "아직 졸업 전")
    elif grad.target != "무관":
        add("졸업", grad.target, "pass")
    if grad.max_years_since is not None:
        if me.graduation_date is None:
            add("졸업", f"졸업 후 {grad.max_years_since}년 이내", "hold", "졸업일 미입력")
        else:
            years = (today - me.graduation_date).days / 365.25
            ok = years <= grad.max_years_since
            add(
                "졸업",
                f"졸업 후 {grad.max_years_since}년 이내",
                "pass" if ok else "fail",
                "" if ok else f"졸업 후 {years:.1f}년",
            )

    # 병역
    if posting.military == "군필 또는 면제":
        add("병역", "군필 또는 면제", "pass" if me.military_done else "fail", "" if me.military_done else "미필")

    # 필수 자격증
    for cert in posting.certificates:
        if cert.preferred_only:
            continue
        names = " 또는 ".join(cert.any_of)
        pending = next((p for p in me.pending_certificates if p.name in cert.any_of), None)
        if any(c in me.certificates for c in cert.any_of):
            add("자격증", names, "pass")
        elif pending:
            if pending.expected_date and posting.deadline and pending.expected_date > posting.deadline:
                add(
                    "자격증",
                    names,
                    "fail",
                    f"합격 발표 {pending.expected_date:%m/%d}가 마감 {posting.deadline:%m/%d}보다 늦음",
                )
            elif pending.expected_date and posting.deadline:
                add("자격증", names, "hold", f"합격 발표 {pending.expected_date:%m/%d}, 마감 전 — 합격하면 충족")
            else:
                add("자격증", names, "hold", "취득 예정 — 서류 마감 전 취득 가능한지 확인")
        elif cert.allows_related and me.certificates:
            add("자격증", f"{names} 또는 관련", "hold", "보유 자격증 인정 여부 확인")
        else:
            add("자격증", names, "fail", "미보유")

    # 어학
    for lang in posting.languages:
        if lang.preferred_only:
            continue
        verdict, reason = _check_language(lang, me)
        result = {"ok": "pass", "soft": "hold", "hard": "fail"}[verdict]
        add("어학", _language_label(lang), result, reason)

    # 근무지
    if me.allowed_regions:
        if not posting.location:
            add("근무지", "근무지 미기재", "hold", "공고 원문 확인")
        elif region_allowed(posting.location, me.allowed_regions):
            add("근무지", posting.location, "pass")
        else:
            add("근무지", posting.location, "fail", f"희망 지역({', '.join(me.allowed_regions)}) 밖")

    # 고용형태
    if posting.employment_type:
        bad = posting.employment_type in me.excluded_employment_types
        add("고용형태", posting.employment_type, "fail" if bad else "pass", "제외한 고용형태" if bad else "")

    # 추출 신뢰도가 낮은 필드는 판정을 보류
    for f in posting.meta.low_confidence_fields:
        add("추출", f.value, "hold", "추출 신뢰도 낮음 — 원문 확인 필요")

    # 사용자가 직접 인정한 보류는 통과로
    for c in checks:
        if c.result == "hold" and c.id in overrides:
            c.result, c.overridden, c.detail = "pass", True, f"직접 확인함 ({c.detail})"

    fails = [c for c in checks if c.result == "fail"]
    holds = [c for c in checks if c.result == "hold"]
    reasons = [f"{c.label}: {c.detail}" if c.detail else c.label for c in fails + holds]
    if fails:
        return EligibilityResult(status="ineligible", reasons=reasons, checks=checks)
    if holds:
        return EligibilityResult(status="ambiguous", reasons=reasons, checks=checks)
    return EligibilityResult(status="eligible", checks=checks)


def _language_label(lang: LanguageReq) -> str:
    if lang.accepted_tests:
        tests = " / ".join(
            f"{t.test} {t.score:g}" if t.score is not None else f"{t.test} {t.grade or ''}".strip()
            for t in lang.accepted_tests
        )
        return f"{lang.language} {tests}"
    return f"{lang.language} {lang.min_level}" if lang.min_level else lang.language


# 시 이름 없이 동네나 업무지구로만 표기되는 경우를 위한 별칭
REGION_ALIASES: dict[str, list[str]] = {
    "서울": [
        "서울",
        "강남",
        "마곡",
        "여의도",
        "성수",
        "을지로",
        "구로디지털",
        "가산디지털",
        "상암",
        "잠실",
        "종로",
        "광화문",
    ],
    "성남": ["성남", "판교", "분당", "정자", "수내", "서현", "위례"],
    "안양": ["안양", "평촌", "범계", "인덕원"],
    "과천": ["과천", "과천지식정보타운"],
}


def region_allowed(location: str, allowed: list[str]) -> bool:
    keywords = [kw for r in allowed for kw in REGION_ALIASES.get(r, [r])]
    return any(kw in location for kw in keywords)


# 등급형 시험의 서열 (낮음 → 높음)
GRADE_ORDER: dict[str, list[str]] = {
    "OPIc": ["NL", "NM", "NH", "IL", "IM1", "IM2", "IM3", "IH", "AL"],
    "TOEIC Speaking": ["NL", "NM", "NH", "IL", "IM1", "IM2", "IM3", "IH", "AL", "AM", "AH"],
    "JLPT": ["N5", "N4", "N3", "N2", "N1"],
}


def _meets_test(required: TestScore, mine: TestScore) -> bool:
    if required.test != mine.test:
        return False
    if required.score is not None:
        return mine.score is not None and mine.score >= required.score
    if required.grade is not None:
        order = GRADE_ORDER.get(required.test)
        if not order or mine.grade not in order or required.grade not in order:
            return False  # 서열을 모르는 등급은 충족으로 치지 않음
        return order.index(mine.grade) >= order.index(required.grade)
    return True  # 점수 기준 없이 '성적 보유'만 요구


def _check_language(lang: LanguageReq, me: UserConstraints) -> tuple[str, str]:
    """반환: ("ok" | "soft" | "hard", 사유)"""
    ability = next((a for a in me.languages if a.language == lang.language), None)

    # 1) 공인 성적 요구
    if lang.accepted_tests:
        tests = " 또는 ".join(
            f"{t.test} {t.score:g}" if t.score is not None else f"{t.test} {t.grade or ''}".strip()
            for t in lang.accepted_tests
        )
        if ability and any(_meets_test(r, m) for r in lang.accepted_tests for m in ability.tests):
            return "ok", ""
        if lang.allows_equivalent:
            return "soft", f"{lang.language} 성적 {tests} 요구, '이에 준하는 능력' 인정 — 성적 없이 지원 가능한지 확인"
        return "hard", f"{lang.language} 공인 성적 필수: {tests} (보유 성적 없음)"

    # 2) 수준만 요구 (본인 판단과 비교)
    if lang.min_level:
        mine = PROFICIENCY_ORDER[ability.level] if ability else 0
        gap = PROFICIENCY_ORDER[lang.min_level] - mine
        if gap <= 0:
            return "ok", ""
        if gap == 1:
            return "soft", f"{lang.language} {lang.min_level} 수준 요구 — 한 단계 위, 면접에서 검증될 수 있음"
        return "hard", f"{lang.language} {lang.min_level} 수준 필수 (현재 {ability.level if ability else '없음'})"

    return "ok", ""


def education_tier(posting: JobPosting, me: UserConstraints) -> EducationTier:
    if CriticalField.education in posting.meta.low_confidence_fields:
        return EducationTier.UNKNOWN

    edu = posting.education
    req = DEGREE_ORDER[edu.min_degree]
    mine = DEGREE_ORDER[me.degree]

    if edu.preferred_only:
        # '석사 우대'처럼 내 학력보다 높은 학력을 우대 → 지원 가능하지만 불리
        return EducationTier.DISADVANTAGED if req > mine else EducationTier.OPEN

    if req == mine:
        return EducationTier.DEGREE_MATCH  # '학사 이상', '대졸' → 1순위
    return EducationTier.OPEN  # 무관, 고졸, 전문학사 이상


PASS_COVERAGE = 0.8  # 합격권 최소 적합도 점수 (이상)
STABLE_COVERAGE = 0.5  # 안정권 최소 적합도 점수 (초과). 이하이면 불합격권


def fit_band(eligibility: EligibilityResult, tier: EducationTier, gap: GapReport | None) -> FitBand:
    if eligibility.status == "ineligible":
        return FitBand.BLOCKED
    if eligibility.status == "ambiguous":
        return FitBand.LOW
    coverage = gap.fit_score if gap else None
    if coverage is None:
        return FitBand.STABLE  # 계산할 요건이 없어 검증 불가 → 안정권까지만
    if coverage <= STABLE_COVERAGE:
        return FitBand.LOW
    if coverage >= PASS_COVERAGE and tier in (EducationTier.DEGREE_MATCH, EducationTier.OPEN):
        return FitBand.PASS
    return FitBand.STABLE  # 석사 우대처럼 학력에서 불리하면 충족률이 높아도 안정권까지


def sort_key(band: FitBand, tier: EducationTier, gap: GapReport | None) -> tuple[int, int, float]:
    """라벨 → 학력 우선순위(학사 기준 공고 먼저) → 적합도 점수 순."""
    coverage = gap.fit_score if gap and gap.fit_score is not None else 0.0
    return (BAND_ORDER[band], tier.value, -coverage)


# 전공 계열 (Spec 5.1). 'IT인공지능학부'가 'Computer Science 또는 관련 전공'을 통과하도록.
# 영어 약어(AI, IT, CS, SW)는 단어 경계로만 찾는다 ('Digital' 안의 'it'은 아님).
CS_MAJOR_WORDS = ["컴퓨터", "소프트웨어", "전산", "정보", "인공지능", "데이터", "통신", "computer", "software", "data"]
CS_MAJOR_ABBR = ["ai", "it", "cs", "sw"]
STEM_WORDS = ["이공", "공학", "공과", "전기", "전자", "수학", "통계", "물리", "자연과학"]
RELATED_WORDS = ["관련 전공", "관련전공", "관련학과", "관련 학과", "관련 계열", "유관 전공"]


def _has(text: str, words: list[str], abbr: list[str] = ()) -> bool:
    low = text.lower()
    return any(w in low for w in words) or any(re.search(rf"(?<![a-z]){a}(?![a-z])", low) for a in abbr)


def is_cs_major(text: str) -> bool:
    return _has(text, CS_MAJOR_WORDS, CS_MAJOR_ABBR)


def major_matches(required: str, mine: str) -> bool:
    """요구 전공 문구에 내 전공이 해당하는가.
    - 문구가 내 전공을 그대로 포함하면 통과
    - 문구가 컴퓨터 계열을 말하거나 '관련 전공'(IT 공고라 컴퓨터 계열로 본다)이고, 내 전공이 컴퓨터 계열이면 통과
    - 문구가 '이공계'이고 내 전공이 컴퓨터 계열 또는 이공 계열이면 통과
    """
    if not mine:
        return False
    if mine in required or required in mine:
        return True
    mine_cs = is_cs_major(mine)
    if mine_cs and (is_cs_major(required) or _has(required, RELATED_WORDS)):
        return True
    return _has(required, ["이공"]) and (mine_cs or _has(mine, STEM_WORDS))
