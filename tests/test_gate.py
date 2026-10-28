"""T6 완료 조건 테스트 — 관문 노드 연결 (IT 직무 확인, 중복 확인, 자격 필터, 분기)

TASKS.md T6, AGENT_DESIGN 2, 4, 5장
- 비IT 공고는 끝내고 저장하지 않는다. 분류가 애매하면 사람 확인
- 이미 분석한 공고(같은 원문 또는 같은 회사와 직무)는 기존 기록에 URL만 덧붙이고 끝낸다
- 분석하다 멈춘 공고(공고는 저장됐지만 기록이 없음)는 중복이 아니다. 같은 공고 id로 이어서 분석
- 같은 회사 다른 포지션의 지원 이력은 함께 알려준다
- 자격 필터는 저장된 보류 인정을 반영한다
- 분기 함수는 상태만 보고 다음 노드를 고른다
"""

from __future__ import annotations

from datetime import date

import pytest
from fitcheck.nodes.gate import (
    GateDeps,
    check_duplicate_node,
    check_eligibility_node,
    check_supported_node,
    route_after_duplicate,
    route_after_eligibility,
    route_after_supported,
)

from fitcheck.config import EXAMPLE, load_profile
from fitcheck.schemas import (
    ApplicationRecord,
    ApplicationStatus,
    CertificateReq,
    CriticalField,
    EligibilityResult,
    ExtractionMeta,
    JobPosting,
)
from fitcheck.store import Store
from fitcheck.tracking import dedup_key

TODAY = date(2026, 10, 28)
ME, _, _ = load_profile(EXAMPLE)


def posting(company="(주)샘플랩스", title="AX 프로덕트 엔지니어", category="AX/자동화", url=None, low=(), **kw):
    base = dict(
        company=company,
        title=title,
        job_category=category,
        location="서울 강남구",
        employment_type="정규직",
        raw_text=f"{company} {title} 원문",
        meta=ExtractionMeta(input_type="url" if url else "text", source_url=url, low_confidence_fields=list(low)),
    )
    base.update(kw)
    return JobPosting(**base)


@pytest.fixture
def deps(tmp_path):
    store = Store(tmp_path / "fitcheck.sqlite")
    yield GateDeps(store=store, me=ME, today=TODAY)
    store.close()


def state(p: JobPosting, h="h1", **kw):
    return {"posting": p, "content_hash": h, **kw}


def record_for(pid: str, p: JobPosting, status=ApplicationStatus.interested):
    return ApplicationRecord(
        posting_id=pid, dedup_key=dedup_key(p.company, p.title), company=p.company, title=p.title, status=status
    )


# ── IT 직무 확인 ─────────────────────────────────────────


def test_it_posting_supported(deps):
    out = check_supported_node(state(posting()), deps)
    assert out["supported_verdict"] == "supported" and "end_reason" not in out


def test_non_it_ends_and_is_not_saved(deps):
    out = check_supported_node(state(posting(category="마케팅")), deps)
    assert out["supported_verdict"] == "unsupported" and out["end_reason"] == "unsupported"
    assert "IT 직무" in out["unsupported_reason"]
    assert deps.store.count_postings() == 0


def test_unsure_category_goes_to_verify(deps):
    out = check_supported_node(state(posting(category="기타", low=[CriticalField.job_category])), deps)
    assert out["supported_verdict"] == "verify"


# ── 중복 확인 ────────────────────────────────────────────


def test_new_posting_is_saved_and_continues(deps):
    out = check_duplicate_node(state(posting(url="https://wanted.example/1")), deps)
    assert "end_reason" not in out
    assert deps.store.get_posting(out["posting_id"]) is not None
    assert deps.store.source_urls(out["posting_id"]) == ["https://wanted.example/1"]


def test_same_content_already_analyzed_is_duplicate(deps):
    p = posting(url="https://saramin.example/1")
    first = check_duplicate_node(state(p), deps)
    deps.store.save_record(record_for(first["posting_id"], p))
    again = check_duplicate_node(state(posting(url="https://wanted.example/9")), deps)
    assert again["end_reason"] == "duplicate" and again["posting_id"] == first["posting_id"]
    assert deps.store.source_urls(first["posting_id"]) == ["https://saramin.example/1", "https://wanted.example/9"]
    assert deps.store.count_postings() == 1


