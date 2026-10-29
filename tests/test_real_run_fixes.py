"""실제 OpenAI 첫 실행(2026-10-29, t02 아정당 AI Engineer)에서 찾은 문제 테스트

1. 내 정보 파일이 없으면 예시 파일로 판정하는데, 알리지 않았다 → 예시면 분명히 경고
2. 'Computer Science 또는 관련 전공'을 IT인공지능학부가 통과하지 못해 보류 → 불합격권.
   컴퓨터 계열 전공은 관련 전공으로 인정한다
3. 학력, 전공 요건이 자격 필터와 요건 매칭 두 곳에서 판정됐다 → 매칭과 적합도에서 뺀다
4. 보완 문장이 규칙 문장으로 바뀐 이유가 남지 않았다 → 이유를 상태에 남기고 show --debug로 본다
5. 저장해 둔 진행 상태를 다시 읽을 때 LangGraph 경고 7줄 → 우리 타입을 등록한다
6. 터미널에 요건별 매칭이 안 보여 원인을 찾기 어려웠다 → 요건 칩을 한 줄씩 보여준다
"""

from __future__ import annotations

import logging
from datetime import date

import pytest

from fitcheck import cli
from fitcheck.config import EXAMPLE, load_profile
from fitcheck.filters import check_eligibility, major_matches
from fitcheck.llm import FakeLLM, UsageLedger
from fitcheck.schemas import EducationReq, ExtractionMeta, JobPosting, Requirement, RequirementMatch
from fitcheck.scoring import build_gap_report, is_education_requirement
from tests.test_graph import TEXT, TODAY, extraction, make, pending_interrupt, start

ME, _, _ = load_profile(EXAMPLE)


def P(majors, min_degree="학사"):
    return JobPosting(
        company="A",
        title="t",
        raw_text="x",
        location="서울",
        employment_type="정규직",
        job_category="개발",
        education=EducationReq(min_degree=min_degree, majors=majors),
        meta=ExtractionMeta(input_type="text"),
    )


# ── 2. 전공 ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "required",
    [
        "Computer Science 또는 관련 전공",
        "컴퓨터공학 또는 관련 전공",
        "관련 전공",
        "소프트웨어 관련학과",
        "이공계 전공",
        "컴퓨터공학, 전산학 계열",
    ],
)
def test_it_majors_count_as_related(required):
    for mine in ("IT인공지능학부 AI빅데이터트랙", "소프트웨어학과", "컴퓨터공학", "정보통신공학과"):
        assert major_matches(required, mine), (required, mine)


def test_unrelated_major_not_counted():
    assert not major_matches("Computer Science 또는 관련 전공", "경영학과")
    assert not major_matches("해양, 측지, GIS, 토목공학", "IT인공지능학부 AI빅데이터트랙")


def test_short_english_words_need_word_boundary():
    """'it'이 'Digital' 안에 있다고 IT 전공이 되지 않는다"""
    assert not major_matches("Computer Science 또는 관련 전공", "Digital Media Arts")


def test_related_major_passes_eligibility():
    me = ME.model_copy(update={"major": "IT인공지능학부 AI빅데이터트랙"})
    e = check_eligibility(P(["Computer Science 또는 관련 전공"]), me, today=TODAY)
    chip = next(c for c in e.checks if c.label.startswith("전공"))
    assert chip.result == "pass" and e.status == "eligible"


def test_unrelated_major_still_hold():
    me = ME.model_copy(update={"major": "경영학과"})
    e = check_eligibility(P(["Computer Science 또는 관련 전공"]), me, today=TODAY)
    assert next(c for c in e.checks if c.label.startswith("전공")).result == "hold"


# ── 3. 학력 요건은 매칭과 적합도에서 뺀다 ───────────────


@pytest.mark.parametrize(
    "text",
    [
        "Computer Science 또는 관련 전공, 혹은 그에 준하는 실무 경험",
        "학력 요건: 4년제 학사 졸업자 이상",
        "컴퓨터공학 전공자",
        "이공계 석사 이상 학위 소지자",
    ],
)
def test_education_requirements_detected(text):
    assert is_education_requirement(Requirement(text=text, category="skill"))


