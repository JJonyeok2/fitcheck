"""터미널 명령 — fitcheck analyze / resume / show (TASKS T7)

예시
  fitcheck analyze --file 공고.txt          붙여넣은 공고를 파일로
  fitcheck analyze --url https://...        공고 주소
  fitcheck resume <thread> --decision apply|hold|skip
  fitcheck resume <thread> --override "자격증:정보처리기사"
  fitcheck resume <thread> --fix location="서울 강남구"
  fitcheck show <thread>

데이터 위치: FIT_DATA_DIR (기본은 저장소의 data/)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import date
from pathlib import Path

from .config import EXAMPLE, ROOT, load_env, load_full_profile, profile_path
from .graph import FitGraph, GraphDeps, build_graph, pending_interrupt
from .llm import BaseLLM, LLMError, UsageLedger, get_llm
from .profile import ProfileError
from .scoring import percent
from .store import Store

END_MESSAGES = {
    "unsupported": "IT 직무 공고가 아니라서 분석하지 않았어요.",
    "saved": "저장했어요.",
}


def _data_dir() -> Path:
    return Path(os.environ.get("FIT_DATA_DIR") or ROOT / "data")


def _make_llm(ledger: UsageLedger) -> BaseLLM:
    return get_llm(ledger)


def _today() -> date:
    return date.today()


def _open() -> tuple[FitGraph, GraphDeps]:
    load_env()
    d = _data_dir()
    if profile_path().resolve() == EXAMPLE.resolve():
        print(
            "! 내 정보 파일(competencies.local.yaml)이 없어서 공개용 예시 정보로 판정해요. "
            "결과는 내 이력과 상관없어요. competencies.example.yaml을 복사해 내 정보로 바꿔 주세요.",
            file=sys.stderr,
        )
    profile = load_full_profile()
    today = _today()
    me, prefs, db = profile.constraints(today), profile.preferences(), profile.competency_db()
    ledger = UsageLedger(d / "usage.sqlite")
    deps = GraphDeps(
        store=Store(d / "fitcheck.sqlite"),
        me=me,
        prefs=prefs,
        db=db,
        profile=profile,
        llm=_make_llm(ledger),
        today=today,
        checkpoint_path=d / "checkpoints.sqlite",
    )
    return build_graph(deps), deps


def _duplicate_message(v: dict, deps: GraphDeps) -> str:
    rec = deps.store.get_record(v["posting_id"]) if v.get("posting_id") else None
    status = f" (지금 상태: {rec.status.value})" if rec else ""
    added = " 이 주소를 기존 기록에 덧붙였어요." if v.get("input_url") else ""
    return f"이미 분석한 공고예요{status}.{added} 내 정보가 바뀌면 다시 넣었을 때 다시 판정해요."


STRENGTH_ORDER = ["직접 구현", "팀에서 담당", "사용 경험", "조금 만져봄", "없음"]


def _print_matches(matches: list[dict]) -> None:
    from .schemas import Requirement
    from .scoring import is_education_requirement

    for m in matches:
        kind = "필수" if m["is_required"] else "우대"
        text = m["text"] if len(m["text"]) <= 48 else m["text"][:47] + "…"
        if is_education_requirement(Requirement(text=m["text"], category=m["category"])):
            print(f"  · {kind} {text}  → 학력 조건이라 위 자격 칩에서 판정")
            continue
        if m["category"] == "soft":
            print(f"  · {kind} {text}  → 소프트 스킬이라 점수에서 제외")
            continue
        evidence = f"  [{', '.join(m['evidence'])}]" if m["evidence"] else ""
        print(f"  · {kind} {m['strength'] or '없음'}  {text}{evidence}")


def _report(graph: FitGraph, thread: str, deps: GraphDeps, debug: bool = False) -> None:
    wait = pending_interrupt(graph, thread)
    v = graph.values(thread)
    if debug:
        print(f"[debug] LLM 호출 {v.get('llm_calls', 0)}회")
        if v.get("gap_summary_refined") and v.get("gap_summary_note"):
            print(f"[debug] 보완 포인트는 규칙 문장: {v['gap_summary_note']}")
    if wait is None:
        reason = v.get("end_reason")
        if reason == "duplicate":
            msg = _duplicate_message(v, deps)
        else:
            msg = v.get("error") or v.get("unsupported_reason") or END_MESSAGES.get(reason, "끝났어요.")
        print(msg)
        if reason == "saved" and v.get("application"):
            print(f"  상태: {v['application'].status.value}")
        return

    if wait.get("error"):
        print(f"  ! {wait['error']}")
    if wait["kind"] == "verify":
        print(f"확인이 필요해요  thread: {thread}")
        for f in wait["fields"]:
            print(f"  - {f['field']}: 지금 값 {json.dumps(f['current'], ensure_ascii=False)}")
        print(f"  고치기: fitcheck resume {thread} --fix 필드=값")
        return

    title = "다시 판정 (내 정보가 바뀌었어요)" if v.get("rejudge") else "지원 결정 대기"
    print(f"{title}  thread: {thread}")
    score = "" if wait.get("fit_score") is None else f"  적합도 {percent(wait['fit_score'])}"
    print(f"  {wait['company']} {wait['title']}  →  {wait['band']}{score}")
    if wait.get("gap_summary"):
        print(f"  보완: {wait['gap_summary']}")
    marks = {"pass": "✓", "hold": "△", "fail": "✕"}
    for c in wait["checks"]:
        extra = f" ({c['detail']})" if c["detail"] else ""
        print(f"  {marks[c['result']]} {c['label']}{extra}")
        if c["result"] == "hold":
            print(f'      인정하려면: fitcheck resume {thread} --override "{c["id"]}"')
    _print_matches(wait.get("matches") or [])
    for r in wait["already_applied"]:
        print(f"  같은 회사 지원 기록: {r['title']} ({r['status']})")
    print(f"  결정: fitcheck resume {thread} --decision apply|hold|skip")


def _parse_fix(items: list[str]) -> dict:
    fields = {}
    for item in items:
        key, _, raw = item.partition("=")
        try:
            fields[key.strip()] = json.loads(raw)
        except json.JSONDecodeError:
            fields[key.strip()] = raw
    return {"fields": fields}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fitcheck", description="IT 직무 채용공고 적합도 진단")
    sub = parser.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="공고 분석 시작")
    src = a.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="공고 내용을 붙여넣은 텍스트 파일")
    src.add_argument("--text", help="공고 내용")
    src.add_argument("--url", help="공고 주소")

    r = sub.add_parser("resume", help="멈춘 분석 이어 가기")
    r.add_argument("thread")
    act = r.add_mutually_exclusive_group(required=True)
    act.add_argument("--decision", choices=["apply", "hold", "skip"])
    act.add_argument("--override", nargs="+", metavar="조건_ID")
    act.add_argument("--fix", nargs="+", metavar="필드=값")

    s = sub.add_parser("show", help="분석 상태 보기")
    s.add_argument("thread")
    s.add_argument("--debug", action="store_true", help="LLM 호출 수, 보완 포인트가 규칙 문장이 된 이유")

    args = parser.parse_args(argv)
    try:
        graph, deps = _open()
    except LLMError as e:
        print(e.message, file=sys.stderr)
        return 2
    except ProfileError as e:
        print(e, file=sys.stderr)
        return 2

    try:
        if args.cmd == "analyze":
            thread = uuid.uuid4().hex[:12]
            inputs = (
                {"input_text": Path(args.file).read_text(encoding="utf-8")}
                if args.file
                else {"input_text": args.text}
                if args.text
                else {"input_url": args.url}
            )
            graph.invoke(inputs, graph.config(thread))
        elif args.cmd == "resume":
            thread = args.thread
            if pending_interrupt(graph, thread) is None:
                print("이어 갈 분석이 없어요. thread 번호를 확인해 주세요.", file=sys.stderr)
                return 1
            if args.decision:
                value = {"decision": args.decision}
            elif args.override:
                value = {"decision": "override", "condition_ids": args.override}
            else:
                value = _parse_fix(args.fix)
            graph.resume(thread, value)
        else:
            thread = args.thread
        if args.cmd != "show":
            graph.refine_summary(thread)  # 터미널은 기다려도 되니 바로 다듬는다 (화면은 백그라운드)
        _report(graph, thread, deps, debug=getattr(args, "debug", False))
        return 0
    except LLMError as e:
        print(e.message, file=sys.stderr)
        return 2
    finally:
        graph.close()
        deps.store.close()
        deps.llm.ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())
