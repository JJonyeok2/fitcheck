"""T20 완료 조건 테스트 — 내 정보를 이력서 구조로 (Spec 7장, DESIGN_DECISIONS D16, D17)

- 이력서 13개 항목을 받는다. 판정 코드가 쓰는 값(경력 연차, 최고 학력 등)은 저장하지 않고 계산한다
- 경력 연차: 아르바이트 제외, 인턴은 옵션, 겹치는 기간은 한 번만
- 만 나이: 생일 전날과 당일 경계
- 민감 항목(생년월일, 취업 우대사항)은 repr, 오류 메시지, LLM 프롬프트에 나오지 않는다
- 예전 구조의 yaml도 읽되, 바뀐 점을 알린다. 예전 예시와 새 예시는 같은 판정 값을 낸다
- 역량 지문에 판정에 쓰는 새 항목이 들어간다
"""

from __future__ import annotations

import textwrap
from datetime import date

import pytest
import yaml

from fitcheck import config
from fitcheck.profile import (
    Career,
    EmploymentSupport,
    LegacyProfileWarning,
    Profile,
    ProfileError,
    Training,
    experience_years,
    load_profile_file,
    man_age,
)
from fitcheck.tracking import competency_fingerprint

TODAY = date(2026, 10, 29)

# 지금 저장소의 예전 구조 예시 (구조 변경 전 competencies.example.yaml 그대로)
LEGACY_EXAMPLE = textwrap.dedent(
    """
    constraints:
      experience_years: 0
      degree: 학사
      major: 컴퓨터공학
      graduated: true
      graduation_date: 2025-08-31
      military_done: true
      certificates: []
      pending_certificates:
        - name: 정보처리기사
          expected_date: null
      languages:
        - language: 영어
          level: 일상회화
          tests: []
      allowed_regions: [서울, 성남]
      excluded_employment_types: [파트타임, 계약직]
    preferences:
      min_salary: 3000
      want_mentor: true
      company_stages: [Series A, Series B, 상장/대기업]
      work_modes: []
    projects:
      - id: sample-chatbot
        name: 사내 문서 Q&A 챗봇
        period: 2026.03 ~ 2026.04
        team_size: 1
        my_role: 1인 개발
        contributions: [사내 문서를 임베딩해 검색하는 RAG 파이프라인 구축]
        skills: [Python, RAG]
    """
)


def write(tmp_path, text, name="p.yaml"):
    f = tmp_path / name
    f.write_text(text, encoding="utf-8")
    return f


# ── 경력 연차 ────────────────────────────────────────────


def career(kind, start, end):
    return Career(company="A", kind=kind, start=start, end=end, role="개발")


def test_part_time_job_not_counted():
    cs = [career("정규직", "2024.01", "2024.12"), career("아르바이트", "2020.01", "2023.12")]
    assert experience_years(cs, TODAY) == 1.0


def test_intern_counted_only_when_asked():
    cs = [career("인턴", "2025.01", "2025.06")]
    assert experience_years(cs, TODAY) == 0
    assert experience_years(cs, TODAY, include_intern=True) == 0.5


def test_overlapping_periods_counted_once():
    cs = [career("정규직", "2024.01", "2024.12"), career("계약직", "2024.07", "2025.06")]
    assert experience_years(cs, TODAY) == 1.5


def test_ongoing_career_counts_until_today():
    cs = [career("정규직", "2025.10", None)]
    assert experience_years(cs, date(2026, 10, 31)) == 1.1


def test_month_strings_parse_to_whole_months():
    c = career("정규직", "2024.03", "2024-05")
    assert c.start == date(2024, 3, 1) and c.end == date(2024, 5, 31)


# ── 만 나이 ──────────────────────────────────────────────


def test_man_age_boundary():
    birth = date(2000, 5, 10)
    assert man_age(birth, date(2026, 5, 9)) == 25
    assert man_age(birth, date(2026, 5, 10)) == 26


def test_age_unknown_without_birth_date():
    assert Profile().age(TODAY) is None


# ── 판정 값 계산 ─────────────────────────────────────────


