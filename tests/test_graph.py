"""T7 완료 조건 테스트 — 그래프 골격과 CLI

TASKS.md T7, AGENT_DESIGN 2, 5, 6, 11장 (경로 테스트)
- 텍스트 공고 한 건이 입력부터 지원 결정 대기까지 간다
- 프로그램을 껐다 켜도 같은 thread로 이어서 결정할 수 있다
- 보류 인정 → 자격 필터부터 재판정 → 라벨 변경. 보류가 아닌 조건 id는 거부하고 다시 묻는다
- 비IT는 저장 없이 종료, 중복은 LLM 호출 없이 종료, 지원 불가는 결정 없이 저장
- 핵심 필드가 불확실하면 사람 확인에서 멈추고, 고친 값으로 계속 간다
- 터미널 명령 analyze, resume로 같은 흐름이 된다
모든 LLM 호출은 FakeLLM. 아직 없는 노드(이미지, 매칭, 이력서 방향)는 자리표시자다.
"""

from __future__ import annotations

import contextlib
from datetime import date

import pytest

from fitcheck import cli
from fitcheck.config import EXAMPLE, load_profile
from fitcheck.graph import GraphDeps, build_graph, pending_interrupt
from fitcheck.llm import FakeLLM, UsageLedger
from fitcheck.schemas import ApplicationStatus
from fitcheck.store import Store

TODAY = date(2026, 10, 28)
ME, _, _ = load_profile(EXAMPLE)

TEXT = """(주)샘플랩스 AX 프로덕트 엔지니어 채용
근무지: 서울 강남구 테헤란로 1
고용형태: 정규직
주요업무
- 사내 반복 업무를 찾아 LLM 에이전트로 자동화합니다.
- 현업 인터뷰로 요구사항을 정리하고 워크플로를 설계합니다.
자격요건
- 학사 이상
- 신입 또는 경력 3년 이하
- Python으로 업무 자동화를 구축해 본 경험
우대사항
- LangGraph 사용 경험
마감: 2026-11-30
"""


def extraction(**over):
    base = {
        "company": "(주)샘플랩스",
        "title": "AX 프로덕트 엔지니어",
        "job_category": "AX/자동화",
        "location": "서울 강남구",
        "employment_type": "정규직",
        "deadline": "2026-11-30",
        "experience": {"level": "신입/경력"},
        "education": {"min_degree": "학사"},
        "required": [{"text": "Python으로 업무 자동화를 구축해 본 경험", "category": "skill", "keywords": ["Python"]}],
        "preferred": [{"text": "LangGraph 사용 경험", "category": "skill", "keywords": ["LangGraph"]}],
        "low_confidence_fields": [],
    }
    base.update(over)
    return base


_OPENED: list = []


@pytest.fixture(autouse=True)
def _close_connections():
    """Windows는 열린 DB 파일을 지우지 못한다. 테스트가 끝나면 그래프, 저장소, 사용량 기록 연결을 모두 닫는다."""
    yield
    while _OPENED:
        with contextlib.suppress(Exception):
            _OPENED.pop().close()


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path


def make(data_dir, responses=(), today=TODAY):
    ledger = UsageLedger(data_dir / "usage.sqlite")
    _OPENED.append(ledger)
    deps = GraphDeps(
        store=Store(data_dir / "fitcheck.sqlite"),
        me=ME,
        llm=FakeLLM(list(responses), ledger=ledger),
        today=today,
        checkpoint_path=data_dir / "checkpoints.sqlite",
    )
    graph = build_graph(deps)
    _OPENED.extend([deps.store, graph])
    return graph, deps


def start(graph, thread, text=TEXT):
    return graph.invoke({"input_text": text}, {"configurable": {"thread_id": thread}})


def resume(graph, thread, value):
    return graph.resume(thread, value)


# ── 정상 경로 ────────────────────────────────────────────


def test_text_posting_reaches_decide(data_dir):
    graph, deps = make(data_dir, [extraction()])
    out = start(graph, "t1")
    wait = pending_interrupt(graph, "t1")
    assert wait["kind"] == "decide"
    assert wait["band"] in ("합격권", "안정권", "불합격권")
    assert any(c["kind"] == "마감" for c in wait["checks"])
    assert out["posting_id"] and deps.store.get_posting(out["posting_id"]) is not None
    assert deps.store.get_record(out["posting_id"]) is None, "결정 전에는 기록을 만들지 않는다"


def test_apply_saves_record(data_dir):
    graph, deps = make(data_dir, [extraction()])
    pid = start(graph, "t1")["posting_id"]
    out = resume(graph, "t1", {"decision": "apply"})
    assert out["end_reason"] == "saved"
    rec = deps.store.get_record(pid)
    assert rec.status == ApplicationStatus.applied and rec.judged_with
    assert pending_interrupt(graph, "t1") is None


