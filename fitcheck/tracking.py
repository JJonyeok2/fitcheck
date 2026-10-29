"""지원 현황 관리 보조 — 중복 공고 묶기, 역량 변경 시 재판정 대상 찾기"""

from __future__ import annotations

import hashlib
import json
import re

from .schemas import ApplicationRecord, ApplicationStatus, CompetencyDB, UserConstraints

_COMPANY_NOISE = re.compile(r"\(주\)|㈜|주식회사|\(유\)|유한회사|inc\.?|corp\.?|co\.,?\s*ltd\.?", re.I)
_TITLE_NOISE = re.compile(r"\[[^\]]*\]|\([^)]*\)|【[^】]*】|채용|모집|공고|신입|경력|정규직|인턴", re.I)
_SPACES = re.compile(r"[\s\-_/·,.|]+")


def dedup_key(company: str, title: str) -> str:
    """플랫폼마다 다른 표기('(주)도슨티 [신입] AI Builder 채용' vs '도슨티 AI-native Builder')를 같은 키로 모은다.
    직무명이 크게 다르면 다른 공고로 본다 (같은 회사의 다른 포지션을 합치지 않기 위해)."""
    c = _SPACES.sub("", _COMPANY_NOISE.sub("", company)).lower()
    t = _SPACES.sub("", _TITLE_NOISE.sub("", title)).lower()
    return f"{c}::{t}"


def merge_duplicate(existing: ApplicationRecord, new_url: str | None) -> ApplicationRecord:
    if new_url and new_url not in existing.source_urls:
        existing.source_urls.append(new_url)
    return existing


def already_applied_to(company: str, records: list[ApplicationRecord]) -> list[ApplicationRecord]:
    """같은 회사에 이전 지원 기록이 있는지 (포지션이 달라도). 재지원 판단용."""
    key = _SPACES.sub("", _COMPANY_NOISE.sub("", company)).lower()
    done = {
        ApplicationStatus.applied,
        ApplicationStatus.document_passed,
        ApplicationStatus.interview,
        ApplicationStatus.offer,
        ApplicationStatus.rejected,
    }
    return [r for r in records if r.dedup_key.split("::")[0] == key and r.status in done]


def competency_fingerprint(me: UserConstraints, db: CompetencyDB, profile=None) -> str:
    """판정에 영향을 주는 내 정보의 지문. 자격증 취득, 프로젝트 추가 등으로 바뀌면 값이 달라진다.

    profile(이력서 구조)을 주면 교육 이수, 취업 우대사항, 희망 조건 같은 판정 항목도 넣는다.
    한 방향 해시라 민감 항목이 들어가도 값을 되돌릴 수 없다.
    """
    extra = profile.judgement_payload() if profile is not None else None
    payload = json.dumps(
        {"me": me.model_dump(mode="json"), "db": db.model_dump(mode="json"), "profile": extra},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def needs_rejudge(records: list[ApplicationRecord], fingerprint: str) -> list[ApplicationRecord]:
    """아직 결론이 안 난 공고 중 예전 역량으로 판정한 것"""
    return [r for r in records if is_open_for_rejudge(r, fingerprint)]


OPEN_STATES = {ApplicationStatus.interested, ApplicationStatus.on_hold, ApplicationStatus.skipped}


def is_open_for_rejudge(record: ApplicationRecord, fingerprint: str) -> bool:
    """결론이 안 났고(관심, 보류, 미지원) 예전 내 정보로 판정한 기록. 지원 이후 단계는 건드리지 않는다."""
    return not record.imported and record.status in OPEN_STATES and record.judged_with != fingerprint
