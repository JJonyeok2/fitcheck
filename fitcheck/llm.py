"""LLM 클라이언트 — 공급자 독립 인터페이스, FakeLLM, OpenAI 구현, 사용량과 비용 기록

Spec 11.3, 11.4 · AGENT_DESIGN 7장 · DESIGN_DECISIONS D1, D10, D14

사용 규칙
- 노드는 get_llm()으로 받은 클라이언트의 structured()만 부른다.
- purpose는 "new_analysis"(새 공고 분석), "rejudge"(재판정), "override"(보류 인정 후 재판정), "eval"(정답셋 평가) 중 하나.
  누적 비용이 차단선에 닿으면 new_analysis만 막는다. rejudge, eval은 Flex 등급(절반 가격)으로 보낸다.
- FIT_LLM=fake면 키가 있어도 FakeLLM만 쓴다. 테스트와 CI는 항상 이 모드다.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ValidationError

from .budget import MAX_PARSE_RETRIES, LLMBudget

Purpose = Literal["new_analysis", "rejudge", "override", "eval"]
FLEX_PURPOSES = {"rejudge", "eval"}
ALLOWED_WHEN_BLOCKED = {"rejudge", "override"}

PRICING_PATH = Path(__file__).with_name("pricing.yaml")
DEFAULT_USAGE_DB = Path(__file__).resolve().parent.parent / "data" / "usage.sqlite"


# ──────────────────────────────────────────────
# 오류
# ──────────────────────────────────────────────

ErrorKind = Literal["auth", "spend_limit", "credit", "retryable", "parse_failed", "config", "budget", "other"]


class LLMError(RuntimeError):
    """사용자에게 그대로 보여줄 수 있는 문장을 담은 오류. 키나 원문 응답은 담지 않는다."""

    def __init__(self, kind: ErrorKind, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


class BudgetBlocked(LLMError):
    def __init__(self, spent: float, limit: float):
        super().__init__(
            "budget",
            f"누적 API 사용액이 ${spent:.2f}로 새 분석 차단선(${limit:.0f})에 닿았어요. "
            "이미 분석한 공고 조회, 재판정, 보류 인정만 할 수 있어요.",
        )


def explain_error(status: int | None, code: str | None) -> LLMError:
    """OpenAI 오류 상태와 코드를 사용자 문장으로 바꾼다 (Spec 11.3)."""
    code = code or ""
    if status == 401:
        return LLMError("auth", "API 키가 만료됐거나 잘못됐을 수 있어요. .env의 OPENAI_API_KEY를 확인해 주세요.")
    if code in ("project_spend_limit_exceeded", "organization_spend_limit_exceeded"):
        return LLMError(
            "spend_limit", "OpenAI 대시보드에 걸어둔 사용 한도에 닿았어요. 한도를 올리거나 다음 달까지 기다려야 해요."
        )
    if code in ("credit_balance_exhausted", "insufficient_quota"):
        return LLMError("credit", "OpenAI 크레딧이 다 떨어졌어요. 결제 페이지에서 크레딧을 충전해야 해요.")
    if status == 429 or (status is not None and status >= 500):
        return LLMError("retryable", "OpenAI 서버가 바빠요. 잠시 후 다시 시도할게요.")
    return LLMError("other", "분석 요청을 처리하지 못했어요. 같은 공고로 다시 시도할 수 있어요.")


# ──────────────────────────────────────────────
# 단가와 누적 사용량
# ──────────────────────────────────────────────


@dataclass(frozen=True)
class ModelPrice:
    input: float
    cached_input: float
    output: float


@dataclass(frozen=True)
class BudgetLimits:
    total_usd: float
    warn_at_usd: float
    block_new_analysis_at_usd: float


@dataclass(frozen=True)
class Pricing:
    per: int
    tiers: dict[str, float]
    models: dict[str, ModelPrice]
    budget: BudgetLimits


def load_pricing(path: str | Path = PRICING_PATH) -> Pricing:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Pricing(
        per=int(raw["per"]),
        tiers={k: float(v) for k, v in raw["tiers"].items()},
        models={name: ModelPrice(**{k: float(v) for k, v in m.items()}) for name, m in raw["models"].items()},
        budget=BudgetLimits(**{k: float(v) for k, v in raw["budget"].items()}),
    )


class UsageLedger:
    """호출마다 토큰과 비용을 쌓는다. 월마다 초기화하지 않는 누적 기록이다 (Spec 11.4)."""

    def __init__(self, path: str | Path = DEFAULT_USAGE_DB, pricing: Pricing | None = None):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.pricing = pricing or load_pricing()
        self.db = sqlite3.connect(str(path))
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS usage (
                 at TEXT, model TEXT, tier TEXT, purpose TEXT,
                 input_tokens INTEGER, cached_input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL)"""
        )

    def cost_of(
        self, model: str, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0, tier: str = "standard"
    ) -> float:
        price = self.pricing.models.get(model)
        if price is None:
            raise LLMError("config", f"단가표(pricing.yaml)에 없는 모델이에요: {model}")
        fresh = max(0, input_tokens - cached_input_tokens)
        usd = (
            fresh * price.input + cached_input_tokens * price.cached_input + output_tokens * price.output
        ) / self.pricing.per
        return usd * self.pricing.tiers.get(tier, 1.0)

    def record(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
        tier: str = "standard",
        purpose: str = "",
    ) -> float:
        cost = self.cost_of(model, input_tokens, output_tokens, cached_input_tokens, tier)
        self._insert(model, tier, purpose, input_tokens, cached_input_tokens, output_tokens, cost)
        return cost

    def record_free(self, purpose: str = "") -> None:
        """FakeLLM 호출: 횟수만 남기고 비용은 0"""
        self._insert("fake", "fake", purpose, 0, 0, 0, 0.0)

    def add_raw_cost(self, usd: float) -> None:
        """테스트와 수동 보정용"""
        self._insert("manual", "manual", "manual", 0, 0, 0, usd)

    def _insert(self, *row: Any) -> None:
        self.db.execute("INSERT INTO usage VALUES (?,?,?,?,?,?,?,?)", (datetime.now().isoformat(), *row))
        self.db.commit()

    def total_usd(self) -> float:
        return float(self.db.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM usage").fetchone()[0])

    def calls(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM usage WHERE model != 'manual'").fetchone()[0])

    def state(self) -> Literal["ok", "warn", "blocked"]:
        spent, b = self.total_usd(), self.pricing.budget
        if spent >= b.block_new_analysis_at_usd:
            return "blocked"
        if spent >= b.warn_at_usd:
            return "warn"
        return "ok"

    def check(self, purpose: str) -> None:
        if self.state() == "blocked" and purpose not in ALLOWED_WHEN_BLOCKED:
            raise BudgetBlocked(self.total_usd(), self.pricing.budget.block_new_analysis_at_usd)


# ──────────────────────────────────────────────
# 공통 재시도 루프
# ──────────────────────────────────────────────


def _retry_prompt(prompt: str, error: ValidationError) -> str:
    problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or '(전체)'}: {e['msg']}" for e in error.errors())
    return f"{prompt}\n\n[이전 응답이 형식에 맞지 않았습니다. 다음 문제를 고쳐 다시 답하세요: {problems}]"


class BaseLLM:
    ledger: UsageLedger

    def structured(
        self,
        prompt: str,
        schema: type[BaseModel],
        *,
        purpose: Purpose = "new_analysis",
        images: Iterable[str] | None = None,
        budget: LLMBudget | None = None,
        model: str | None = None,
    ) -> BaseModel:
        """스키마로 검증된 결과를 돌려준다. 형식 오류면 오류 내용을 넣어 최대 MAX_PARSE_RETRIES회 재시도."""
        self.ledger.check(purpose)
        current = prompt
        last_error: ValidationError | None = None
        for _ in range(1 + MAX_PARSE_RETRIES):
            if budget is not None:
                budget.spend(what=schema.__name__)
            try:
                return self._call(current, schema, purpose=purpose, images=list(images or []), model=model)
            except ValidationError as e:
                last_error = e
                current = _retry_prompt(prompt, e)
        raise LLMError(
            "parse_failed",
            f"응답 형식이 {1 + MAX_PARSE_RETRIES}번 모두 맞지 않았어요 ({schema.__name__}). 사람 확인으로 넘길게요.",
        ) from last_error

    def _call(
        self, prompt: str, schema: type[BaseModel], *, purpose: str, images: list[str], model: str | None
    ) -> BaseModel:
        raise NotImplementedError


# ──────────────────────────────────────────────
# FakeLLM — 테스트, CI, 키 없이 개발할 때
# ──────────────────────────────────────────────


class FakeLLM(BaseLLM):
    """미리 넣어둔 응답을 순서대로 돌려준다. dict는 그대로, str은 JSON으로 읽는다. Exception은 그대로 던진다."""

    def __init__(self, responses: Iterable[dict | str | Exception] = (), ledger: UsageLedger | None = None):
        self.responses = list(responses)
        self.prompts: list[str] = []
        self.ledger = ledger or UsageLedger()

    def _call(self, prompt, schema, *, purpose, images, model):
        self.prompts.append(prompt)
        self.ledger.record_free(purpose)
        if not self.responses:
            raise LLMError("config", "FakeLLM에 준비된 응답이 없어요. 테스트에서 응답을 넣어 주세요.")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        data = json.loads(item) if isinstance(item, str) else item
        return schema.model_validate(data)


# ──────────────────────────────────────────────
# OpenAI
# ──────────────────────────────────────────────

Transport = Callable[..., Any]


class OpenAILLM(BaseLLM):
    """OpenAI Responses API 구조화 출력. transport를 바꿔 끼우면 네트워크 없이 테스트할 수 있다."""

    def __init__(
        self,
        api_key: str,
        text_model: str = "gpt-6-luna",
        vision_model: str = "gpt-6-luna",
        ledger: UsageLedger | None = None,
        transport: Transport | None = None,
    ):
        self.__key = api_key  # 이름 맹글링: vars()나 repr에 키 이름이 바로 드러나지 않게
        self.text_model = text_model
        self.vision_model = vision_model
        self.ledger = ledger or UsageLedger()
        self._transport = transport

    def __repr__(self) -> str:
        return f"OpenAILLM(text_model={self.text_model!r}, vision_model={self.vision_model!r})"

    def _sdk_transport(self) -> Transport:
        if self._transport is None:
            from openai import OpenAI

            self._transport = OpenAI(api_key=self.__key, max_retries=1).responses.parse
        return self._transport

    def _call(self, prompt, schema, *, purpose, images, model):
        model = model or (self.vision_model if images else self.text_model)
        tier = "flex" if purpose in FLEX_PURPOSES else "standard"
        content: list[dict] = [{"type": "input_text", "text": prompt}]
        content += [{"type": "input_image", "image_url": url} for url in images]
        kwargs: dict[str, Any] = {
            "model": model,
            "input": [{"role": "user", "content": content}],
            "text_format": schema,
        }
        if tier == "flex":
            kwargs["service_tier"] = "flex"
        try:
            resp = self._sdk_transport()(**kwargs)
        except ValidationError:
            raise
        except Exception as e:  # SDK 오류를 사용자 문장으로. 원문 메시지는 키가 섞일 수 있어 버린다
            raise explain_error(getattr(e, "status_code", None), getattr(e, "code", None)) from None

        usage = getattr(resp, "usage", None)
        if usage is not None:
            details = getattr(usage, "input_tokens_details", None)
            self.ledger.record(
                model,
                input_tokens=usage.input_tokens,
                cached_input_tokens=getattr(details, "cached_tokens", 0) or 0,
                output_tokens=usage.output_tokens,
                tier=tier,
                purpose=purpose,
            )
        parsed = getattr(resp, "output_parsed", None)
        if parsed is None:
            # 모델이 스키마에 맞는 출력을 못 냈다. 재시도 루프가 다시 부르도록 검증 오류로 바꾼다
            return schema.model_validate({})
        return parsed if isinstance(parsed, schema) else schema.model_validate(parsed)


# ──────────────────────────────────────────────
# 선택
# ──────────────────────────────────────────────


def get_llm(ledger: UsageLedger | None = None) -> BaseLLM:
    """FIT_LLM 환경 변수로 구현을 고른다. fake면 키가 있어도 실제 API를 부르지 않는다."""
    from .config import load_env

    load_env()
    mode = os.environ.get("FIT_LLM", "openai").strip().lower()
    if mode == "fake":
        return FakeLLM(ledger=ledger)
    if mode != "openai":
        raise LLMError("config", f"FIT_LLM은 fake 또는 openai여야 해요 (지금 값: {mode}).")
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise LLMError("config", "OPENAI_API_KEY가 없어요. .env 파일에 키를 넣어 주세요 (.env.example 참고).")
    return OpenAILLM(
        api_key=key,
        text_model=os.environ.get("FIT_TEXT_MODEL", "gpt-6-luna"),
        vision_model=os.environ.get("FIT_VISION_MODEL", "gpt-6-luna"),
        ledger=ledger,
    )
