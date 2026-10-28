"""요건 매칭 노드 (TASKS T9, AGENT_DESIGN 4.6, 7장)

1. 후보 고르기 (코드)
   - 기술 요건(skill, certificate, language): 그 기술을 쓴 프로젝트만. 없으면 LLM 없이 '없음'
   - 경험형 요건(domain, etc): 모든 프로젝트. 키워드로 못 찾는 경험을 놓치지 않기 위해 (DESIGN_DECISIONS D15)
   - 소프트 스킬: 판정하지 않음 (점수에서도 제외)
2. 판정 (LLM 1회): 요건마다 충족 정도 5단계와 근거 프로젝트 id
3. 재검사 (코드)
   - 후보 밖 근거는 버리고, 근거가 없으면 '없음'
   - 팀 프로젝트에서 그 기술이 내 기술 목록에 없으면 '팀에서 담당'까지
   - '조금 만져봄'으로 표시한 기술만 걸리면 '조금 만져봄'까지
   - LLM이 빠뜨린 요건은 '없음'
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from ..aliases import canonical, skills_in
from ..budget import LLMBudget
from ..llm import BaseLLM, Purpose
from ..schemas import CompetencyDB, JobPosting, MatchStrength, Project, Requirement, RequirementMatch
from ..scoring import STRENGTH_CREDIT

PROMPT_VERSION = "match-v1"
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "match_requirements.md"

SKILL_CATEGORIES = {"skill", "certificate", "language"}
EXPERIENCE_CATEGORIES = {"domain", "etc"}
ORDER: list[str] = ["없음", "조금 만져봄", "사용 경험", "팀에서 담당", "직접 구현"]


class Judgment(BaseModel):
    req_index: int
    strength: MatchStrength
    evidence_ids: list[str] = Field(default_factory=list)
    note: str = ""


class MatchJudgments(BaseModel):
    items: list[Judgment] = Field(default_factory=list)


@dataclass
class _Item:
    index: int
    requirement: Requirement
    is_required: bool
    keys: set[str]
    candidates: list[Project]


def _keys(r: Requirement) -> set[str]:
    return {canonical(k) for k in r.keywords} | skills_in(r.text)


def _skills(p: Project) -> set[str]:
    return {canonical(s) for s in p.skills}


def _touched(p: Project) -> set[str]:
    return {canonical(s) for s in p.touched_skills} - _skills(p)


def _candidates(r: Requirement, keys: set[str], db: CompetencyDB) -> list[Project]:
    if r.category == "soft":
        return []
    if r.category in EXPERIENCE_CATEGORIES:
        return list(db.projects)
    return [p for p in db.projects if keys & (_skills(p) | _touched(p))]


def _cap_for(p: Project, keys: set[str]) -> str:
    if keys and keys <= _touched(p):
        return "조금 만져봄"
    if p.team_size > 1 and not (keys & _skills(p)):
        return "팀에서 담당"
    return "직접 구현"


def _weaker(a: str, b: str) -> str:
    return a if ORDER.index(a) <= ORDER.index(b) else b


def _status(strength: str) -> str:
    credit = STRENGTH_CREDIT[strength]
    return "matched" if credit >= 0.6 else "partial" if credit > 0 else "missing"


def _render(items: list[_Item]) -> str:
    reqs = "\n".join(
        f"[{it.index}] ({'필수' if it.is_required else '우대'}) {it.requirement.text}"
        f" | 후보: {', '.join(p.id for p in it.candidates)}"
        for it in items
    )
    seen: dict[str, Project] = {p.id: p for it in items for p in it.candidates}
    projects = "\n\n".join(
        f"- id: {p.id}\n  이름: {p.name}\n  인원: {p.team_size}명\n  내 역할: {p.my_role}\n"
        f"  내가 한 일: {' / '.join(p.contributions) or '(없음)'}\n  내가 쓴 기술: {', '.join(p.skills) or '(없음)'}"
        for p in seen.values()
    )
    template = PROMPT_PATH.read_text(encoding="utf-8")
    return template.replace("{requirements}", reqs).replace("{projects}", projects)


def match_requirements(
    posting: JobPosting,
    db: CompetencyDB,
    *,
    llm: BaseLLM,
    budget: LLMBudget | None = None,
    purpose: Purpose = "new_analysis",
) -> list[RequirementMatch]:
    pairs = [(r, True) for r in posting.required] + [(r, False) for r in posting.preferred]
    if not pairs:  # 필수와 우대가 모두 없으면 담당업무로 (Spec 5.2)
        pairs = [(Requirement(text=t, category="etc"), True) for t in posting.responsibilities]

    items = []
    for i, (r, required) in enumerate(pairs):
        keys = _keys(r)
        items.append(_Item(i, r, required, keys, _candidates(r, keys, db)))

    pending = [it for it in items if it.candidates]
    judged: dict[int, Judgment] = {}
    if pending:
        out = llm.structured(_render(pending), MatchJudgments, purpose=purpose, budget=budget)
        judged = {j.req_index: j for j in out.items}

    results: list[RequirementMatch] = []
    for it in items:
        j = judged.get(it.index)
        cand_ids = {p.id for p in it.candidates}
        evidence = [e for e in dict.fromkeys(j.evidence_ids) if e in cand_ids] if j else []
        strength = j.strength if (j and evidence) else "없음"
        if strength != "없음":
            best_cap = max((_cap_for(p, it.keys) for p in it.candidates if p.id in evidence), key=ORDER.index)
            strength = _weaker(strength, best_cap)
        results.append(
            RequirementMatch(
                requirement=it.requirement,
                is_required=it.is_required,
                status=_status(strength),
                strength=strength,
                evidence_project_ids=evidence if strength != "없음" else [],
                note=j.note if j else "",
            )
        )
    return results
