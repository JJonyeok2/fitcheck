"""T9 완료 조건 테스트 — 요건 매칭

TASKS.md T9, AGENT_DESIGN 4.6, 7장, DESIGN_DECISIONS D4, D12, D15
- 기술 별칭: React.js와 리액트는 같은 기술, React Native는 다른 기술
- 기술 요건은 그 기술을 쓴 프로젝트만 후보. 후보가 없으면 LLM 없이 '없음'
- 경험형 요건(domain, etc)은 모든 프로젝트가 후보 (키워드로 못 찾는 경험을 놓치지 않게)
- 소프트 스킬은 LLM에 보내지 않는다
- LLM은 요건 전체를 한 번에 판정하고 충족 정도 5단계와 근거 프로젝트를 낸다
- 코드 재검사: 후보 밖 근거나 근거 없는 충족은 '없음', 팀 프로젝트에서 내가 안 한 기술은 '팀에서 담당'까지,
  '조금 만져봄' 기술은 0.3까지, LLM이 빠뜨린 요건은 '없음'
- 필수와 우대가 모두 없으면 담당업무로 매칭
"""

from __future__ import annotations

import pytest

from fitcheck.aliases import canonical, skills_in
from fitcheck.llm import FakeLLM, UsageLedger
from fitcheck.nodes.match import match_requirements
from fitcheck.schemas import CompetencyDB, ExtractionMeta, JobPosting, Project, Requirement

DB = CompetencyDB(
    projects=[
        Project(
            id="officer",
            name="Officer",
            period="2026.08 ~ 2026.09",
            team_size=1,
            my_role="1인 구축 및 운영",
            contributions=["Discord 봇과 cron으로 뉴스, 채용공고 수집 자동화", "Composio MCP로 Google Calendar 연동"],
            skills=["Python", "MCP", "Linux"],
        ),
        Project(
            id="lovv",
            name="Lovv",
            period="2026.05 ~ 2026.07",
            team_size=4,
            my_role="프론트엔드와 기획",
            contributions=["React로 여행 큐레이션 화면 구현", "팀 요구사항을 Spec 문서로 정리"],
            skills=["React", "TypeScript"],
            touched_skills=["AWS"],
        ),
        Project(
            id="ramen",
            name="야!",
            period="2026.09 ~ 2026.10",
            team_size=1,
            my_role="1인 개발",
            contributions=["라멘 취향 RAG 추천"],
            skills=["RAG", "Next.js"],
        ),
    ]
)


def posting(required=(), preferred=(), responsibilities=()):
    return JobPosting(
        company="A",
        title="AX 엔지니어",
        required=list(required),
        preferred=list(preferred),
        responsibilities=list(responsibilities),
        raw_text="원문",
        meta=ExtractionMeta(input_type="text"),
    )


def req(text, category="skill", keywords=(), core=False):
    return Requirement(text=text, category=category, keywords=list(keywords), core=core)


def judgments(*items):
    return {"items": [{"req_index": i, "strength": s, "evidence_ids": list(ev), "note": ""} for i, s, ev in items]}


@pytest.fixture
def ledger(tmp_path):
    return UsageLedger(tmp_path / "usage.sqlite")


def by_text(matches):
    return {m.requirement.text: m for m in matches}


# ── 별칭 ─────────────────────────────────────────────────


def test_aliases():
    assert canonical("React.js") == canonical("리액트") == canonical("reactjs") == "react"
    assert canonical("React Native") != canonical("React")
    assert canonical("파이썬") == "python"
    assert skills_in("React Native와 TypeScript로 앱 개발") == {"react native", "typescript"}


# ── 후보 고르기 ──────────────────────────────────────────


def test_skill_without_candidate_is_missing_without_llm(ledger):
    llm = FakeLLM([], ledger=ledger)
    m = match_requirements(posting(required=[req("Kotlin 개발 경험", keywords=["Kotlin"])]), DB, llm=llm)
    assert m[0].status == "missing" and m[0].strength == "없음"
    assert llm.prompts == [], "후보가 없으면 LLM을 부르지 않는다"


def test_skill_candidates_only_projects_with_that_skill(ledger):
    llm = FakeLLM([judgments((0, "직접 구현", ["lovv"]))], ledger=ledger)
    match_requirements(posting(required=[req("React 경험", keywords=["React.js"])]), DB, llm=llm)
    prompt = llm.prompts[0]
    assert "React로 여행 큐레이션" in prompt
    assert "라멘 취향" not in prompt and "Discord 봇" not in prompt, "후보가 아닌 프로젝트는 보내지 않는다"


def test_skill_found_in_text_when_keywords_missing(ledger):
    llm = FakeLLM([judgments((0, "직접 구현", ["officer"]))], ledger=ledger)
    m = match_requirements(posting(required=[req("파이썬으로 자동화해 본 경험")]), DB, llm=llm)
    assert m[0].status == "matched" and m[0].evidence_project_ids == ["officer"]


