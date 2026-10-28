"""공고 파싱 노드 — 텍스트를 JobPosting으로 (TASKS T4, AGENT_DESIGN 7장)

LLM은 원문을 구조화만 한다. 그 결과를 코드가 다시 검사한다 (DESIGN_DECISIONS D1, D2).
- 원문에 없는 경력 연수, 학력, 근무지 → 추출 불확실로 표시해 사람 확인으로 (틀리면 공고를 잘못 버리게 되는 값)
- 원문에 없는 연봉 → 지운다 (선호 조건이라 사람 확인까지 갈 필요는 없음)
- 분류 목록 밖의 직무 → '기타' + 직무 분류 불확실

같은 원문(공백 차이 무시)과 같은 프롬프트 버전이면 캐시를 쓰고 LLM을 부르지 않는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import create_model

from ..budget import LLMBudget, ParseCache, content_hash
from ..llm import BaseLLM, LLMError, Purpose
from ..schemas import CriticalField, ExtractionMeta, InputType, JobCategory, JobPosting

PROMPT_VERSION = "parse-v1"
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "parse_posting.md"
CATEGORIES: tuple[str, ...] = JobCategory.__args__

# LLM이 채우는 형식: JobPosting에서 원문과 메타를 빼고, 직무 분류는 일단 문자열로 받아 코드가 검사한다
_EXCLUDED = {"raw_text", "meta", "job_category"}
PostingExtraction = create_model(
    "PostingExtraction",
    job_category=(str, "기타"),
    low_confidence_fields=(list[CriticalField], []),
    **{name: (f.annotation, f) for name, f in JobPosting.model_fields.items() if name not in _EXCLUDED},
)


@dataclass
class ParseResult:
    posting: JobPosting | None
    content_hash: str
    from_cache: bool = False
    needs_human_check: list[CriticalField] = field(default_factory=list)
    error: str | None = None


def render_prompt(text: str) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    return template.replace("{categories}", ", ".join(CATEGORIES)).replace("{text}", text)


def _cache_key(h: str) -> str:
    return f"{h}:{PROMPT_VERSION}"


# ── 지어낸 값 검사 ──────────────────────────────────

DEGREE_WORDS = {
    "고졸": ("고졸", "고등학교"),
    "전문학사": ("전문학사", "초대졸", "2년제", "3년제"),
    "학사": ("학사", "대졸", "4년제", "대학교 졸업"),
    "석사": ("석사",),
    "박사": ("박사",),
}


def _digits_text(text: str) -> str:
    """'3,800'을 '3800'으로 읽을 수 있게 숫자 사이 쉼표를 뺀다"""
    return re.sub(r"(?<=\d),(?=\d)", "", text)


def _in_text(value: int, text: str) -> bool:
    return re.search(rf"(?<!\d){value}(?!\d)", _digits_text(text)) is not None


def _location_in_text(location: str, text: str) -> bool:
    for token in location.split():
        if len(token) < 2:
            continue
        stem = token.rstrip("시군구도") or token
        if token in text or (len(stem) >= 2 and stem in text):
            return True
    return False


def verify_against_text(data: dict, text: str) -> tuple[dict, set[CriticalField]]:
    """LLM 추출값을 원문과 대조한다. 고친 데이터와 사람 확인이 필요한 필드를 돌려준다."""
    flags: set[CriticalField] = set(data.get("low_confidence_fields") or [])

    if data.get("job_category") not in CATEGORIES:
        data["job_category"] = "기타"
        flags.add(CriticalField.job_category)

    exp = data.get("experience") or {}
    for key in ("min_years", "max_years"):
        v = exp.get(key)
        if v is not None and not _in_text(int(v), text):
            flags.add(CriticalField.experience)

    degree = (data.get("education") or {}).get("min_degree", "무관")
    if degree != "무관" and not any(w in text for w in DEGREE_WORDS.get(degree, (degree,))):
        flags.add(CriticalField.education)

    loc = data.get("location")
    if loc and not _location_in_text(loc, text):
        flags.add(CriticalField.location)

    perks = dict(data.get("perks") or {})
    for key in ("salary_min", "salary_max"):
        v = perks.get(key)
        if v is not None and not _in_text(int(v), text):
            perks[key] = None
    data["perks"] = perks

    data["low_confidence_fields"] = sorted(flags)
    return data, flags


def _build(
    data: dict, text: str, input_type: InputType, source_url: str | None
) -> tuple[JobPosting, list[CriticalField]]:
    data, flags = verify_against_text(dict(data), text)
    low = data.pop("low_confidence_fields")
    meta = ExtractionMeta(input_type=input_type, source_url=source_url, low_confidence_fields=low)
    posting = JobPosting(**data, raw_text=text, meta=meta)
    return posting, sorted(flags)


def parse_posting(
    text: str,
    *,
    llm: BaseLLM,
    cache: ParseCache,
    input_type: InputType = "text",
    source_url: str | None = None,
    budget: LLMBudget | None = None,
    purpose: Purpose = "new_analysis",
) -> ParseResult:
    h = content_hash(text)
    cached = cache.get(_cache_key(h))
    if cached is not None:
        posting, flags = _build(cached, text, input_type, source_url)
        return ParseResult(posting, h, from_cache=True, needs_human_check=flags)

    try:
        extracted = llm.structured(render_prompt(text), PostingExtraction, purpose=purpose, budget=budget)
    except LLMError as e:
        if e.kind != "parse_failed":
            raise
        return ParseResult(None, h, needs_human_check=list(CriticalField), error="parse_failed")

    raw = extracted.model_dump(mode="json")
    cache.put(_cache_key(h), raw)  # 검사 전 원본을 저장한다. 검사 규칙이 바뀌어도 다시 검사할 수 있게
    posting, flags = _build(raw, text, input_type, source_url)
    return ParseResult(posting, h, needs_human_check=flags)