@pytest.mark.parametrize(
    "text",
    ["Python 백엔드를 직접 설계, 배포해 본 경험", "졸업작품으로 웹 서비스를 배포해 본 경험", "SQL 활용 능력"],
)
def test_skill_requirements_not_mistaken_for_education(text):
    assert not is_education_requirement(Requirement(text=text, category="skill"))


def test_education_requirement_excluded_from_score():
    edu = RequirementMatch(
        requirement=Requirement(text="Computer Science 또는 관련 전공", category="skill"),
        is_required=True,
        status="missing",
        strength="없음",
    )
    py = RequirementMatch(
        requirement=Requirement(text="Python 백엔드 경험", category="skill"),
        is_required=True,
        status="matched",
        strength="직접 구현",
        evidence_project_ids=["p"],
    )
    g = build_gap_report([edu, py], [])
    assert g.fit_score == 1.0 and g.required_met == (1, 1)
    assert "Computer Science 또는 관련 전공" in g.excluded_requirements


def test_education_requirement_not_sent_to_llm(tmp_path):
    from fitcheck.nodes.match import match_requirements
    from fitcheck.schemas import CompetencyDB, Project

    db = CompetencyDB(projects=[Project(id="p", name="P", period="2026", my_role="1인", skills=["Python"])])
    posting = P([]).model_copy(
        update={
            "required": [
                # 경험형(domain)이면 모든 프로젝트가 후보라 키워드로 걸러지지 않는다. 그래도 LLM에 가면 안 된다
                Requirement(text="Computer Science 또는 관련 전공", category="domain"),
                Requirement(text="Python 경험", category="skill", keywords=["Python"]),
            ]
        }
    )
    llm = FakeLLM(
        [{"items": [{"req_index": 1, "strength": "직접 구현", "evidence_ids": ["p"]}]}],
        ledger=UsageLedger(tmp_path / "u.sqlite"),
    )
    m = match_requirements(posting, db, llm=llm)
    assert "Computer Science" not in llm.prompts[0]
    assert m[1].strength == "직접 구현"


# ── 1, 4, 6. 터미널 ──────────────────────────────────────


JUDGED = {"items": [{"req_index": 0, "strength": "직접 구현", "evidence_ids": ["sample-chatbot"]}]}
OK_SUMMARY = {"summary": "우대 요건 근거를 보강해 보세요."}


def run_cli(data_dir, monkeypatch, responses, *args):
    """응답 순서는 실제 호출 순서: 파싱, 요건 매칭, 보완 포인트 다듬기"""
    posting_file = data_dir / "posting.txt"
    posting_file.write_text(TEXT, encoding="utf-8")
    monkeypatch.setenv("FIT_DATA_DIR", str(data_dir))
    monkeypatch.setattr(cli, "_make_llm", lambda ledger: FakeLLM(list(responses), ledger=ledger))
    monkeypatch.setattr(cli, "_today", lambda: TODAY)
    return cli.main(["analyze", "--file", str(posting_file), *args])


def test_example_profile_warned(tmp_path, monkeypatch, capsys):
    assert run_cli(tmp_path, monkeypatch, [extraction(), JUDGED, OK_SUMMARY]) == 0
    err = capsys.readouterr().err
    assert "예시" in err and "competencies.local.yaml" in err


def test_no_warning_with_own_profile(tmp_path, monkeypatch, capsys):
    own = tmp_path / "mine.yaml"
    own.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("FIT_PROFILE_FILE", str(own))
    assert run_cli(tmp_path, monkeypatch, [extraction(), JUDGED, OK_SUMMARY]) == 0
    assert "예시" not in capsys.readouterr().err


def test_requirement_lines_printed(tmp_path, monkeypatch, capsys):
    assert run_cli(tmp_path, monkeypatch, [extraction(), JUDGED, OK_SUMMARY]) == 0
    out = capsys.readouterr().out
    assert "직접 구현" in out and "sample-chatbot" in out and "없음" in out