def test_resume_after_restart(data_dir):
    graph, deps = make(data_dir, [extraction()])
    pid = start(graph, "t1")["posting_id"]
    deps.store.close()
    del graph  # 프로그램 종료

    graph2, deps2 = make(data_dir)  # 새로 켜기. LLM 응답 없음 → 다시 파싱하면 실패한다
    assert pending_interrupt(graph2, "t1")["kind"] == "decide"
    resume(graph2, "t1", {"decision": "hold"})
    assert deps2.store.get_record(pid).status == ApplicationStatus.on_hold


# ── 보류 인정 ────────────────────────────────────────────


def _cert_required():
    return extraction(certificates=[{"any_of": ["정보처리기사"]}])


def test_override_rejudges_and_changes_band(data_dir):
    from fitcheck.schemas import CompetencyDB, Project

    text = TEXT + "- 정보처리기사 필수\n"
    strong = {
        "items": [
            {"req_index": 0, "strength": "직접 구현", "evidence_ids": ["p"]},
            {"req_index": 1, "strength": "직접 구현", "evidence_ids": ["p"]},
        ]
    }
    # 매칭은 인정 전과 후에 한 번씩 돈다. 보완 포인트는 보류가 있는 첫 판정에서만 LLM을 부른다
    # (인정 후에는 보류도 부족한 요건도 없어서 코드 문장)
    note = {"summary": "정보처리기사 취득 여부를 먼저 확인해 주세요."}
    graph, deps = make(data_dir, [_cert_required(), strong, note, strong])
    deps.db = CompetencyDB(
        projects=[Project(id="p", name="P", period="2026", my_role="1인", skills=["Python", "LangGraph"])]
    )
    pid = start(graph, "t1", text)["posting_id"]
    first = pending_interrupt(graph, "t1")
    assert first["band"] == "불합격권"
    assert first["gap_summary"] == note["summary"]
    hold = next(c["id"] for c in first["checks"] if c["result"] == "hold")

    resume(graph, "t1", {"decision": "override", "condition_ids": [hold]})
    second = pending_interrupt(graph, "t1")
    assert second["kind"] == "decide" and second["band"] == "합격권"
    assert "합격권" in second["gap_summary"] and deps.llm.responses == []
    assert next(c for c in second["checks"] if c["id"] == hold)["result"] == "pass"
    assert hold in deps.store.overrides(pid), "인정 기록은 저장소에 남는다"


def test_override_of_non_hold_condition_is_rejected(data_dir):
    graph, _ = make(data_dir, [extraction()])
    start(graph, "t1")
    resume(graph, "t1", {"decision": "override", "condition_ids": ["경력:없는 조건"]})
    again = pending_interrupt(graph, "t1")
    assert again["kind"] == "decide" and "보류" in again.get("error", "")


# ── 일찍 끝나는 경로 ────────────────────────────────────


def test_non_it_ends_without_saving(data_dir):
    graph, deps = make(data_dir, [extraction(job_category="마케팅")])
    out = start(graph, "t1")
    assert out["end_reason"] == "unsupported" and "IT 직무" in out["unsupported_reason"]
    assert deps.store.count_postings() == 0 and pending_interrupt(graph, "t1") is None


def test_duplicate_ends_without_llm(data_dir):
    graph, deps = make(data_dir, [extraction()])
    start(graph, "t1")
    resume(graph, "t1", {"decision": "apply"})
    calls_before = deps.llm.ledger.calls()
    out = start(graph, "t2")  # 같은 공고를 다시 넣음. 파싱 캐시 덕에 LLM을 부르지 않는다
    assert out["end_reason"] == "duplicate"
    assert deps.llm.ledger.calls() == calls_before


def test_expired_posting_saved_without_decide(data_dir):
    graph, deps = make(data_dir, [extraction(deadline="2026-10-01")])
    out = start(graph, "t1")
    assert out["end_reason"] == "saved" and pending_interrupt(graph, "t1") is None
    rec = deps.store.get_record(out["posting_id"])
    assert rec.status == ApplicationStatus.skipped and "지원 불가" in rec.history[-1].memo


def test_too_short_input_ends_with_message(data_dir):
    graph, _ = make(data_dir)
    out = start(graph, "t1", "AI 엔지니어 채용")
    assert out["end_reason"] == "input_unreachable" and "붙여넣" in out["error"]


# ── 사람 확인 ────────────────────────────────────────────


def test_low_confidence_field_asks_human_then_continues(data_dir):
    graph, _ = make(data_dir, [extraction(location="부산 해운대구")])  # 원문에 없는 근무지 → 불확실
    start(graph, "t1")
    wait = pending_interrupt(graph, "t1")
    assert wait["kind"] == "verify" and [f["field"] for f in wait["fields"]] == ["location"]
    resume(graph, "t1", {"fields": {"location": "서울 강남구"}})
    nxt = pending_interrupt(graph, "t1")
    assert nxt["kind"] == "decide"
    assert any(c["kind"] == "근무지" and c["result"] == "pass" for c in nxt["checks"])


