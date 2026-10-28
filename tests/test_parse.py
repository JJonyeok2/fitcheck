"""T4 완료 조건 테스트 — 공고 파싱 노드 (텍스트)

TASKS.md T4, AGENT_DESIGN 7장, DESIGN_DECISIONS D1, D2
- 텍스트를 JobPosting으로 바꾸고 원문, 출처, 입력 형태를 붙인다
- 같은 원문(공백 차이 무시)은 캐시에서 꺼내고 LLM을 부르지 않는다. 프롬프트 버전이 바뀌면 다시 파싱한다
- 형식 오류는 재시도, 끝까지 실패하면 사람 확인으로 넘기고 캐시에 남기지 않는다
- 분류 목록 밖의 직무는 '기타' + 직무 분류 불확실
- 지어낸 값 검사: 원문에 없는 경력 연수, 학력, 근무지는 '추출 불확실'로 표시, 원문에 없는 연봉은 지운다
"""

from __future__ import annotations

import pytest
from fitcheck.nodes import parse as parse_mod
from fitcheck.nodes.parse import PROMPT_VERSION, parse_posting

from fitcheck.budget import BudgetExceeded, LLMBudget, ParseCache
from fitcheck.llm import FakeLLM, UsageLedger
from fitcheck.schemas import IT_CATEGORIES, CriticalField

TEXT = """(주)샘플랩스 AX 프로덕트 엔지니어 채용
근무지: 경기 성남시 분당구 판교역로 1
고용형태: 정규직
자격요건
- 학사 이상
- 신입 또는 경력 3년 이하
- Python으로 업무 자동화를 구축해 본 경험
우대사항
- LangGraph 사용 경험
연봉: 3,800 ~ 4,500만원
마감: 2026-11-30
"""


def extraction(**over):
    base = {
        "company": "(주)샘플랩스",
        "title": "AX 프로덕트 엔지니어",
        "job_category": "AX/자동화",
        "location": "경기 성남시 분당구",
        "employment_type": "정규직",
        "deadline": "2026-11-30",
        "experience": {"level": "신입/경력"},
        "education": {"min_degree": "학사"},
        "required": [{"text": "Python으로 업무 자동화를 구축해 본 경험", "category": "skill", "keywords": ["Python"]}],
        "preferred": [{"text": "LangGraph 사용 경험", "category": "skill", "keywords": ["LangGraph"]}],
        "perks": {"salary_min": 3800, "salary_max": 4500},
        "low_confidence_fields": [],
    }
    base.update(over)
    return base


@pytest.fixture
def ledger(tmp_path):
    return UsageLedger(tmp_path / "usage.sqlite")


@pytest.fixture
def cache(tmp_path):
    return ParseCache(tmp_path / "fitcheck.sqlite")


def run(llm, cache, text=TEXT, **kw):
    return parse_posting(text, llm=llm, cache=cache, **kw)


# ── 정상 ─────────────────────────────────────────────────


def test_parses_and_attaches_raw_text_and_meta(ledger, cache):
    llm = FakeLLM([extraction()], ledger=ledger)
    r = run(llm, cache, source_url="https://wanted.example/1", input_type="url")
    assert r.error is None and not r.from_cache
    p = r.posting
    assert p.company == "(주)샘플랩스" and p.job_category == "AX/자동화"
    assert p.raw_text == TEXT
    assert p.meta.input_type == "url" and p.meta.source_url == "https://wanted.example/1"
    assert r.content_hash


def test_prompt_contains_text_categories_and_no_guessing_rule(ledger, cache):
    llm = FakeLLM([extraction()], ledger=ledger)
    run(llm, cache)
    prompt = llm.prompts[0]
    assert "AX 프로덕트 엔지니어" in prompt, "공고 원문이 프롬프트에 들어가야 한다"
    assert all(c in prompt for c in IT_CATEGORIES), "직무 분류 목록이 프롬프트에 들어가야 한다"
    assert "추측" in prompt and "담당업무" in prompt


def test_llm_low_confidence_passes_through(ledger, cache):
    llm = FakeLLM([extraction(low_confidence_fields=["education"])], ledger=ledger)
    r = run(llm, cache)
    assert CriticalField.education in r.needs_human_check