def test_constraints_from_resume_structure():
    p = Profile.model_validate(
        {
            "education": [
                {"school": "가나고", "degree": "고졸", "status": "졸업", "graduation_date": "2019-02-10"},
                {
                    "school": "다라대",
                    "location": "충남",
                    "major": "컴퓨터공학",
                    "degree": "학사",
                    "status": "졸업",
                    "graduation_date": "2026-02-28",
                },
            ],
            "careers": [{"company": "A", "kind": "정규직", "start": "2024.01", "end": "2024.12", "role": "개발"}],
            "certificates": {
                "acquired": [{"name": "SQLD", "date": "2025-06-01"}],
                "pending": [{"name": "정보처리기사"}],
            },
            "employment_support": {"military": "군필"},
            "desired": {"regions": ["서울"], "excluded_employment_types": ["계약직"], "min_salary": 3500},
        }
    )
    me = p.constraints(TODAY)
    assert me.degree == "학사" and me.major == "컴퓨터공학" and me.graduated
    assert me.graduation_date == date(2026, 2, 28)
    assert me.experience_years == 1.0 and me.military_done
    assert me.certificates == ["SQLD"] and me.pending_certificates[0].name == "정보처리기사"
    assert me.allowed_regions == ["서울"] and me.excluded_employment_types == ["계약직"]
    assert p.preferences().min_salary == 3500


def test_expected_graduation_is_not_graduated():
    p = Profile.model_validate(
        {"education": [{"school": "다라대", "degree": "학사", "status": "졸업예정", "graduation_date": "2027-02-28"}]}
    )
    me = p.constraints(TODAY)
    assert me.degree == "학사" and not me.graduated


def test_dropout_does_not_count_as_degree():
    p = Profile.model_validate(
        {
            "education": [
                {"school": "가나고", "degree": "고졸", "status": "졸업"},
                {"school": "다라대", "degree": "학사", "status": "중퇴"},
            ]
        }
    )
    assert p.constraints(TODAY).degree == "고졸"


@pytest.mark.parametrize("value,done", [("군필", True), ("면제", True), ("해당 없음", True), ("미필", False)])
def test_military(value, done):
    p = Profile.model_validate({"employment_support": {"military": value}})
    assert p.constraints(TODAY).military_done is done


def test_manual_experience_years_wins():
    p = Profile.model_validate({"experience_years_manual": 2.5, "careers": []})
    assert p.constraints(TODAY).experience_years == 2.5


def test_skills_come_only_from_projects():
    """스킬은 따로 받지 않는다 (D4). 프로젝트의 기술을 모아 보여줄 뿐"""
    p = Profile.model_validate(
        {
            "projects": [
                {"id": "a", "name": "A", "period": "2026", "my_role": "1인", "skills": ["Python"]},
                {
                    "id": "b",
                    "name": "B",
                    "period": "2026",
                    "my_role": "1인",
                    "skills": ["React"],
                    "touched_skills": ["AWS"],
                },
            ]
        }
    )
    assert p.skills() == ["Python", "React", "AWS"]
    assert "skills" not in Profile.model_fields


# ── 민감 항목 (D17) ──────────────────────────────────────


def sensitive_profile():
    return Profile.model_validate(
        {
            "basic": {"birth_date": "1987-06-05"},
            "education": [{"school": "다라대", "degree": "학사", "status": "졸업", "graduation_date": "2026-02-28"}],
            "employment_support": {
                "veteran": True,
                "disabled": True,
                "national_employment_support": True,
                "others": ["SENSITIVE-MARK"],
                "military": "군필",
            },
            "projects": [
                {"id": "officer", "name": "Officer", "period": "2026", "my_role": "1인 구축", "skills": ["Python"]}
            ],
        }
    )


def test_sensitive_fields_hidden_from_repr():
    p = sensitive_profile()
    text = repr(p) + str(p) + repr(p.basic) + repr(p.employment_support) + str(p.employment_support)
    assert "1987" not in text and "SENSITIVE-MARK" not in text and "veteran" not in text


def test_sensitive_defaults_are_not_entered():
    s = EmploymentSupport()
    assert s.veteran is None and s.disabled is None and s.national_employment_support is None and s.others is None


def test_error_message_does_not_echo_values(tmp_path):
    f = write(tmp_path, "basic:\n  birth_date: 1987-13-45\n")
    with pytest.raises(ProfileError) as e:
        load_profile_file(f)
    assert "basic.birth_date" in str(e.value) and "1987-13-45" not in str(e.value) and "1987" not in str(e.value)


