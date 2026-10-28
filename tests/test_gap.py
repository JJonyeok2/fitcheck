"""T10 완료 조건 테스트 — 점수, 라벨, 보완 포인트

TASKS.md T10, AGENT_DESIGN 7장 (summarize_gap 계약), DESIGN_DECISIONS D1
- 라벨 4종이 고정 입력에서 각각 나온다
- 보완 포인트는 2문장 이내. 숫자와 라벨은 코드가 넘긴 값만 쓴다
- 어기면 LLM 문장을 버리고 코드가 만든 기본 문장을 쓴다
- 보완할 게 없거나 계산할 요건이 없으면 LLM을 부르지 않는다
- 보완 포인트는 보조 정보라서 LLM이 실패해도 분석은 멈추지 않는다 (기본 문장)
"""

from __future__ import annotations

import pytest

from fitcheck.budget import LLMBudget
from fitcheck.filters import fit_band
from fitcheck.llm import FakeLLM, LLMError, UsageLedger
from fitcheck.nodes.gap import default_summary, summarize_gap
from fitcheck.schemas import ConditionCheck, EducationTier, EligibilityResult, FitBand, Requirement, RequirementMatch
from fitcheck.scoring import build_gap_report

ELIGIBLE = EligibilityResult(status="eligible", checks=[ConditionCheck(kind="학력", label="학사", result="pass")])
HOLD = EligibilityResult(
    status="ambiguous",
    reasons=["정보처리기사 취득 예정"],
    checks=[ConditionCheck(kind="자격증", label="정보처리기사", result="hold", detail="취득 예정")],
)
BLOCKED = EligibilityResult(
    status="ineligible",
    reasons=["경력 3년 이상"],
    checks=[ConditionCheck(kind="경력", label="3년 이상", result="fail", detail="경력 3년 이상")],
)


def m(text, strength, required=True, core=False, evidence=("p1",), category="skill"):
    return RequirementMatch(
        requirement=Requirement(text=text, category=category, core=core),
        is_required=required,
        status="matched"
        if strength in ("직접 구현", "팀에서 담당", "사용 경험")
        else "partial"
        if strength == "조금 만져봄"
        else "missing",
        strength=strength,
        evidence_project_ids=list(evidence) if strength != "없음" else [],
    )


def report(matches):
    return build_gap_report([x for x in matches if x.is_required], [x for x in matches if not x.is_required])


# 적합도 = (4.5×1 + 4.5×0 + 3×0.3 + 1×0) / 13 = 0.4154 → 42%, 필수 1/3 충족, 우대 0/1
MIXED = [
    m("Python 개발", "직접 구현", core=True),
    m("Kotlin 3년 경험", "없음", core=True),
    m("AWS 배포", "조금 만져봄"),
    m("Docker", "없음", required=False),
]
STRONG = [m("Python 개발", "직접 구현", core=True), m("React", "직접 구현")]
MIDDLE = [m("Python 개발", "직접 구현"), m("React", "사용 경험"), m("Kotlin", "없음")]


@pytest.fixture
def ledger(tmp_path):
    return UsageLedger(tmp_path / "usage.sqlite")


def summary(*texts):
    return [{"summary": t} for t in texts]


# ── 라벨 4종 (고정 입력) ─────────────────────────────────


def test_four_bands_from_fixed_inputs():
    tier = EducationTier.DEGREE_MATCH
    assert fit_band(ELIGIBLE, tier, report(STRONG)) == FitBand.PASS
    assert fit_band(ELIGIBLE, tier, report(MIDDLE)) == FitBand.STABLE
    assert fit_band(ELIGIBLE, tier, report(MIXED)) == FitBand.LOW
    assert fit_band(HOLD, tier, report(STRONG)) == FitBand.LOW, "보류가 있으면 점수가 높아도 불합격권"
    assert fit_band(BLOCKED, tier, report(STRONG)) == FitBand.BLOCKED


def test_mixed_fixture_numbers():
    g = report(MIXED)
    assert round(g.fit_score * 100) == 42 and g.required_met == (1, 3) and g.preferred_met == (0, 1)


# ── 기본 문장 (코드) ─────────────────────────────────────


def test_default_summary_states_band_score_and_core_gap_first():
    g = report(MIXED)
    text = default_summary(g, FitBand.LOW, ELIGIBLE)
    assert "불합격권" in text and "42%" in text and "1/3" in text
    assert "Kotlin 3년 경험" in text
    assert text.index("Kotlin 3년 경험") < text.index("AWS 배포"), "핵심 요건부터"


def test_default_summary_mentions_hold_first():
    text = default_summary(report(STRONG), FitBand.LOW, HOLD)
    assert "정보처리기사" in text


def test_default_summary_passes_its_own_check(ledger):
    """기본 문장도 같은 규칙(숫자, 라벨, 2문장)을 지켜야 한다"""
    g = report(MIXED)
    llm = FakeLLM(summary(default_summary(g, FitBand.LOW, ELIGIBLE)), ledger=ledger)
    out = summarize_gap(g, FitBand.LOW, ELIGIBLE, llm=llm)
    assert out.source == "llm"


