"""관문 노드 — IT 직무 확인, 중복 확인, 자격 필터 (TASKS T6, AGENT_DESIGN 2, 4, 5장)

이미 구현된 판정 규칙(filters.py)을 저장소와 이어 그래프 노드로 감싼다.
노드는 (상태, 의존성)을 받아 자기 담당 필드만 담은 dict를 돌려준다. 분기 함수는 상태만 본다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from ..filters import check_eligibility, check_supported
from ..schemas import GraphState, UserConstraints
from ..store import Store
from ..tracking import dedup_key


@dataclass
class GateDeps:
    store: Store
    me: UserConstraints
    today: date | None = None


# ── 노드 ────────────────────────────────────────────


def check_supported_node(state: GraphState, deps: GateDeps) -> dict:
    """비IT 공고는 여기서 끝난다. 저장하지 않는다 (DESIGN_DECISIONS D7)."""
    verdict, message = check_supported(state["posting"])
    out: dict = {"supported_verdict": verdict}
    if verdict == "unsupported":
        out |= {"end_reason": "unsupported", "unsupported_reason": message}
    return out


def check_duplicate_node(state: GraphState, deps: GateDeps) -> dict:
    """이미 분석한 공고면 URL만 덧붙이고 끝낸다. 아니면 공고를 저장하고 같은 회사 지원 이력을 찾는다.

    '이미 분석함'의 기준은 지원 기록이 있는가다. 공고만 저장되고 기록이 없으면(분석 중단) 이어서 분석한다.
    """
    store = deps.store
    posting = state["posting"]
    url = posting.meta.source_url
    key = dedup_key(posting.company, posting.title)

    same_text = store.find_posting_by_hash(state["content_hash"])
    record = (store.get_record(same_text) if same_text else None) or store.find_record_by_dedup_key(key)

    if record is not None:
        if url:
            store.add_source_url(record.posting_id, url)
            if url not in record.source_urls:
                record.source_urls.append(url)
                store.save_record(record)
        return {"end_reason": "duplicate", "posting_id": record.posting_id}

    pid = store.save_posting(posting, state["content_hash"], url)
    others = [r for r in store.already_applied(posting.company) if r.posting_id != pid]
    return {"posting_id": pid, "already_applied": others}


def check_eligibility_node(state: GraphState, deps: GateDeps) -> dict:
    """저장된 보류 인정과 이번 실행에서 받은 인정을 합쳐 판정한다."""
    overrides = deps.store.overrides(state["posting_id"]) | set(state.get("overrides") or [])
    result = check_eligibility(state["posting"], deps.me, today=deps.today, overrides=overrides)
    return {"eligibility": result}


# ── 분기 (상태만 보는 순수 함수, AGENT_DESIGN 5장) ──


def route_after_supported(state: GraphState) -> Literal["check_duplicate", "human_verify", "__end__"]:
    return {"supported": "check_duplicate", "verify": "human_verify", "unsupported": "__end__"}[
        state["supported_verdict"]
    ]


def route_after_duplicate(state: GraphState) -> Literal["check_eligibility", "human_verify", "__end__"]:
    if state.get("end_reason") == "duplicate":
        return "__end__"
    return "human_verify" if state["posting"].meta.low_confidence_fields else "check_eligibility"


def route_after_eligibility(state: GraphState) -> Literal["match_requirements", "save_record"]:
    return "save_record" if state["eligibility"].status == "ineligible" else "match_requirements"