def test_experience_requirement_sees_all_projects(ledger):
    llm = FakeLLM([judgments((0, "직접 구현", ["officer"]))], ledger=ledger)
    m = match_requirements(posting(required=[req("업무 자동화를 구축해 본 경험", category="domain")]), DB, llm=llm)
    prompt = llm.prompts[0]
    assert all(s in prompt for s in ("Discord 봇", "React로 여행", "라멘 취향"))
    assert m[0].strength == "직접 구현"


def test_soft_skill_not_sent_to_llm(ledger):
    llm = FakeLLM([], ledger=ledger)
    m = match_requirements(posting(required=[req("원활한 커뮤니케이션", category="soft")]), DB, llm=llm)
    assert llm.prompts == [] and m[0].strength == "없음"


def test_one_llm_call_for_all_requirements(ledger):
    llm = FakeLLM([judgments((0, "직접 구현", ["officer"]), (1, "사용 경험", ["lovv"]))], ledger=ledger)
    m = match_requirements(
        posting(required=[req("Python", keywords=["Python"])], preferred=[req("TypeScript", keywords=["TypeScript"])]),
        DB,
        llm=llm,
    )
    assert len(llm.prompts) == 1
    assert [x.is_required for x in m] == [True, False]


# ── 코드 재검사 ──────────────────────────────────────────


def test_evidence_outside_candidates_becomes_missing(ledger):
    llm = FakeLLM([judgments((0, "직접 구현", ["ramen"]))], ledger=ledger)  # React 후보는 lovv뿐
    m = match_requirements(posting(required=[req("React", keywords=["React"])]), DB, llm=llm)
    assert m[0].strength == "없음" and m[0].evidence_project_ids == []


def test_claim_without_evidence_becomes_missing(ledger):
    llm = FakeLLM([judgments((0, "사용 경험", []))], ledger=ledger)
    m = match_requirements(posting(required=[req("Python", keywords=["Python"])]), DB, llm=llm)
    assert m[0].strength == "없음"


def test_team_project_capped_when_not_my_skill(ledger):
    # 경험형 요건이라 lovv가 후보지만, 4인 팀이고 내 기술 목록에 'SQL'이 없다
    llm = FakeLLM([judgments((0, "직접 구현", ["lovv"]))], ledger=ledger)
    m = match_requirements(
        posting(required=[req("SQL로 데이터 모델링", category="domain", keywords=["SQL"])]), DB, llm=llm
    )
    assert m[0].strength == "팀에서 담당"


def test_team_project_with_my_skill_keeps_direct(ledger):
    llm = FakeLLM([judgments((0, "직접 구현", ["lovv"]))], ledger=ledger)
    m = match_requirements(posting(required=[req("React", keywords=["React"])]), DB, llm=llm)
    assert m[0].strength == "직접 구현"


def test_solo_project_keeps_direct(ledger):
    llm = FakeLLM([judgments((0, "직접 구현", ["officer"]))], ledger=ledger)
    m = match_requirements(posting(required=[req("업무 자동화", category="domain")]), DB, llm=llm)
    assert m[0].strength == "직접 구현"


def test_touched_skill_capped(ledger):
    llm = FakeLLM([judgments((0, "직접 구현", ["lovv"]))], ledger=ledger)
    m = match_requirements(posting(required=[req("AWS 배포", keywords=["AWS"])]), DB, llm=llm)
    assert m[0].strength == "조금 만져봄" and m[0].status == "partial"


def test_requirement_omitted_by_llm_is_missing(ledger):
    llm = FakeLLM([judgments((0, "직접 구현", ["officer"]))], ledger=ledger)
    m = match_requirements(
        posting(required=[req("Python", keywords=["Python"]), req("React", keywords=["React"])]), DB, llm=llm
    )
    assert by_text(m)["React"].strength == "없음"


def test_strength_maps_to_status(ledger):
    llm = FakeLLM(
        [judgments((0, "사용 경험", ["officer"]), (1, "팀에서 담당", ["lovv"]))],
        ledger=ledger,
    )
    m = match_requirements(
        posting(required=[req("Python", keywords=["Python"]), req("React", keywords=["React"])]), DB, llm=llm
    )
    assert [x.status for x in m] == ["matched", "matched"]


# ── 담당업무 대체 ────────────────────────────────────────


def test_responsibilities_used_when_no_requirements(ledger):
    llm = FakeLLM([judgments((0, "직접 구현", ["officer"]))], ledger=ledger)
    m = match_requirements(posting(responsibilities=["사내 반복 업무를 자동화합니다"]), DB, llm=llm)
    assert len(m) == 1 and m[0].requirement.text == "사내 반복 업무를 자동화합니다" and m[0].is_required


def test_no_requirements_at_all_returns_empty(ledger):
    llm = FakeLLM([], ledger=ledger)
    assert match_requirements(posting(), DB, llm=llm) == [] and llm.prompts == []