def test_invalid_human_fix_is_asked_again(data_dir):
    graph, _ = make(data_dir, [extraction(location="부산 해운대구")])
    start(graph, "t1")
    resume(graph, "t1", {"fields": {"employment_type": "알 수 없는 형태"}})
    again = pending_interrupt(graph, "t1")
    assert again["kind"] == "verify" and again.get("error")


# ── CLI ─────────────────────────────────────────────────


def test_cli_analyze_then_resume(data_dir, tmp_path, monkeypatch, capsys):
    posting_file = tmp_path / "posting.txt"
    posting_file.write_text(TEXT, encoding="utf-8")
    monkeypatch.setenv("FIT_DATA_DIR", str(data_dir))
    judged = {"items": [{"req_index": 0, "strength": "직접 구현", "evidence_ids": ["sample-chatbot"]}]}
    note = {"summary": "우대 요건 근거를 보강해 보세요."}
    monkeypatch.setattr(cli, "_make_llm", lambda ledger: FakeLLM([extraction(), judged, note], ledger=ledger))
    monkeypatch.setattr(cli, "_today", lambda: TODAY)

    assert cli.main(["analyze", "--file", str(posting_file)]) == 0
    out = capsys.readouterr().out
    assert "지원 결정 대기" in out and "thread:" in out
    assert "적합도" in out and "보완: 우대 요건 근거를 보강해 보세요." in out
    thread = out.split("thread:")[1].split()[0]

    assert cli.main(["resume", thread, "--decision", "apply"]) == 0
    assert "저장했어요" in capsys.readouterr().out


# ── 요건 매칭 연결 (T9) ──────────────────────────────────


def test_matching_produces_fit_score_and_pass_band(data_dir):
    from fitcheck.schemas import CompetencyDB, Project

    db = CompetencyDB(
        projects=[
            Project(
                id="officer",
                name="Officer",
                period="2026.08",
                team_size=1,
                my_role="1인 구축",
                contributions=["Python과 cron으로 업무 자동화"],
                skills=["Python"],
            ),
            Project(
                id="oma",
                name="oh_my_agents",
                period="2026.06",
                team_size=1,
                my_role="1인 개발",
                contributions=["LangGraph 없이 에이전트 파이프라인 설계"],
                skills=["Python", "LangGraph"],
            ),
        ]
    )
    judged = {
        "items": [
            {"req_index": 0, "strength": "직접 구현", "evidence_ids": ["officer", "oma"]},
            {"req_index": 1, "strength": "사용 경험", "evidence_ids": ["oma"]},
        ]
    }
    graph, deps = make(data_dir, [extraction(), judged])
    deps.db = db
    start(graph, "t1")
    wait = pending_interrupt(graph, "t1")
    assert wait["fit_score"] is not None and wait["band"] == "합격권"
    assert [m["strength"] for m in wait["matches"]] == ["직접 구현", "사용 경험"]
    assert wait["matches"][0]["evidence"] == ["officer", "oma"]


def test_tests_use_example_profile_not_local(monkeypatch):
    import os

    from fitcheck.config import EXAMPLE, load_profile

    assert os.environ["FIT_PROFILE_FILE"] == str(EXAMPLE), "conftest 안전장치가 켜져 있어야 한다"
    _, _, db = load_profile()
    assert {p.id for p in db.projects} == {"sample-chatbot", "sample-dashboard"}


# ── 보완 포인트 연결 (T10) ───────────────────────────────


def test_gap_summary_reaches_decide(data_dir):
    """역량이 비어 있으면 요건이 모두 '없음'이라 보완할 게 있다 → 보완 포인트 LLM 1회"""
    text = "필수 요건에 맞는 프로젝트 근거가 아직 없어요. 핵심 요건부터 작은 프로젝트로 채워 보세요."
    graph, deps = make(data_dir, [extraction(), {"summary": text}])
    out = start(graph, "t1")
    wait = pending_interrupt(graph, "t1")
    assert wait["gap_summary"] == text
    assert out["llm_calls"] == 2  # 파싱 1 + 보완 포인트 1 (후보가 없어 매칭은 0)


def test_gap_summary_falls_back_without_llm_answer(data_dir):
    """보완 포인트 LLM이 실패해도 결정 화면까지 간다 (코드가 만든 기본 문장)"""
    graph, deps = make(data_dir, [extraction()])
    start(graph, "t1")
    wait = pending_interrupt(graph, "t1")
    assert wait["kind"] == "decide" and wait["band"] in wait["gap_summary"]
