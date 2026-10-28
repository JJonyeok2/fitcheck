"""판정 규칙 테스트 — pytest tests/"""
from datetime import date

from fitcheck.config import EXAMPLE, load_profile
from fitcheck.filters import check_eligibility, check_supported, education_tier, fit_band
from fitcheck.schemas import *
from fitcheck.scoring import build_gap_report
from fitcheck.tracking import already_applied_to, competency_fingerprint, dedup_key, needs_rejudge

TODAY = date(2026, 10, 27)
# 테스트는 항상 공개용 예시 파일 기준 (내 실제 파일과 무관하게 같은 결과)
ME, PREF, DB = load_profile(EXAMPLE)


def P(**k):
    base = dict(company="A", title="t", raw_text="x", location="서울", employment_type="정규직", job_category="개발",
                meta=ExtractionMeta(input_type="text"))
    base.update(k)
    return JobPosting(**base)


def status(p, **kw):
    return check_eligibility(p, ME, today=TODAY, **kw).status


def M(text, st, cat="skill"):
    return RequirementMatch(requirement=Requirement(text=text, category=cat), is_required=True, status=st)


# IT 직무 관문
def test_it_supported():       assert check_supported(P(job_category="AX/자동화"))[0] == "supported"
def test_non_it_rejected():    assert check_supported(P(job_category="마케팅"))[0] == "unsupported"
def test_non_dev_it_roles():
    for c in ["서비스기획/PM/PO", "프로덕트 디자인", "데이터 분석", "IT운영/전산", "IT영업/고객성공", "IT교육/콘텐츠"]:
        assert check_supported(P(job_category=c))[0] == "supported", c
def test_category_unsure():
    p = P(job_category="기타", meta=ExtractionMeta(input_type="image", low_confidence_fields=["job_category"]))
    assert check_supported(p)[0] == "verify"

# 마감
def test_deadline_passed():  assert status(P(deadline=date(2026, 10, 20))) == "ineligible"
def test_deadline_soon():
    r = check_eligibility(P(deadline=date(2026, 10, 29)), ME, today=TODAY)
    deadline = next(c for c in r.checks if c.kind == "마감")
    assert r.status == "eligible" and deadline.detail == "마감 임박"

# 졸업, 병역
def test_grad_expected_only():  assert status(P(graduation=GraduationReq(target="졸업예정자"))) == "ineligible"
def test_grad_within_2y():      assert status(P(graduation=GraduationReq(target="기졸업 및 졸업예정자", max_years_since=2))) == "eligible"
def test_grad_within_0y():      assert status(P(graduation=GraduationReq(max_years_since=0))) == "ineligible"
def test_military():            assert status(P(military="군필 또는 면제")) == "eligible"

# 고용형태
def test_contract_excluded():   assert status(P(employment_type="계약직")) == "ineligible"
def test_parttime_excluded():   assert status(P(employment_type="파트타임")) == "ineligible"
def test_intern_allowed():      assert status(P(employment_type="채용전환형 인턴")) == "eligible"

# 자격증 취득 예정일 비교
def test_pending_cert_no_date():
    assert status(P(certificates=[CertificateReq(any_of=["정보처리기사"])])) == "ambiguous"
def test_pending_cert_after_deadline():
    me = ME.model_copy(deep=True); me.pending_certificates[0].expected_date = date(2026, 12, 10)
    p = P(deadline=date(2026, 11, 30), certificates=[CertificateReq(any_of=["정보처리기사"])])
    assert check_eligibility(p, me, today=TODAY).status == "ineligible"

# 보류 직접 해제
def test_override():
    p = P(languages=[LanguageReq(language="영어", accepted_tests=[TestScore(test="TOEIC", score=800)], allows_equivalent=True)])
    r = check_eligibility(p, ME, today=TODAY)
    assert r.status == "ambiguous"
    hold_id = next(c.id for c in r.checks if c.result == "hold")
    r2 = check_eligibility(p, ME, today=TODAY, overrides={hold_id})
    assert r2.status == "eligible" and any(c.overridden for c in r2.checks)

# 적합도 점수
from fitcheck.scoring import percent


def W(text, strength, required=True, core=False, ev=("p1",), cat="skill"):
    st = {"직접 구현": "matched", "팀에서 담당": "matched", "사용 경험": "matched", "조금 만져봄": "partial", "없음": "missing"}[strength]
    return RequirementMatch(requirement=Requirement(text=text, category=cat, core=core), is_required=required,
                            status=st, strength=strength, evidence_project_ids=list(ev) if strength != "없음" else [])


def test_soft_excluded():
    g = build_gap_report([W("Python", "직접 구현"), W("커뮤니케이션", "없음", cat="soft")], [])
    assert g.fit_score == 1.0 and g.soft_requirements == ["커뮤니케이션"] and len(g.breakdown) == 1
def test_status_fallback_without_strength():
    g = build_gap_report([M("RAG", "partial"), M("Python", "matched")], [])
    assert g.fit_score == 0.75
