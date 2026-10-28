"""저장소 — 공고, 지원 기록, 보류 인정, 파싱 캐시를 SQLite 파일 하나에

Spec 8장 · AGENT_DESIGN 4.3 · TASKS T3

- 기본 위치는 data/fitcheck.sqlite (Git 제외)
- 공고 id는 원문 해시에서 만든다. 같은 원문이면 같은 id라서 한 번만 저장되고, 출처 URL만 덧붙는다
- 모델은 Pydantic JSON으로 저장하고, 조회에 쓰는 값(해시, dedup_key, 상태)만 따로 열로 둔다
- 스키마가 바뀌면 MIGRATIONS 끝에 함수를 추가한다. 버전마다 한 번만 실행된다
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .budget import ParseCache
from .schemas import ApplicationRecord, ApplicationStatus, JobPosting, StatusChange
from .tracking import already_applied_to, dedup_key

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "fitcheck.sqlite"


# ──────────────────────────────────────────────
# 마이그레이션 (순서대로, 끝에만 추가한다)
# ──────────────────────────────────────────────


def _v1_initial(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE postings (
            id           TEXT PRIMARY KEY,
            content_hash TEXT UNIQUE NOT NULL,
            dedup_key    TEXT NOT NULL,
            posting_json TEXT NOT NULL,
            source_urls  TEXT NOT NULL DEFAULT '[]',
            created_at   TEXT NOT NULL
        );
        CREATE INDEX idx_postings_dedup ON postings(dedup_key);

        CREATE TABLE records (
            posting_id  TEXT PRIMARY KEY,
            dedup_key   TEXT NOT NULL,
            status      TEXT NOT NULL,
            record_json TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        CREATE INDEX idx_records_dedup ON records(dedup_key);
        CREATE INDEX idx_records_status ON records(status);

        CREATE TABLE overrides (
            posting_id   TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            at           TEXT NOT NULL,
            PRIMARY KEY (posting_id, condition_id)
        );
        """
    )


Migration = Callable[[sqlite3.Connection], None]
MIGRATIONS: list[Migration] = [_v1_initial]


# ──────────────────────────────────────────────
# 저장소
# ──────────────────────────────────────────────


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.execute("PRAGMA foreign_keys = ON")
        self._migrate()
        self.parse_cache = ParseCache(self.path)

    def close(self) -> None:
        self.parse_cache.db.close()
        self.db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── 스키마 ────────────────────────────────────

    def _migrate(self) -> None:
        self.db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        current = self.schema_version()
        for version, step in enumerate(MIGRATIONS, start=1):
            if version <= current:
                continue
            with self.db:  # 한 버전은 통째로 적용되거나 통째로 안 된다
                step(self.db)
                self.db.execute("DELETE FROM schema_version")
                self.db.execute("INSERT INTO schema_version VALUES (?)", (version,))

    def schema_version(self) -> int:
        row = self.db.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return int(row[0] or 0)

    # ── 공고 ──────────────────────────────────────

    @staticmethod
    def posting_id(content_hash: str) -> str:
        return f"p_{content_hash[:16]}"

    def save_posting(self, posting: JobPosting, content_hash: str, source_url: str | None = None) -> str:
        """같은 원문(해시)이면 새로 저장하지 않고 출처 URL만 덧붙인다. 공고 id를 돌려준다."""
        existing = self.find_posting_by_hash(content_hash)
        if existing:
            if source_url:
                self.add_source_url(existing, source_url)
            return existing
        pid = self.posting_id(content_hash)
        with self.db:
            self.db.execute(
                "INSERT INTO postings VALUES (?, ?, ?, ?, ?, ?)",
                (
                    pid,
                    content_hash,
                    dedup_key(posting.company, posting.title),
                    posting.model_dump_json(),
                    json.dumps([source_url] if source_url else [], ensure_ascii=False),
                    _now(),
                ),
            )
        return pid

    def update_posting(self, posting_id: str, posting: JobPosting) -> None:
        """사람이 고친 값으로 공고를 바꾼다 (원문 해시와 출처 URL은 그대로)"""
        with self.db:
            self.db.execute(
                "UPDATE postings SET posting_json = ?, dedup_key = ? WHERE id = ?",
                (posting.model_dump_json(), dedup_key(posting.company, posting.title), posting_id),
            )

    def get_posting(self, posting_id: str) -> JobPosting | None:
        row = self.db.execute("SELECT posting_json FROM postings WHERE id = ?", (posting_id,)).fetchone()
        return JobPosting.model_validate_json(row[0]) if row else None

    def find_posting_by_hash(self, content_hash: str) -> str | None:
        row = self.db.execute("SELECT id FROM postings WHERE content_hash = ?", (content_hash,)).fetchone()
        return row[0] if row else None

    def source_urls(self, posting_id: str) -> list[str]:
        row = self.db.execute("SELECT source_urls FROM postings WHERE id = ?", (posting_id,)).fetchone()
        return json.loads(row[0]) if row else []

    def add_source_url(self, posting_id: str, url: str) -> None:
        urls = self.source_urls(posting_id)
        if url in urls:
            return
        with self.db:
            self.db.execute(
                "UPDATE postings SET source_urls = ? WHERE id = ?",
                (json.dumps([*urls, url], ensure_ascii=False), posting_id),
            )

    def count_postings(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM postings").fetchone()[0])

    # ── 지원 기록 ─────────────────────────────────

    def save_record(self, record: ApplicationRecord) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO records VALUES (?, ?, ?, ?, ?)",
                (record.posting_id, record.dedup_key, record.status.value, record.model_dump_json(), _now()),
            )

    def get_record(self, posting_id: str) -> ApplicationRecord | None:
        row = self.db.execute("SELECT record_json FROM records WHERE posting_id = ?", (posting_id,)).fetchone()
        return ApplicationRecord.model_validate_json(row[0]) if row else None

    def update_status(self, posting_id: str, status: ApplicationStatus, memo: str = "") -> ApplicationRecord:
        rec = self.get_record(posting_id)
        if rec is None:
            raise KeyError(f"지원 기록이 없어요: {posting_id}")
        rec.status = status
        rec.history.append(StatusChange(status=status, memo=memo))
        self.save_record(rec)
        return rec

    def list_records(self, status: ApplicationStatus | None = None) -> list[ApplicationRecord]:
        if status is None:
            rows = self.db.execute("SELECT record_json FROM records ORDER BY updated_at").fetchall()
        else:
            rows = self.db.execute(
                "SELECT record_json FROM records WHERE status = ? ORDER BY updated_at", (status.value,)
            ).fetchall()
        return [ApplicationRecord.model_validate_json(r[0]) for r in rows]

    def find_record_by_dedup_key(self, key: str) -> ApplicationRecord | None:
        row = self.db.execute("SELECT record_json FROM records WHERE dedup_key = ? LIMIT 1", (key,)).fetchone()
        return ApplicationRecord.model_validate_json(row[0]) if row else None

    def already_applied(self, company: str) -> list[ApplicationRecord]:
        """포지션이 달라도 같은 회사에 지원한 기록 (재지원 판단용)"""
        return already_applied_to(company, self.list_records())

    # ── 보류 인정 ─────────────────────────────────

    def add_override(self, posting_id: str, condition_id: str) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO overrides VALUES (?, ?, ?)", (posting_id, condition_id, _now()))

    def overrides(self, posting_id: str) -> set[str]:
        rows = self.db.execute("SELECT condition_id FROM overrides WHERE posting_id = ?", (posting_id,)).fetchall()
        return {r[0] for r in rows}
