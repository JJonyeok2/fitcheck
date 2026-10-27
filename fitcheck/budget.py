"""LLM 비용 관리 — 공고당 호출 상한, 이미지 조각 상한, 파싱 결과 캐시

원칙
- 같은 원문(정규화 후 해시가 같음)은 다시 파싱하지 않고 저장된 결과를 쓴다.
  URL이 달라도 원문이 같으면 재사용된다 (플랫폼 간 중복 공고).
- 공고 1건에 쓸 수 있는 LLM 호출 수에 상한을 둔다. 넘으면 그 자리에서 멈추고 사람 확인으로 보낸다.
- 이미지 공고는 조각 수만큼 비전 호출이 나가므로 조각 수에도 상한을 둔다.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

# 공고 1건당 호출 예산 (정상 경로 기준 예상치에 여유를 둔 값)
#   텍스트 공고: 파싱 1 + 매칭 1 + 보완 포인트 1 + 이력서 방향 1 = 4
#   이미지 공고: 위 4 + 비전 조각 수
#   파싱 재시도 최대 2회
MAX_IMAGE_CHUNKS = 8
MAX_PARSE_RETRIES = 2
MAX_LLM_CALLS_PER_POSTING = 4 + MAX_IMAGE_CHUNKS + MAX_PARSE_RETRIES   # 14

_WS = re.compile(r"\s+")


def content_hash(text: str) -> str:
    """공백, 줄바꿈 차이는 무시하고 같은 원문이면 같은 값"""
    return hashlib.sha256(_WS.sub(" ", text).strip().encode()).hexdigest()[:20]


class BudgetExceeded(RuntimeError):
    pass


class LLMBudget:
    """노드에서 LLM을 부르기 전에 spend()를 호출한다. 상한을 넘으면 BudgetExceeded."""

    def __init__(self, used: int = 0, limit: int = MAX_LLM_CALLS_PER_POSTING):
        self.used, self.limit = used, limit

    def spend(self, n: int = 1, what: str = "") -> int:
        if self.used + n > self.limit:
            raise BudgetExceeded(f"LLM 호출 상한 {self.limit}회 초과 ({what}). 사람 확인으로 전환")
        self.used += n
        return self.used


def cap_image_chunks(chunks: list[str]) -> tuple[list[str], bool]:
    """조각이 너무 많으면 앞에서부터 상한까지만 쓴다. 잘렸는지 여부를 함께 반환 (잘리면 추출 신뢰도 낮음 처리)."""
    return chunks[:MAX_IMAGE_CHUNKS], len(chunks) > MAX_IMAGE_CHUNKS


class ParseCache:
    """원문 해시 → 파싱 결과(JSON). 스키마 버전이 바뀌면 예전 캐시는 쓰지 않는다."""

    SCHEMA_VERSION = "1"

    def __init__(self, path: str | Path = "data/parse_cache.sqlite"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS parse_cache (hash TEXT, ver TEXT, payload TEXT, PRIMARY KEY (hash, ver))"
        )

    def get(self, h: str) -> dict | None:
        row = self.db.execute(
            "SELECT payload FROM parse_cache WHERE hash = ? AND ver = ?", (h, self.SCHEMA_VERSION)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, h: str, posting_json: dict) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO parse_cache VALUES (?, ?, ?)",
            (h, self.SCHEMA_VERSION, json.dumps(posting_json, ensure_ascii=False)),
        )
        self.db.commit()