def test_same_company_and_title_other_text_is_duplicate(deps):
    p = posting(url="https://saramin.example/1")
    first = check_duplicate_node(state(p, h="h1"), deps)
    deps.store.save_record(record_for(first["posting_id"], p))
    other = posting(company="샘플랩스", title="[신입] AX 프로덕트 엔지니어 채용", url="https://groupby.example/3")
    again = check_duplicate_node(state(other, h="h2"), deps)
    assert again["end_reason"] == "duplicate" and again["posting_id"] == first["posting_id"]
    rec = deps.store.get_record(first["posting_id"])
    assert "https://groupby.example/3" in rec.source_urls, "다른 플랫폼 URL이 기존 기록에 붙어야 한다"
    assert deps.store.count_postings() == 1, "중복이면 새 공고를 저장하지 않는다"


def test_abandoned_analysis_is_not_duplicate(deps):
    first = check_duplicate_node(state(posting()), deps)  # 공고만 저장되고 기록은 없음
    again = check_duplicate_node(state(posting()), deps)
    assert "end_reason" not in again and again["posting_id"] == first["posting_id"]


def test_other_position_same_company_is_reported_not_duplicate(deps):
    old = posting(title="데이터 분석가")
    pid = check_duplicate_node(state(old, h="old"), deps)["posting_id"]
    deps.store.save_record(record_for(pid, old, status=ApplicationStatus.rejected))
    out = check_duplicate_node(state(posting(company="㈜샘플랩스"), h="new"), deps)
    assert "end_reason" not in out
    assert [r.title for r in out["already_applied"]] == ["데이터 분석가"]


# ── 자격 필터 ────────────────────────────────────────────


def test_expired_posting_is_ineligible(deps):
    out = check_eligibility_node(state(posting(deadline=date(2026, 10, 1)), posting_id="p1"), deps)
    assert out["eligibility"].status == "ineligible"


def test_stored_overrides_are_applied(deps):
    p = posting(certificates=[CertificateReq(any_of=["정보처리기사"])])
    pid = check_duplicate_node(state(p), deps)["posting_id"]
    first = check_eligibility_node(state(p, posting_id=pid), deps)
    assert first["eligibility"].status == "ambiguous"
    hold = next(c.id for c in first["eligibility"].checks if c.result == "hold")
    deps.store.add_override(pid, hold)
    second = check_eligibility_node(state(p, posting_id=pid), deps)
    assert second["eligibility"].status == "eligible"


def test_state_overrides_merged_with_stored(deps):
    p = posting(certificates=[CertificateReq(any_of=["정보처리기사"])])
    pid = check_duplicate_node(state(p), deps)["posting_id"]
    first = check_eligibility_node(state(p, posting_id=pid), deps)
    hold = next(c.id for c in first["eligibility"].checks if c.result == "hold")
    out = check_eligibility_node(state(p, posting_id=pid, overrides=[hold]), deps)
    assert out["eligibility"].status == "eligible"


# ── 분기 ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("verdict", "nxt"), [("supported", "check_duplicate"), ("verify", "human_verify"), ("unsupported", "__end__")]
)
def test_route_after_supported(verdict, nxt):
    assert route_after_supported({"supported_verdict": verdict}) == nxt


def test_route_after_duplicate():
    assert route_after_duplicate({"end_reason": "duplicate", "posting": posting()}) == "__end__"
    assert route_after_duplicate({"posting": posting(low=[CriticalField.location])}) == "human_verify"
    assert route_after_duplicate({"posting": posting()}) == "check_eligibility"


def test_route_after_eligibility():
    assert route_after_eligibility({"eligibility": EligibilityResult(status="ineligible")}) == "save_record"
    assert route_after_eligibility({"eligibility": EligibilityResult(status="ambiguous")}) == "match_requirements"
    assert route_after_eligibility({"eligibility": EligibilityResult(status="eligible")}) == "match_requirements"
