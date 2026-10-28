"""T3 완료 조건 테스트 — 저장소 (SQLite)

TASKS.md T3, Spec 8장, AGENT_DESIGN 4.3
- 공고 파싱 결과, 지원 기록, 보류 인정을 저장하고 파일을 다시 열어도 그대로 읽힌다
- 같은 원문은 한 번만 저장하고, 다른 플랫폼 URL만 덧붙인다
- 회사명 표기가 달라도 같은 공고(dedup_key)와 같은 회사 지원 이력을 찾는다
- 스키마 버전을 기록하고, 마이그레이션은 버전마다 한 번만 돈다
- 파싱 캐시도 같은 DB 파일을 쓰고, DB 파일(data/)은 Git에 올라가지 않는다
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from fitcheck.schemas import (
    ApplicationRecord,
    ApplicationStatus,
    ExtractionMeta,
    JobPosting,
    StatusChange,
)
from fitcheck.store import DEFAULT_DB, MIGRATIONS, Store
from fitcheck.tracking import dedup_key

ROOT = Path(__file__).resolve().parent.parent


def posting(company="(주)도슨티", title="[신입] AI-native Builder 채용", raw="원문 A", url=None) -> JobPosting:
    return JobPosting(
        company=company,
        title=title,
        location="서울 관악구",
        deadline=date(2026, 11, 30),
        raw_text=raw,
        meta=ExtractionMeta(input_type="url" if url else "text", source_url=url),
    )


@pytest.fixture
def db(tmp_path) -> Path:
    return tmp_path / "fitcheck.sqlite"


# ── 공고 ─────────────────────────────────────────────────


def test_posting_round_trip_after_reopen(db):
    p = posting()
    pid = Store(db).save_posting(p, content_hash="h1", source_url="https://a.example/1")
    again = Store(db).get_posting(pid)
    assert again == p


def test_same_content_saved_once_and_urls_merged(db):
    s = Store(db)
    a = s.save_posting(posting(), content_hash="h1", source_url="https://saramin.example/1")
    b = s.save_posting(posting(), content_hash="h1", source_url="https://wanted.example/9")
    c = s.save_posting(posting(), content_hash="h1", source_url="https://wanted.example/9")
    assert a == b == c
    assert s.count_postings() == 1
    assert s.source_urls(a) == ["https://saramin.example/1", "https://wanted.example/9"]


def test_find_posting_by_hash(db):
    s = Store(db)
    pid = s.save_posting(posting(), content_hash="h1")
    assert s.find_posting_by_hash("h1") == pid
    assert s.find_posting_by_hash("없음") is None


def test_missing_posting_returns_none(db):
    assert Store(db).get_posting("p_없음") is None


# ── 지원 기록 ────────────────────────────────────────────


def record(pid: str, company="(주)도슨티", title="[신입] AI-native Builder 채용", status=ApplicationStatus.interested):
    return ApplicationRecord(
        posting_id=pid, dedup_key=dedup_key(company, title), company=company, title=title, status=status
    )


def test_record_round_trip_with_history(db):
    s = Store(db)
    pid = s.save_posting(posting(), content_hash="h1")
    rec = record(pid)
    rec.history.append(StatusChange(status=ApplicationStatus.applied, at=datetime(2026, 10, 28, 18, 0), memo="원티드"))
    rec.status = ApplicationStatus.applied
    s.save_record(rec)
    got = Store(db).get_record(pid)
    assert got == rec


def test_update_status_appends_history_and_persists(db):
    s = Store(db)
    pid = s.save_posting(posting(), content_hash="h1")
    s.save_record(record(pid))
    s.update_status(pid, ApplicationStatus.applied, memo="사람인")
    s.update_status(pid, ApplicationStatus.document_passed)
    got = Store(db).get_record(pid)
    assert got.status == ApplicationStatus.document_passed
    assert [h.status for h in got.history] == [ApplicationStatus.applied, ApplicationStatus.document_passed]


def test_update_status_of_unknown_record_fails(db):
    with pytest.raises(KeyError):
        Store(db).update_status("p_없음", ApplicationStatus.applied)


def test_find_by_dedup_key_across_spellings(db):
    s = Store(db)
    pid = s.save_posting(posting(), content_hash="h1")
    s.save_record(record(pid))
    found = s.find_record_by_dedup_key(dedup_key("도슨티", "AI-native Builder"))
    assert found is not None and found.posting_id == pid


def test_already_applied_company(db):
    s = Store(db)
    p1 = s.save_posting(posting(title="프론트엔드 개발자", raw="원문 B"), content_hash="h2")
    s.save_record(record(p1, title="프론트엔드 개발자", status=ApplicationStatus.rejected))
    hits = s.already_applied("㈜도슨티")
    assert [h.posting_id for h in hits] == [p1]
    assert s.already_applied("다른회사") == []


def test_list_records_filter_by_status(db):
    s = Store(db)
    a = s.save_posting(posting(raw="1"), content_hash="a")
    b = s.save_posting(posting(title="다른 직무", raw="2"), content_hash="b")
    s.save_record(record(a))
    s.save_record(record(b, title="다른 직무", status=ApplicationStatus.applied))
    assert {r.posting_id for r in s.list_records()} == {a, b}
    assert [r.posting_id for r in s.list_records(status=ApplicationStatus.applied)] == [b]


# ── 보류 인정 ────────────────────────────────────────────


def test_overrides_accumulate_without_duplicates(db):
    s = Store(db)
    pid = s.save_posting(posting(), content_hash="h1")
    s.add_override(pid, "자격증:정보처리기사")
    s.add_override(pid, "어학:영어 TOEIC 800")
    s.add_override(pid, "자격증:정보처리기사")
    assert Store(db).overrides(pid) == {"자격증:정보처리기사", "어학:영어 TOEIC 800"}


# ── 스키마 버전, 마이그레이션 ────────────────────────────


def test_schema_version_is_latest_and_stable(db):
    s = Store(db)
    assert s.schema_version() == len(MIGRATIONS)
    Store(db)
    assert Store(db).schema_version() == len(MIGRATIONS)


def test_migration_runs_once_per_version(db, monkeypatch):
    Store(db)
    runs = []

    def extra(conn):
        runs.append(1)
        conn.execute("CREATE TABLE IF NOT EXISTS extra (x INTEGER)")

    monkeypatch.setattr("fitcheck.store.MIGRATIONS", [*MIGRATIONS, extra])
    Store(db)
    Store(db)
    assert runs == [1], "새 버전 마이그레이션은 한 번만 실행되어야 한다"


# ── 파싱 캐시, 파일 위치 ─────────────────────────────────


def test_parse_cache_shares_same_db_file(db):
    s = Store(db)
    s.parse_cache.put("h1", {"company": "A"})
    assert Store(db).parse_cache.get("h1") == {"company": "A"}
    assert {p.name for p in db.parent.iterdir()} == {db.name}, "DB 파일은 하나여야 한다"


def test_default_db_is_under_ignored_data_dir():
    assert DEFAULT_DB == ROOT / "data" / "fitcheck.sqlite"
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "data/" in ignored
