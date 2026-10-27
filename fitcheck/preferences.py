"""선호 조건 — 가고 싶은 곳인가 (규칙 기반)

자격 필터(지원할 수 있나)나 라벨(요건 적합도)과 섞지 않는다.
공고에 정보가 없으면 '정보 없음'으로 두고 감점하지 않는다.
"""

from __future__ import annotations

from .schemas import JobPosting, PreferenceCheck, PreferenceResult, UserPreferences


def check_preferences(posting: JobPosting, pref: UserPreferences) -> PreferenceResult:
    perks = posting.perks
    checks: list[PreferenceCheck] = []

    def add(kind: str, label: str, result: str, detail: str = "") -> None:
        checks.append(PreferenceCheck(kind=kind, label=label, result=result, detail=detail))

    if pref.min_salary is not None:
        label = f"연봉 {pref.min_salary:,}만원 이상"
        if perks.salary_min is None and perks.salary_max is None:
            add("연봉", label, "unknown", "연봉 미기재 또는 회사 내규")
        else:
            # 상한이 기준 이상이면 협상 여지가 있으므로 충족으로 본다
            top = perks.salary_max or perks.salary_min
            ok = top >= pref.min_salary
            shown = f"{perks.salary_min or '?'}~{perks.salary_max or '?'}만원"
            add("연봉", label, "match" if ok else "miss", shown)

    if pref.want_mentor:
        if perks.has_mentor is None:
            add("사수", "사수 있음", "unknown", "공고에 언급 없음")
        else:
            add("사수", "사수 있음", "match" if perks.has_mentor else "miss")

    if pref.company_stages:
        if perks.company_stage == "미상":
            add("회사 단계", ", ".join(pref.company_stages), "unknown", "투자 단계 정보 없음")
        else:
            ok = perks.company_stage in pref.company_stages
            add("회사 단계", perks.company_stage, "match" if ok else "miss")

    if pref.work_modes:
        if perks.work_mode == "미상":
            add("근무 방식", ", ".join(pref.work_modes), "unknown")
        else:
            ok = perks.work_mode in pref.work_modes
            add("근무 방식", perks.work_mode, "match" if ok else "miss")

    return PreferenceResult(checks=checks)