# ── LLM을 부르지 않는 경우 ───────────────────────────────


def test_nothing_to_improve_skips_llm(ledger):
    llm = FakeLLM([], ledger=ledger)
    out = summarize_gap(report(STRONG), FitBand.PASS, ELIGIBLE, llm=llm)
    assert llm.prompts == [] and out.source == "rule" and "합격권" in out.text


def test_no_requirements_skips_llm(ledger):
    llm = FakeLLM([], ledger=ledger)
    out = summarize_gap(report([]), FitBand.STABLE, ELIGIBLE, llm=llm)
    assert llm.prompts == [] and out.source == "rule" and out.text


# ── LLM 문장 검사 ────────────────────────────────────────


def test_llm_summary_used_and_prompt_has_code_values(ledger):
    text = "필수 3개 중 1개만 근거가 있어 적합도가 42%예요. Kotlin 3년 경험을 먼저 채우고 AWS 배포 근거를 보강하세요."
    llm = FakeLLM(summary(text), ledger=ledger)
    budget = LLMBudget()
    out = summarize_gap(report(MIXED), FitBand.LOW, ELIGIBLE, llm=llm, budget=budget)
    assert out.source == "llm" and out.text == text and budget.used == 1
    p = llm.prompts[0]
    assert all(s in p for s in ("불합격권", "42%", "1/3", "Kotlin 3년 경험", "AWS 배포", "Docker"))


def test_number_from_requirement_text_is_allowed(ledger):
    llm = FakeLLM(summary("Kotlin 3년 경험이 가장 큰 공백이에요."), ledger=ledger)
    assert summarize_gap(report(MIXED), FitBand.LOW, ELIGIBLE, llm=llm).source == "llm"


def test_invented_number_falls_back(ledger):
    llm = FakeLLM(summary("Kotlin을 6주만 공부하면 합격 가능성이 높아져요."), ledger=ledger)
    out = summarize_gap(report(MIXED), FitBand.LOW, ELIGIBLE, llm=llm)
    assert out.source == "rule" and "숫자" in out.reason
    assert out.text == default_summary(report(MIXED), FitBand.LOW, ELIGIBLE)


def test_changed_percent_falls_back(ledger):
    llm = FakeLLM(summary("적합도가 45%라 조금만 보완하면 돼요."), ledger=ledger)
    assert summarize_gap(report(MIXED), FitBand.LOW, ELIGIBLE, llm=llm).source == "rule"


def test_other_band_label_falls_back(ledger):
    llm = FakeLLM(summary("Kotlin만 채우면 안정권이에요."), ledger=ledger)
    out = summarize_gap(report(MIXED), FitBand.LOW, ELIGIBLE, llm=llm)
    assert out.source == "rule" and "라벨" in out.reason


def test_own_band_label_is_not_mistaken_for_substring(ledger):
    """'불합격권' 안에 '합격권'이 들어 있다. 같은 라벨을 쓴 문장은 통과해야 한다"""
    llm = FakeLLM(summary("지금은 불합격권이에요. Kotlin 3년 경험을 먼저 채우세요."), ledger=ledger)
    assert summarize_gap(report(MIXED), FitBand.LOW, ELIGIBLE, llm=llm).source == "llm"


def test_more_than_two_sentences_falls_back(ledger):
    llm = FakeLLM(summary("Kotlin이 없어요. AWS도 약해요. Docker도 없어요."), ledger=ledger)
    out = summarize_gap(report(MIXED), FitBand.LOW, ELIGIBLE, llm=llm)
    assert out.source == "rule" and "문장" in out.reason


def test_empty_summary_falls_back(ledger):
    llm = FakeLLM(summary("  "), ledger=ledger)
    assert summarize_gap(report(MIXED), FitBand.LOW, ELIGIBLE, llm=llm).source == "rule"


# ── 실패해도 멈추지 않는다 ───────────────────────────────


def test_llm_error_falls_back(ledger):
    llm = FakeLLM([LLMError("retryable", "잠시 후 다시")], ledger=ledger)
    out = summarize_gap(report(MIXED), FitBand.LOW, ELIGIBLE, llm=llm)
    assert out.source == "rule" and out.text


def test_budget_exceeded_falls_back(ledger):
    llm = FakeLLM(summary("쓰이지 않음"), ledger=ledger)
    out = summarize_gap(report(MIXED), FitBand.LOW, ELIGIBLE, llm=llm, budget=LLMBudget(used=12, limit=12))
    assert out.source == "rule" and llm.prompts == []


def test_pass_prediction_falls_back(ledger):
    """라벨은 합격 확률이 아니다. 숫자가 없어도 합격 예측 표현은 막는다"""
    for t in ("Kotlin만 채우면 합격 가능성이 높아요.", "이대로면 서류에서 떨어질 수 있어요.", "붙을 확률이 낮아요."):
        llm = FakeLLM(summary(t), ledger=ledger)
        out = summarize_gap(report(MIXED), FitBand.LOW, ELIGIBLE, llm=llm)
        assert out.source == "rule" and "예측" in out.reason, t
