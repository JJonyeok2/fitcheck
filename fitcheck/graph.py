"""에이전트 그래프 — 노드를 LangGraph로 잇는다 (TASKS T7, AGENT_DESIGN 2, 5, 6장)

수집(normalize, parse) → 관문(supported, duplicate, human_verify, eligibility)
→ 진단(match, score, summarize) → 결정(decide, draft_resume, save_record)

아직 구현하지 않은 노드는 자리표시자다.
- collect_images: 이미지 공고 (T8). 지금은 안내 후 종료
- match_requirements: 요건 매칭 (T9). 지금은 매칭 결과 없음 → 적합도 계산 불가 → 라벨 최대 안정권
- summarize_gap: 보완 포인트 (T10), draft_resume: 이력서 방향 (T11)

설계와 다른 점: 파싱이 재시도까지 실패하면 설계는 사람 확인으로 보내지만, 고칠 공고 값 자체가 없어서
지금은 안내 후 종료한다 (end_reason=parse_failed). 붙여넣기 다시 하기로 해결된다.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from .budget import LLMBudget
from .filters import education_tier, fit_band
from .llm import BaseLLM
from .nodes.gate import (
    GateDeps,
    check_duplicate_node,
    check_eligibility_node,
    check_supported_node,
    route_after_duplicate,
    route_after_eligibility,
    route_after_supported,
)
from .nodes.normalize import PlaywrightFetcher, normalize_input
from .nodes.parse import parse_posting
from .preferences import check_preferences
from .schemas import (
    ApplicationRecord,
    ApplicationStatus,
    CompetencyDB,
    CriticalField,
    GraphState,
    JobPosting,
    StatusChange,
    UserConstraints,
    UserPreferences,
)
from .scoring import build_gap_report
from .store import Store
from .tracking import competency_fingerprint, dedup_key

MAX_VERIFY_ATTEMPTS = 2
DECISIONS = ("apply", "hold", "skip", "override")
STATUS_BY_DECISION = {
    "apply": ApplicationStatus.applied,
    "hold": ApplicationStatus.on_hold,
    "skip": ApplicationStatus.skipped,
}


@dataclass
class GraphDeps:
    store: Store
    me: UserConstraints
    llm: BaseLLM
    checkpoint_path: Path
    today: date | None = None
    prefs: UserPreferences = field(default_factory=UserPreferences)
    db: CompetencyDB = field(default_factory=lambda: CompetencyDB(projects=[]))
    fetcher: PlaywrightFetcher | None = None

    @property
    def gate(self) -> GateDeps:
        return GateDeps(store=self.store, me=self.me, today=self.today)


# ──────────────────────────────────────────────
# 수집
# ──────────────────────────────────────────────


def normalize_node(state: GraphState, deps: GraphDeps) -> dict:
    r = normalize_input(
        text=state.get("input_text"),
        url=state.get("input_url"),
        image_paths=state.get("input_image_paths"),
        fetcher=deps.fetcher,
    )
    out: dict = {
        "input_type": r.input_type,
        "normalized_text": r.text,
        "text_is_sufficient": r.text_is_sufficient,
        "image_chunks": r.image_urls,
    }
    if r.source_url:
        out["input_url"] = r.source_url
    if r.error:
        out |= {"end_reason": "input_unreachable", "error": r.message}
    return out


def collect_images_node(state: GraphState, deps: GraphDeps) -> dict:
    # 자리표시자 (T8)
    return {
        "end_reason": "image_not_supported_yet",
        "error": "이미지로 된 공고는 아직 읽을 수 없어요. 공고 내용을 복사해서 붙여넣어 주세요.",
    }


def parse_node(state: GraphState, deps: GraphDeps) -> dict:
    used = state.get("llm_calls", 0)
    budget = LLMBudget(used=used)
    r = parse_posting(
        state["normalized_text"],
        llm=deps.llm,
        cache=deps.store.parse_cache,
        input_type=state.get("input_type", "text"),
        source_url=state.get("input_url"),
        budget=budget,
    )
    spent = {"llm_calls": budget.used - used}
    if r.error:
        return spent | {
            "content_hash": r.content_hash,
            "end_reason": "parse_failed",
            "error": "공고를 읽지 못했어요. 공고 본문만 다시 붙여넣어 주세요.",
        }
    return spent | {"posting": r.posting, "content_hash": r.content_hash, "needs_human_check": r.needs_human_check}


# ──────────────────────────────────────────────
# 사람 확인
# ──────────────────────────────────────────────

_FIELD_TO_ATTR = {f: f.value for f in CriticalField}


def _verify_fields(state: GraphState) -> list[str]:
    fields = [f.value for f in state["posting"].meta.low_confidence_fields]
    if state.get("supported_verdict") == "verify" and CriticalField.job_category.value not in fields:
        fields.append(CriticalField.job_category.value)
    return fields


def _snippet(text: str, needle: str | None, width: int = 60) -> str:
    if needle and needle in text:
        i = text.index(needle)
        return text[max(0, i - width) : i + len(needle) + width]
    return text[: width * 2]


def human_verify_node(state: GraphState, deps: GraphDeps) -> dict:
    """불확실한 핵심 필드를 사람에게 보여주고 고친 값을 받는다. 잘못된 값이면 이유와 함께 다시 묻는다."""
    posting: JobPosting = state["posting"]
    fields = _verify_fields(state)
    payload: dict[str, Any] = {
        "kind": "verify",
        "reason": "category_unsure" if state.get("supported_verdict") == "verify" else "low_confidence",
        "fields": [
            {
                "field": f,
                "current": json.loads(json.dumps(getattr(posting, _FIELD_TO_ATTR[CriticalField(f)]), default=str)),
                "source_snippet": _snippet(posting.raw_text, str(getattr(posting, f, "") or "")),
            }
            for f in fields
        ],
    }
    while True:
        answer = interrupt(payload)
        fixes = (answer or {}).get("fields", {})
        unknown = set(fixes) - set(fields)
        if unknown:
            payload = payload | {"error": f"확인 대상이 아닌 항목이에요: {', '.join(sorted(unknown))}"}
            continue
        try:
            fixed = JobPosting.model_validate(posting.model_dump() | fixes)
            break
        except ValidationError as e:
            first = e.errors()[0]
            payload = payload | {"error": f"{'.'.join(map(str, first['loc']))}: {first['msg']}"}

    attempts = state.get("verify_attempts", 0) + 1
    remaining = (
        []
        if attempts >= MAX_VERIFY_ATTEMPTS
        else [f for f in fixed.meta.low_confidence_fields if f.value not in fields]
    )
    fixed = fixed.model_copy(update={"meta": fixed.meta.model_copy(update={"low_confidence_fields": remaining})})
    if state.get("posting_id"):
        deps.store.update_posting(state["posting_id"], fixed)
    return {
        "posting": fixed,
        "verify_attempts": attempts,
        "needs_human_check": remaining,
        "supported_verdict": "supported",
    }


# ──────────────────────────────────────────────
# 진단 (매칭과 보완 포인트는 자리표시자)
# ──────────────────────────────────────────────


def match_requirements_node(state: GraphState, deps: GraphDeps) -> dict:
    return {"matches": []}  # 자리표시자 (T9)


def score_node(state: GraphState, deps: GraphDeps) -> dict:
    matches = state.get("matches") or []
    gap = build_gap_report([m for m in matches if m.is_required], [m for m in matches if not m.is_required])
    tier = education_tier(state["posting"], deps.me)
    return {
        "gap_report": gap,
        "education_tier": tier,
        "fit_band": fit_band(state["eligibility"], tier, gap),
        "preferences": check_preferences(state["posting"], deps.prefs),
    }


def summarize_gap_node(state: GraphState, deps: GraphDeps) -> dict:
    return {"gap_summary": ""}  # 자리표시자 (T10)


# ──────────────────────────────────────────────
# 결정
# ──────────────────────────────────────────────


def _decide_payload(state: GraphState) -> dict:
    p, e, g = state["posting"], state["eligibility"], state.get("gap_report")
    return {
        "kind": "decide",
        "posting_id": state.get("posting_id"),
        "company": p.company,
        "title": p.title,
        "band": state["fit_band"].value,
        "fit_score": g.fit_score if g else None,
        "checks": [
            {"id": c.id, "kind": c.kind, "label": c.label, "result": c.result, "detail": c.detail} for c in e.checks
        ],
        "matches": [],
        "preferences": state["preferences"].summary if state.get("preferences") else "",
        "gap_summary": state.get("gap_summary", ""),
        "already_applied": [{"title": r.title, "status": r.status.value} for r in (state.get("already_applied") or [])],
        "deadline": p.deadline.isoformat() if p.deadline else None,
    }


def decide_node(state: GraphState, deps: GraphDeps) -> dict:
    payload = _decide_payload(state)
    holds = {c["id"] for c in payload["checks"] if c["result"] == "hold"}
    while True:
        answer = interrupt(payload) or {}
        decision = answer.get("decision")
        if decision not in DECISIONS:
            payload = payload | {"error": f"결정은 {', '.join(DECISIONS)} 중 하나여야 해요."}
            continue
        if decision != "override":
            return {"decision": decision}
        ids = set(answer.get("condition_ids") or [])
        if not ids or not ids <= holds:
            payload = payload | {"error": "보류 중인 조건만 인정할 수 있어요."}
            continue
        for cid in sorted(ids):
            deps.store.add_override(state["posting_id"], cid)
        return {"decision": "override", "overrides": sorted(ids)}


def draft_resume_node(state: GraphState, deps: GraphDeps) -> dict:
    return {"resume_draft": ""}  # 자리표시자 (T11)


def save_record_node(state: GraphState, deps: GraphDeps) -> dict:
    p, pid, e = state["posting"], state["posting_id"], state["eligibility"]
    if e.status == "ineligible":
        status, memo = ApplicationStatus.skipped, "지원 불가: " + "; ".join(e.reasons)
    else:
        status, memo = STATUS_BY_DECISION[state["decision"]], ""
    rec = ApplicationRecord(
        posting_id=pid,
        dedup_key=dedup_key(p.company, p.title),
        company=p.company,
        title=p.title,
        source_urls=deps.store.source_urls(pid),
        deadline=p.deadline,
        status=status,
        history=[StatusChange(status=status, memo=memo)],
        overrides=sorted(deps.store.overrides(pid)),
        judged_with=competency_fingerprint(deps.me, deps.db),
    )
    deps.store.save_record(rec)
    return {"application": rec, "end_reason": "saved"}


# ──────────────────────────────────────────────
# 분기 (수집, 결정)
# ──────────────────────────────────────────────


def route_after_normalize(state: GraphState) -> str:
    if state.get("end_reason"):
        return END
    return "parse_posting" if state.get("text_is_sufficient") else "collect_images"


def route_after_parse(state: GraphState) -> str:
    return END if state.get("end_reason") else "check_supported"


def route_after_decide(state: GraphState) -> str:
    return {"override": "check_eligibility", "apply": "draft_resume"}.get(state["decision"], "save_record")


# ──────────────────────────────────────────────
# 조립
# ──────────────────────────────────────────────


class FitGraph:
    """컴파일된 그래프와 체크포인터. thread_id로 멈춘 지점에서 이어 간다."""

    def __init__(self, app, conn: sqlite3.Connection):
        self.app = app
        self._conn = conn

    @staticmethod
    def config(thread_id: str) -> dict:
        return {"configurable": {"thread_id": thread_id}}

    def invoke(self, inputs: dict, config: dict) -> dict:
        return self.app.invoke(inputs, config)

    def resume(self, thread_id: str, value: dict) -> dict:
        return self.app.invoke(Command(resume=value), self.config(thread_id))

    def values(self, thread_id: str) -> dict:
        return dict(self.app.get_state(self.config(thread_id)).values)

    def close(self) -> None:
        self._conn.close()


def pending_interrupt(graph: FitGraph, thread_id: str) -> dict | None:
    st = graph.app.get_state(graph.config(thread_id))
    return st.interrupts[0].value if st.interrupts else None


def build_graph(deps: GraphDeps) -> FitGraph:
    g = StateGraph(GraphState)

    def bind(fn):
        return lambda state: fn(state, deps)

    def bind_gate(fn):
        return lambda state: fn(state, deps.gate)

    g.add_node("normalize_input", bind(normalize_node))
    g.add_node("collect_images", bind(collect_images_node))
    g.add_node("parse_posting", bind(parse_node))
    g.add_node("check_supported", bind_gate(check_supported_node))
    g.add_node("check_duplicate", bind_gate(check_duplicate_node))
    g.add_node("human_verify", bind(human_verify_node))
    g.add_node("check_eligibility", bind_gate(check_eligibility_node))
    g.add_node("match_requirements", bind(match_requirements_node))
    g.add_node("score", bind(score_node))
    g.add_node("summarize_gap", bind(summarize_gap_node))
    g.add_node("decide", bind(decide_node))
    g.add_node("draft_resume", bind(draft_resume_node))
    g.add_node("save_record", bind(save_record_node))

    g.add_edge(START, "normalize_input")
    g.add_conditional_edges("normalize_input", route_after_normalize, ["parse_posting", "collect_images", END])
    g.add_edge("collect_images", END)
    g.add_conditional_edges("parse_posting", route_after_parse, ["check_supported", END])
    g.add_conditional_edges("check_supported", route_after_supported, ["check_duplicate", "human_verify", END])
    g.add_conditional_edges("check_duplicate", route_after_duplicate, ["check_eligibility", "human_verify", END])
    g.add_edge("human_verify", "check_supported")
    g.add_conditional_edges("check_eligibility", route_after_eligibility, ["match_requirements", "save_record"])
    g.add_edge("match_requirements", "score")
    g.add_edge("score", "summarize_gap")
    g.add_edge("summarize_gap", "decide")
    g.add_conditional_edges("decide", route_after_decide, ["check_eligibility", "draft_resume", "save_record"])
    g.add_edge("draft_resume", "save_record")
    g.add_edge("save_record", END)

    deps.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(deps.checkpoint_path), check_same_thread=False)
    return FitGraph(g.compile(checkpointer=SqliteSaver(conn)), conn)
