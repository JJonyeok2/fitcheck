"""U2 시간 재기 도우미 테스트 (eval/README 2번, TASKS U2)

- start <공고id>: 시작 시각을 적어 둔다. 이미 재는 중이면 거부
- stop: 끝 시각과 걸린 분을 time_log.csv에 한 줄 추가. 파일이 없으면 머리글부터
- 재는 중이 아닌데 stop이면 거부
- 같은 공고를 같은 phase로 두 번 재면 거부 (실수로 다시 읽으면 시간이 짧게 나온다)
- 기본 memo는 '설계 참여 후 측정' (늦게 잰 사실을 기록에 남긴다)
"""

from __future__ import annotations

import csv
from datetime import datetime

import pytest

from fitcheck import timelog

T0 = datetime(2026, 10, 28, 21, 40)


@pytest.fixture
def eval_dir(tmp_path):
    return tmp_path


def rows(eval_dir):
    with open(eval_dir / "time_log.csv", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def test_start_then_stop_writes_row(eval_dir):
    timelog.start("t01", eval_dir=eval_dir, now=T0)
    msg = timelog.stop(eval_dir=eval_dir, now=datetime(2026, 10, 28, 21, 52, 30))
    r = rows(eval_dir)
    assert len(r) == 1
    assert r[0]["phase"] == "before" and r[0]["posting_id"] == "t01"
    assert r[0]["started_at"] == "2026-10-28 21:40:00" and r[0]["ended_at"] == "2026-10-28 21:52:30"
    assert r[0]["minutes"] == "12.5"
    assert r[0]["memo"] == "설계 참여 후 측정"
    assert "12.5분" in msg


def test_header_matches_template(eval_dir):
    timelog.start("t01", eval_dir=eval_dir, now=T0)
    timelog.stop(eval_dir=eval_dir, now=T0.replace(minute=45))
    header = (eval_dir / "time_log.csv").read_text(encoding="utf-8").splitlines()[0]
    template = (timelog.ROOT / "eval" / "time_log_template.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header == template


def test_second_row_appends_without_header(eval_dir):
    for i, pid in enumerate(["t01", "t02"]):
        timelog.start(pid, eval_dir=eval_dir, now=T0.replace(hour=20 + i))
        timelog.stop(eval_dir=eval_dir, now=T0.replace(hour=20 + i, minute=50))
    assert [r["posting_id"] for r in rows(eval_dir)] == ["t01", "t02"]


def test_start_while_running_is_rejected(eval_dir):
    timelog.start("t01", eval_dir=eval_dir, now=T0)
    with pytest.raises(timelog.TimelogError, match="t01"):
        timelog.start("t02", eval_dir=eval_dir, now=T0)


def test_stop_without_start_is_rejected(eval_dir):
    with pytest.raises(timelog.TimelogError):
        timelog.stop(eval_dir=eval_dir, now=T0)


def test_same_posting_same_phase_twice_is_rejected(eval_dir):
    timelog.start("t01", eval_dir=eval_dir, now=T0)
    timelog.stop(eval_dir=eval_dir, now=T0.replace(minute=50))
    with pytest.raises(timelog.TimelogError, match="이미"):
        timelog.start("t01", eval_dir=eval_dir, now=T0.replace(hour=22))
    timelog.start("t01", phase="after", eval_dir=eval_dir, now=T0.replace(hour=22))  # 완성 후 측정은 된다


def test_cancel_discards_running(eval_dir):
    timelog.start("t01", eval_dir=eval_dir, now=T0)
    timelog.cancel(eval_dir=eval_dir)
    assert not (eval_dir / "time_log.csv").exists()
    timelog.start("t01", eval_dir=eval_dir, now=T0)  # 취소했으니 다시 시작 가능


def test_cli(eval_dir, monkeypatch, capsys):
    monkeypatch.setenv("FIT_EVAL_DIR", str(eval_dir))
    assert timelog.main(["start", "t03"]) == 0
    assert timelog.main(["start", "t04"]) == 1, "재는 중이면 실패 코드"
    assert timelog.main(["stop", "--memo", "중간에 전화"]) == 0
    assert rows(eval_dir)[0]["memo"] == "중간에 전화"
    assert "t03" in capsys.readouterr().out


def test_append_after_hand_edit_without_final_newline(eval_dir):
    """메모장으로 마지막 줄을 지우면 머리글 끝 줄바꿈까지 사라질 수 있다. 그래도 새 줄은 따로 들어가야 한다"""
    header = ",".join(timelog.FIELDS)
    (eval_dir / "time_log.csv").write_text(header, encoding="utf-8")  # 줄바꿈 없음
    timelog.start("t01", eval_dir=eval_dir, now=T0)
    timelog.stop(eval_dir=eval_dir, now=T0.replace(minute=45))
    r = rows(eval_dir)
    assert len(r) == 1 and r[0]["posting_id"] == "t01" and r[0]["memo"] == "설계 참여 후 측정"