def test_sensitive_values_never_reach_llm_prompts(tmp_path):
    """매칭과 보완 포인트 프롬프트 전부 (이력서 방향은 T11에서 같은 테스트에 추가)"""
    from fitcheck.graph import GraphDeps, build_graph
    from fitcheck.llm import FakeLLM, UsageLedger
    from fitcheck.store import Store
    from tests.test_graph import TEXT, extraction

    p = sensitive_profile()
    ledger = UsageLedger(tmp_path / "u.sqlite")
    judged = {"items": [{"req_index": 0, "strength": "직접 구현", "evidence_ids": ["officer"]}]}
    llm = FakeLLM([extraction(), judged, {"summary": "보완할 점을 정리해 보세요."}], ledger=ledger)
    store = Store(tmp_path / "f.sqlite")
    deps = GraphDeps(
        store=store,
        me=p.constraints(TODAY),
        prefs=p.preferences(),
        db=p.competency_db(),
        profile=p,
        llm=llm,
        today=TODAY,
        checkpoint_path=tmp_path / "c.sqlite",
    )
    graph = build_graph(deps)
    try:
        graph.invoke({"input_text": TEXT}, graph.config("t1"))
        graph.refine_summary("t1")
        assert len(llm.prompts) >= 2
        for prompt in llm.prompts:
            assert "1987" not in prompt and "SENSITIVE-MARK" not in prompt
            assert "보훈" not in prompt and "장애" not in prompt
    finally:
        graph.close()
        store.close()
        ledger.close()


# ── 예전 구조 ────────────────────────────────────────────


def test_legacy_yaml_is_converted_with_notice(tmp_path):
    f = write(tmp_path, LEGACY_EXAMPLE)
    with pytest.warns(LegacyProfileWarning, match="예전 구조"):
        p = load_profile_file(f)
    me = p.constraints(TODAY)
    assert me.degree == "학사" and me.graduation_date == date(2025, 8, 31) and me.military_done
    assert [pc.name for pc in me.pending_certificates] == ["정보처리기사"]


def test_new_example_gives_same_judgement_values_as_legacy(tmp_path):
    with pytest.warns(LegacyProfileWarning):
        old = load_profile_file(write(tmp_path, LEGACY_EXAMPLE))
    new = load_profile_file(config.EXAMPLE)
    a, b = old.constraints(TODAY), new.constraints(TODAY)
    assert a.model_dump() == b.model_dump()
    assert old.preferences().model_dump() == new.preferences().model_dump()


def test_example_has_every_section():
    raw = yaml.safe_load(config.EXAMPLE.read_text(encoding="utf-8"))
    for key in (
        "basic",
        "education",
        "careers",
        "projects",
        "trainings",
        "activities",
        "certificates",
        "languages",
        "awards",
        "links",
        "employment_support",
        "desired",
        "intro",
    ):
        assert key in raw, key


def test_load_profile_still_returns_judgement_values():
    me, prefs, db = config.load_profile()
    assert me.degree == "학사" and db.projects and prefs.min_salary == 3000


# ── 역량 지문 ────────────────────────────────────────────


def test_fingerprint_changes_with_new_judgement_items():
    base = Profile()
    with_training = base.model_copy(
        update={"trainings": [Training(name="AI 캠프", category="K-디지털 트레이닝", completed=True)]}
    )
    fp = lambda p: competency_fingerprint(p.constraints(TODAY), p.competency_db(), p)  # noqa: E731
    assert fp(base) != fp(with_training)
    with_award = base.model_copy(update={"awards": [{"name": "상", "host": "주관", "date": "2026-07-01"}]})
    assert fp(base) == fp(with_award), "수상은 판정에 쓰지 않아 재판정을 일으키지 않는다"


def test_unquoted_month_in_yaml_keeps_october(tmp_path):
    """YAML은 2025.10을 소수 2025.1로 읽는다. 10월이 1월이 되면 안 된다"""
    f = write(
        tmp_path,
        "careers:\n  - {company: A, kind: 정규직, start: 2025.10, end: 2026.01}\nexperience_years_manual: 1.5\n",
    )
    p = load_profile_file(f)
    assert p.careers[0].start == date(2025, 10, 1) and p.careers[0].end == date(2026, 1, 31)
    assert p.experience_years_manual == 1.5, "다른 소수는 그대로"