# ── 캐시 ─────────────────────────────────────────────────


def test_cache_hit_skips_llm(ledger, cache):
    run(FakeLLM([extraction()], ledger=ledger), cache)
    empty = FakeLLM([], ledger=ledger)  # 부르면 오류가 난다
    r = run(empty, cache, text=TEXT.replace("\n", "\n  ") + "   ")
    assert r.from_cache and empty.prompts == []
    assert r.posting.company == "(주)샘플랩스"


def test_prompt_version_change_invalidates_cache(ledger, cache, monkeypatch):
    run(FakeLLM([extraction()], ledger=ledger), cache)
    monkeypatch.setattr(parse_mod, "PROMPT_VERSION", PROMPT_VERSION + "-next")
    llm = FakeLLM([extraction(title="바뀐 제목")], ledger=ledger)
    r = run(llm, cache)
    assert not r.from_cache and len(llm.prompts) == 1 and r.posting.title == "바뀐 제목"


# ── 실패 ─────────────────────────────────────────────────


def test_retry_then_success(ledger, cache):
    llm = FakeLLM([{"company": "A"}, extraction()], ledger=ledger)
    r = run(llm, cache)
    assert r.error is None and len(llm.prompts) == 2


def test_retries_exhausted_goes_to_human_and_not_cached(ledger, cache):
    llm = FakeLLM([{}, {}, {}], ledger=ledger)
    r = run(llm, cache)
    assert r.error == "parse_failed" and r.posting is None
    assert set(r.needs_human_check) == set(CriticalField)
    again = FakeLLM([extraction()], ledger=ledger)
    assert not run(again, cache).from_cache, "실패한 결과는 캐시에 남기지 않는다"


def test_budget_counts_parse_calls(ledger, cache):
    budget = LLMBudget(limit=1)
    with pytest.raises(BudgetExceeded):
        run(FakeLLM([{}, extraction()], ledger=ledger), cache, budget=budget)


# ── 직무 분류 ────────────────────────────────────────────


def test_unknown_category_becomes_other_and_flagged(ledger, cache):
    r = run(FakeLLM([extraction(job_category="마케팅 자동화")], ledger=ledger), cache)
    assert r.posting.job_category == "기타"
    assert CriticalField.job_category in r.needs_human_check


# ── 지어낸 값 검사 ───────────────────────────────────────


def test_years_not_in_text_flagged(ledger, cache):
    ex = extraction(experience={"level": "경력", "min_years": 5})
    r = run(FakeLLM([ex], ledger=ledger), cache)
    assert CriticalField.experience in r.needs_human_check, "원문에 '5'가 없는데 5년이라고 하면 의심한다"


def test_years_in_text_not_flagged(ledger, cache):
    ex = extraction(experience={"level": "신입/경력", "max_years": 3})
    r = run(FakeLLM([ex], ledger=ledger), cache)
    assert CriticalField.experience not in r.needs_human_check


def test_degree_not_in_text_flagged(ledger, cache):
    r = run(FakeLLM([extraction(education={"min_degree": "석사"})], ledger=ledger), cache)
    assert CriticalField.education in r.needs_human_check


def test_location_not_in_text_flagged(ledger, cache):
    r = run(FakeLLM([extraction(location="서울 강남구")], ledger=ledger), cache)
    assert CriticalField.location in r.needs_human_check


def test_location_in_text_not_flagged(ledger, cache):
    r = run(FakeLLM([extraction(location="경기 성남시 분당구")], ledger=ledger), cache)
    assert CriticalField.location not in r.needs_human_check


def test_salary_not_in_text_dropped(ledger, cache):
    r = run(FakeLLM([extraction(perks={"salary_min": 5000, "salary_max": 6000})], ledger=ledger), cache)
    assert r.posting.perks.salary_min is None and r.posting.perks.salary_max is None


def test_salary_in_text_kept_with_comma_format(ledger, cache):
    r = run(FakeLLM([extraction()], ledger=ledger), cache)
    assert (r.posting.perks.salary_min, r.posting.perks.salary_max) == (3800, 4500)