def test_summary_fallback_reason_kept_and_shown(tmp_path, monkeypatch, capsys):
    bad = {"summary": "6주만 공부하면 돼요."}  # 넘겨주지 않은 숫자 → 규칙 문장
    assert run_cli(tmp_path, monkeypatch, [extraction(), JUDGED, bad]) == 0
    thread = capsys.readouterr().out.split("thread:")[1].split()[0]
    assert cli.main(["show", thread, "--debug"]) == 0
    out = capsys.readouterr().out
    assert "규칙 문장" in out and "숫자" in out


def test_debug_reason_hidden_without_flag(tmp_path, monkeypatch, capsys):
    bad = {"summary": "6주만 공부하면 돼요."}
    assert run_cli(tmp_path, monkeypatch, [extraction(), JUDGED, bad]) == 0
    assert "넘겨주지 않은 숫자" not in capsys.readouterr().out


# ── 5. 진행 상태 다시 읽을 때 경고 없음 ─────────────────


def test_no_deserialize_warning_on_resume(tmp_path, caplog, monkeypatch):
    """LangGraph는 같은 경고를 프로그램당 한 번만 낸다. 앞 테스트가 이미 냈을 수 있어 기록을 비운다"""
    from langgraph.checkpoint.serde import jsonplus

    monkeypatch.setattr(jsonplus, "_warned_unregistered_types", set())
    graph, deps = make(tmp_path, [extraction()])
    start(graph, "t1")
    graph.close()
    deps.store.close()
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        graph2, deps2 = make(tmp_path)
        assert pending_interrupt(graph2, "t1")["kind"] == "decide"
        graph2.values("t1")
    assert not [r for r in caplog.records if "unregistered" in r.getMessage()]


_ = date  # 타입 힌트용


# ── 7. 내 정보가 바뀌면 같은 공고도 다시 판정 ───────────


def _profile_change(deps):
    """자격증을 하나 딴 것처럼 내 정보를 바꾼다 → 역량 지문이 달라진다"""
    deps.me = deps.me.model_copy(update={"certificates": [*deps.me.certificates, "SQLD"]})


def test_same_posting_rejudged_after_profile_change(tmp_path):
    from fitcheck.schemas import ApplicationStatus

    graph, deps = make(tmp_path, [extraction()])
    pid = start(graph, "t1")["posting_id"]
    graph.resume("t1", {"decision": "hold"})
    _profile_change(deps)
    out = start(graph, "t2")
    assert out.get("end_reason") != "duplicate", "내 정보가 바뀌었으니 다시 판정"
    assert out["posting_id"] == pid and pending_interrupt(graph, "t2")["kind"] == "decide"
    graph.resume("t2", {"decision": "skip"})
    rec = deps.store.get_record(pid)
    assert [h.status for h in rec.history] == [ApplicationStatus.on_hold, ApplicationStatus.skipped], "이력 유지"


def test_same_posting_same_profile_is_duplicate(tmp_path):
    graph, deps = make(tmp_path, [extraction()])
    start(graph, "t1")
    graph.resume("t1", {"decision": "hold"})
    assert start(graph, "t2")["end_reason"] == "duplicate"


def test_applied_posting_not_rejudged(tmp_path):
    """지원 이후 단계는 다시 판정하지 않는다 (needs_rejudge와 같은 기준)"""
    graph, deps = make(tmp_path, [extraction()])
    start(graph, "t1")
    graph.resume("t1", {"decision": "apply"})
    _profile_change(deps)
    assert start(graph, "t2")["end_reason"] == "duplicate"


def test_duplicate_message_without_url(tmp_path, monkeypatch, capsys):
    assert run_cli(tmp_path, monkeypatch, [extraction(), JUDGED, OK_SUMMARY]) == 0
    thread = capsys.readouterr().out.split("thread:")[1].split()[0]
    cli.main(["resume", thread, "--decision", "hold"])
    capsys.readouterr()
    run_cli(tmp_path, monkeypatch, [])
    out = capsys.readouterr().out
    assert "이미 분석한 공고" in out and "주소" not in out and "보류" in out
