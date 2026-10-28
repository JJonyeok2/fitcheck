"""T2 완료 조건 테스트 — LLM 클라이언트 (실제 API는 절대 부르지 않는다)

TASKS.md T2, Spec 11.3, 11.4
- 구조화 출력: 스키마로 검증하고, 형식이 틀리면 오류 내용을 넣어 최대 2회 재시도
- 공고 1건 호출 상한(LLMBudget)을 재시도까지 포함해 지킨다
- FIT_LLM=fake면 키가 있어도 FakeLLM만 쓴다
- 호출마다 토큰과 비용을 기록하고, 누적 $20 경고, $27 새 분석 차단 (재판정과 보류 인정은 허용)
- 오류 코드를 알기 쉬운 문장으로 바꾼다 (401, 사용 한도, 크레딧 소진, 일시적 429)
- 키는 로그나 오류 메시지, repr 어디에도 나오지 않는다
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fitcheck.llm import (
    BudgetBlocked,
    FakeLLM,
    LLMError,
    OpenAILLM,
    UsageLedger,
    explain_error,
    get_llm,
    load_pricing,
)
from pydantic import BaseModel

from fitcheck.budget import BudgetExceeded, LLMBudget
from fitcheck.config import load_env

FAKE_KEY = "sk-proj-" + "TESTKEYNOTREAL" * 4


class Pick(BaseModel):
    company: str
    years: int


@pytest.fixture
def ledger(tmp_path):
    return UsageLedger(tmp_path / "usage.sqlite")


# ── 구조화 출력과 재시도 ─────────────────────────────────


def test_fake_returns_validated_model(ledger):
    llm = FakeLLM([{"company": "A", "years": 3}], ledger=ledger)
    out = llm.structured("공고", Pick, purpose="new_analysis")
    assert out == Pick(company="A", years=3)


def test_retry_with_error_message_then_success(ledger):
    llm = FakeLLM(['{"company": "A"}', {"company": "A", "years": 1}], ledger=ledger)
    out = llm.structured("공고", Pick, purpose="new_analysis")
    assert out.years == 1
    assert len(llm.prompts) == 2
    assert "years" in llm.prompts[1], "재시도 프롬프트에 검증 오류 내용이 들어가야 한다"


def test_retries_exhausted_raises_parse_failed(ledger):
    llm = FakeLLM(["{}", "{}", "{}"], ledger=ledger)
    with pytest.raises(LLMError) as e:
        llm.structured("공고", Pick, purpose="new_analysis")
    assert e.value.kind == "parse_failed"
    assert len(llm.prompts) == 3, "처음 1회 + 재시도 2회"


def test_per_posting_budget_counts_retries(ledger):
    budget = LLMBudget(limit=2)
    llm = FakeLLM(["{}", "{}", "{}"], ledger=ledger)
    with pytest.raises(BudgetExceeded):
        llm.structured("공고", Pick, purpose="new_analysis", budget=budget)
    assert budget.used == 2


# ── 실제 API 차단 ────────────────────────────────────────


def test_fake_mode_never_uses_openai_even_with_key(monkeypatch):
    monkeypatch.setenv("FIT_LLM", "fake")
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    assert isinstance(get_llm(), FakeLLM)


def test_openai_mode_without_key_explains(monkeypatch):
    monkeypatch.setenv("FIT_LLM", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(LLMError) as e:
        get_llm()
    assert "OPENAI_API_KEY" in str(e.value) and ".env" in str(e.value)


def test_unknown_mode_rejected(monkeypatch):
    monkeypatch.setenv("FIT_LLM", "something")
    with pytest.raises(LLMError):
        get_llm()


# ── 비용 기록과 누적 한도 ───────────────────────────────


def test_pricing_loaded_from_yaml():
    p = load_pricing()
    assert p.models["gpt-6-luna"].input == 0.10 and p.models["gpt-6-luna"].output == 0.50
    assert p.budget.warn_at_usd == 20 and p.budget.block_new_analysis_at_usd == 27


def test_cost_standard_and_flex(ledger):
    c1 = ledger.record("gpt-6-luna", input_tokens=1_000_000, output_tokens=1_000_000, purpose="new_analysis")
    c2 = ledger.record("gpt-6-luna", input_tokens=1_000_000, output_tokens=1_000_000, tier="flex", purpose="rejudge")
    assert c1 == pytest.approx(0.60) and c2 == pytest.approx(0.30)
    assert ledger.total_usd() == pytest.approx(0.90)


def test_cached_input_billed_at_cached_rate(ledger):
    cost = ledger.record("gpt-6-luna", input_tokens=1_000_000, cached_input_tokens=1_000_000, output_tokens=0)
    assert cost == pytest.approx(0.01), "캐시된 입력은 캐시 단가로만 계산"


def test_unknown_model_rejected(ledger):
    with pytest.raises(LLMError):
        ledger.record("unknown-model", input_tokens=1, output_tokens=1)


@pytest.mark.parametrize(("spent", "state"), [(19.99, "ok"), (20.0, "warn"), (26.99, "warn"), (27.0, "blocked")])
def test_budget_state(ledger, spent, state):
    ledger.add_raw_cost(spent)
    assert ledger.state() == state


def test_blocked_stops_new_analysis_but_allows_rejudge(ledger):
    ledger.add_raw_cost(27.0)
    with pytest.raises(BudgetBlocked):
        FakeLLM([{"company": "A", "years": 1}], ledger=ledger).structured("x", Pick, purpose="new_analysis")
    out = FakeLLM([{"company": "A", "years": 1}], ledger=ledger).structured("x", Pick, purpose="rejudge")
    assert out.years == 1


def test_fake_calls_are_free(ledger):
    FakeLLM([{"company": "A", "years": 1}], ledger=ledger).structured("x", Pick, purpose="new_analysis")
    assert ledger.total_usd() == 0
    assert ledger.calls() == 1, "가짜 호출도 횟수는 기록한다 (비용 0)"


# ── 오류 안내 ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "code", "kind", "words"),
    [
        (401, None, "auth", "만료"),
        (429, "project_spend_limit_exceeded", "spend_limit", "사용 한도"),
        (429, "organization_spend_limit_exceeded", "spend_limit", "사용 한도"),
        (429, "insufficient_quota", "credit", "크레딧"),
        (None, "credit_balance_exhausted", "credit", "크레딧"),
        (429, "rate_limit_exceeded", "retryable", "잠시"),
        (500, None, "retryable", "잠시"),
        (400, "invalid_request", "other", "요청"),
    ],
)
def test_explain_error(status, code, kind, words):
    e = explain_error(status, code)
    assert e.kind == kind and words in e.message


# ── OpenAI 구현 (네트워크 없이 가짜 전송으로) ────────────


def _fake_response(payload: dict, cached: int = 0):
    usage = SimpleNamespace(
        input_tokens=1200,
        input_tokens_details=SimpleNamespace(cached_tokens=cached),
        output_tokens=300,
    )
    return SimpleNamespace(output_parsed=Pick(**payload), usage=usage)


def test_openai_records_usage_and_cost(ledger):
    calls = []

    def transport(**kwargs):
        calls.append(kwargs)
        return _fake_response({"company": "A", "years": 2})

    llm = OpenAILLM(api_key=FAKE_KEY, text_model="gpt-6-luna", ledger=ledger, transport=transport)
    out = llm.structured("공고", Pick, purpose="new_analysis")
    assert out.years == 2
    assert calls[0]["model"] == "gpt-6-luna" and calls[0]["text_format"] is Pick
    assert ledger.total_usd() == pytest.approx((1200 * 0.10 + 300 * 0.50) / 1_000_000)


def test_openai_uses_flex_for_rejudge(ledger):
    calls = []
    llm = OpenAILLM(
        api_key=FAKE_KEY,
        ledger=ledger,
        transport=lambda **k: calls.append(k) or _fake_response({"company": "A", "years": 1}),
    )
    llm.structured("x", Pick, purpose="rejudge")
    assert calls[0]["service_tier"] == "flex"


def test_openai_key_never_exposed(ledger):
    llm = OpenAILLM(api_key=FAKE_KEY, ledger=ledger, transport=lambda **k: _fake_response({"company": "A", "years": 1}))
    assert FAKE_KEY not in repr(llm) and FAKE_KEY not in str(vars(llm).get("_public", ""))

    def boom(**k):
        raise RuntimeError(f"connection failed with key {FAKE_KEY}")

    bad = OpenAILLM(api_key=FAKE_KEY, ledger=ledger, transport=boom)
    with pytest.raises(LLMError) as e:
        bad.structured("x", Pick, purpose="new_analysis")
    assert FAKE_KEY not in str(e.value), "오류 메시지에 키가 섞여 나오면 안 된다"


# ── .env 읽기 ────────────────────────────────────────────


def test_load_env_reads_file_without_overriding(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# 주석\nFIT_LLM=fake\nFIT_TEXT_MODEL = gpt-6-luna\nEMPTY=\nQUOTED="a b"\n', encoding="utf-8")
    monkeypatch.setenv("FIT_LLM", "openai")
    monkeypatch.delenv("FIT_TEXT_MODEL", raising=False)
    monkeypatch.delenv("QUOTED", raising=False)
    load_env(env)
    import os

    assert os.environ["FIT_LLM"] == "openai", "이미 설정된 환경 변수는 덮어쓰지 않는다"
    assert os.environ["FIT_TEXT_MODEL"] == "gpt-6-luna"
    assert os.environ["QUOTED"] == "a b"


def test_load_env_missing_file_is_ok(tmp_path):
    load_env(tmp_path / "없음.env")