def test_weighted_score_one_percent_steps():
    req = [W("Python", "직접 구현"), W("LLM API", "사용 경험"), W("RAG", "조금 만져봄", core=True), W("SQL", "팀에서 담당")]
    pref = [W("AWS", "직접 구현", required=False), W("Docker", "없음", required=False)]
    g = build_gap_report(req, pref)
    # (3*1 + 3*.6 + 4.5*.3 + 3*.85 + 1*1 + 1*0) / (3+3+4.5+3+1+1) = 9.7/15.5
    assert g.fit_score == round(9.7 / 15.5, 4) and percent(g.fit_score) == "63%"
    assert g.required_met == (3, 4) and g.preferred_met == (1, 2)
def test_multi_evidence_bonus_capped():
    g = build_gap_report([W("Python", "사용 경험", ev=("a", "b"))], [])
    assert g.fit_score == 0.7
    g = build_gap_report([W("Python", "직접 구현", ev=("a", "b"))], [])
    assert g.fit_score == 1.0
def test_core_weighs_more():
    a = build_gap_report([W("A", "직접 구현", core=True), W("B", "없음")], []).fit_score
    b = build_gap_report([W("A", "직접 구현"), W("B", "없음", core=True)], []).fit_score
    assert a > 0.5 > b
def test_fallback_preferred():
    g = build_gap_report([], [W("AWS", "직접 구현", required=False), W("Docker", "없음", required=False)])
    assert g.coverage_basis == "preferred" and g.fit_score == 0.5
def test_band_uses_fit_score():
    p = P(); e = check_eligibility(p, ME, today=TODAY); tier = education_tier(p, ME)
    hi = build_gap_report([W("A", "직접 구현"), W("B", "팀에서 담당"), W("C", "사용 경험", required=False)], [])
    lo = build_gap_report([W("A", "사용 경험"), W("B", "없음")], [])
    assert fit_band(e, tier, hi) == FitBand.PASS and fit_band(e, tier, lo) == FitBand.LOW
def test_no_requirements_is_stable():
    p = P(); e = check_eligibility(p, ME, today=TODAY)
    g = build_gap_report([], [])
    assert g.coverage_basis == "none" and fit_band(e, education_tier(p, ME), g) == FitBand.STABLE

# 중복, 재판정
def test_dedup():
    assert dedup_key("(주)도슨티", "[신입] AI Builder 채용") == dedup_key("도슨티", "AI Builder")
    assert dedup_key("도슨티", "AI Builder") != dedup_key("도슨티", "프론트엔드 개발자")
def test_already_applied():
    rec = ApplicationRecord(posting_id="1", dedup_key=dedup_key("도슨티", "AI Builder"), company="도슨티",
                            title="AI Builder", status=ApplicationStatus.applied)
    assert already_applied_to("㈜도슨티", [rec])
def test_rejudge_on_new_cert():
    fp = competency_fingerprint(ME, DB)
    rec = ApplicationRecord(posting_id="1", dedup_key="a::b", company="a", title="b", judged_with=fp)
    me2 = ME.model_copy(deep=True); me2.certificates.append("정보처리기사")
    assert needs_rejudge([rec], fp) == [] and needs_rejudge([rec], competency_fingerprint(me2, DB)) == [rec]


# 선호 조건
from fitcheck.preferences import check_preferences


def test_pref_all_known():
    p = P(perks=PostingPerks(salary_min=3800, has_mentor=True, company_stage="Series A"))
    r = check_preferences(p, PREF)
    assert r.matched == 3 and r.known == 3 and r.summary == "선호 3/3"
def test_pref_unknown_not_penalized():
    r = check_preferences(P(), PREF)
    assert r.matched == 0 and r.known == 0 and "정보 없음" in r.summary
def test_pref_salary_range_upper_counts():
    r = check_preferences(P(perks=PostingPerks(salary_min=3000, salary_max=4000)), PREF)
    assert next(c for c in r.checks if c.kind == "연봉").result == "match"
def test_pref_does_not_change_band():
    p = P(perks=PostingPerks(salary_max=2800, has_mentor=False))
    e = check_eligibility(p, ME, today=TODAY)
    assert e.status == "eligible"  # 선호 미충족이어도 자격, 라벨은 그대로


# 비용, 캐시
import pytest
from fitcheck.budget import (MAX_IMAGE_CHUNKS, BudgetExceeded, LLMBudget, ParseCache,
                    cap_image_chunks, content_hash)


def test_hash_ignores_whitespace():
    assert content_hash("AI  엔지니어\n모집") == content_hash("AI 엔지니어 모집")
def test_budget_limit():
    b = LLMBudget(limit=2); b.spend(); b.spend()
    with pytest.raises(BudgetExceeded):
        b.spend(what="parse")
def test_cap_chunks():
    kept, cut = cap_image_chunks([str(i) for i in range(MAX_IMAGE_CHUNKS + 3)])
    assert len(kept) == MAX_IMAGE_CHUNKS and cut
def test_parse_cache(tmp_path):
    c = ParseCache(tmp_path / "c.sqlite"); h = content_hash("원문")
    assert c.get(h) is None
    c.put(h, {"company": "A"})
    assert c.get(h) == {"company": "A"}
