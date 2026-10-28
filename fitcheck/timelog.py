"""시간 재기 도우미 — 도입 전과 후의 공고 처리 시간을 time_log.csv에 기록 (TASKS U2, eval/README 2번)

사용법 (저장소 폴더에서)
  uv run fitcheck-time start t01      공고 t01 읽기 시작
  uv run fitcheck-time stop           끝. 걸린 분을 한 줄 기록
  uv run fitcheck-time stop --memo "중간에 전화"
  uv run fitcheck-time cancel         잘못 시작했을 때 버리기
  uv run fitcheck-time status         지금 재는 중인지
  --phase after                       완성 후 측정 (기본 before)

- 재는 중인 상태는 eval/.timer.json에, 기록은 eval/time_log.csv에 둔다 (둘 다 Git 제외)
- 같은 공고를 같은 단계로 두 번 재지 않는다. 다시 읽으면 내용을 알아서 시간이 짧게 나온다
- 기본 memo '설계 참여 후 측정': 판정 기준을 함께 설계한 뒤에 쟀다는 사실을 남긴다
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIELDS = ["phase", "posting_id", "started_at", "ended_at", "minutes", "steps_done", "memo"]
STEPS = "공고 읽기 + 지원 여부 판단 + 이력서 프로젝트 순서 정하기"
DEFAULT_MEMO = "설계 참여 후 측정"
FMT = "%Y-%m-%d %H:%M:%S"


class TimelogError(Exception):
    pass


def _dir(eval_dir: str | Path | None) -> Path:
    return Path(eval_dir or os.environ.get("FIT_EVAL_DIR") or ROOT / "eval")


def _timer(d: Path) -> Path:
    return d / ".timer.json"


def _log(d: Path) -> Path:
    return d / "time_log.csv"


def _running(d: Path) -> dict | None:
    p = _timer(d)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _done(d: Path, posting_id: str, phase: str) -> bool:
    if not _log(d).exists():
        return False
    with open(_log(d), encoding="utf-8", newline="") as f:
        return any(r["posting_id"] == posting_id and r["phase"] == phase for r in csv.DictReader(f))


def start(posting_id: str, *, phase: str = "before", eval_dir=None, now: datetime | None = None) -> str:
    d = _dir(eval_dir)
    run = _running(d)
    if run:
        raise TimelogError(f"{run['posting_id']}을 재는 중이에요. stop이나 cancel 먼저 해 주세요.")
    if _done(d, posting_id, phase):
        raise TimelogError(f"{posting_id}는 {phase}로 이미 쟀어요. 다른 공고로 해 주세요.")
    d.mkdir(parents=True, exist_ok=True)
    at = (now or datetime.now()).replace(microsecond=0)
    _timer(d).write_text(
        json.dumps({"posting_id": posting_id, "phase": phase, "started_at": at.strftime(FMT)}), encoding="utf-8"
    )
    return f"{posting_id} 시작 ({at:%H:%M:%S}). 다 하면 stop"


def stop(*, memo: str = DEFAULT_MEMO, eval_dir=None, now: datetime | None = None) -> str:
    d = _dir(eval_dir)
    run = _running(d)
    if not run:
        raise TimelogError("재는 중인 공고가 없어요. start 먼저 해 주세요.")
    began = datetime.strptime(run["started_at"], FMT)
    ended = (now or datetime.now()).replace(microsecond=0)
    minutes = round((ended - began).total_seconds() / 60, 1)
    new = not _log(d).exists()
    with open(_log(d), "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(
            {
                "phase": run["phase"],
                "posting_id": run["posting_id"],
                "started_at": run["started_at"],
                "ended_at": ended.strftime(FMT),
                "minutes": minutes,
                "steps_done": STEPS,
                "memo": memo,
            }
        )
    _timer(d).unlink()
    return f"{run['posting_id']} 끝: {minutes}분 기록"


def cancel(*, eval_dir=None) -> str:
    d = _dir(eval_dir)
    run = _running(d)
    if not run:
        return "재는 중인 공고가 없어요."
    _timer(d).unlink()
    return f"{run['posting_id']} 측정을 버렸어요."


def status(*, eval_dir=None) -> str:
    run = _running(_dir(eval_dir))
    return f"{run['posting_id']} 재는 중 (시작 {run['started_at']})" if run else "재는 중인 공고가 없어요."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fitcheck-time", description="공고 처리 시간 재기")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("posting_id")
    s.add_argument("--phase", choices=["before", "after"], default="before")
    e = sub.add_parser("stop")
    e.add_argument("--memo", default=DEFAULT_MEMO)
    sub.add_parser("cancel")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "start":
            print(start(args.posting_id, phase=args.phase))
        elif args.cmd == "stop":
            print(stop(memo=args.memo))
        elif args.cmd == "cancel":
            print(cancel())
        else:
            print(status())
        return 0
    except TimelogError as err:
        print(err, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
