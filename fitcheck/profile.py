"""내 정보 — 이력서 구조 (Spec 7장, TASKS T20, DESIGN_DECISIONS D16, D17)

사람인, 원티드, 그룹바이 이력서와 같은 13개 항목으로 받는다. 신입이라 경력기술서 자리에 프로젝트가 들어간다.

판정 코드가 쓰는 값은 저장하지 않고 계산한다.
- 경력 연차: 정규직, 계약직 기간의 합 (아르바이트 제외, 인턴은 옵션, 겹친 기간은 한 번만)
- 최고 학력: 졸업, 졸업예정인 학력 중 가장 높은 것 (중퇴, 재학은 제외)
- 만 나이: 생년월일과 기준일

`constraints()`와 `preferences()`가 지금 판정 코드(filters, preferences)가 쓰는 모양으로 바꿔 준다.

민감 항목 (생년월일, 취업 우대사항)
- repr과 str에 나오지 않는다 (Field repr=False)
- 불러오기 오류 메시지에 값을 넣지 않는다 (칸 이름과 이유만)
- LLM 프롬프트에 넣지 않는다. 매칭과 보완 포인트는 프로젝트와 계산 결과만 받는다 (tests/test_profile.py)
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
import warnings
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from .schemas import (
    DEGREE_ORDER,
    CompetencyDB,
    Degree,
    LanguageAbility,
    PendingCertificate,
    Project,
    UserConstraints,
    UserPreferences,
)

COUNTED_KINDS = {"정규직", "계약직"}
DONE_MILITARY = {"군필", "면제", "해당 없음"}
DAYS_PER_YEAR = 365.25

_MONTH = re.compile(r"^\s*(\d{4})[.\-/](\d{1,2})\s*$")


class LegacyProfileWarning(UserWarning):
    """예전 구조(constraints, preferences)의 역량 파일을 읽었을 때"""


class ProfileError(ValueError):
    """역량 파일 형식 오류. 메시지에는 칸 이름과 이유만 넣는다 (값은 민감할 수 있다)."""


def _month(value, *, end: bool):
    """'2024.03', '2024-03' 같은 달 표기를 날짜로. 시작은 그 달 1일, 끝은 그 달 마지막 날"""
    if isinstance(value, str):
        m = _MONTH.match(value)
        if m:
            y, mo = int(m.group(1)), int(m.group(2))
            return date(y, mo, calendar.monthrange(y, mo)[1] if end else 1)
    return value


# ──────────────────────────────────────────────
# 13개 항목
# ──────────────────────────────────────────────


class BasicInfo(BaseModel):
    """1. 기본 정보. 이름, 연락처, 사진, 성별은 받지 않는다."""

    birth_date: date | None = Field(None, repr=False, description="민감. 만 나이(청년 기준) 계산에만")


class Education(BaseModel):
    """2. 학력"""

    school: str
    location: str = Field("", description="학교 소재지 시도. 지역인재 판정")
    major: str = ""
    degree: Degree
    status: Literal["졸업", "졸업예정", "재학", "중퇴"] = "졸업"
    admitted: date | None = None
    graduation_date: date | None = Field(None, description="졸업일, 졸업예정이면 예정일")
    gpa: str = Field("", description="선택. 예: 3.8/4.5")


class Career(BaseModel):
    """3. 경력. 아르바이트도 적되 경력 연차에서는 뺀다 (도메인 경험 근거로는 쓴다)."""

    company: str
    kind: Literal["정규직", "계약직", "인턴", "아르바이트"]
    start: date
    end: date | None = Field(None, description="비우면 재직 중")
    role: str = ""
    tasks: list[str] = Field(default_factory=list)

    @field_validator("start", mode="before")
    @classmethod
    def _start(cls, v):
        return _month(v, end=False)

    @field_validator("end", mode="before")
    @classmethod
    def _end(cls, v):
        return _month(v, end=True)


class Training(BaseModel):
    """5. 교육 이수"""

    name: str
    institution: str = ""
    category: str = Field("", description="예: K-디지털 트레이닝, 이어드림스쿨, 폴리텍 하이테크")
    start: date | None = None
    end: date | None = None
    completed: bool = False

    @field_validator("start", mode="before")
    @classmethod
    def _start(cls, v):
        return _month(v, end=False)

    @field_validator("end", mode="before")
    @classmethod
    def _end(cls, v):
        return _month(v, end=True)


class Activity(BaseModel):
    """5. 동아리, 대외활동"""

    name: str
    kind: str = ""
    period: str = ""
    description: str = ""


class AcquiredCertificate(BaseModel):
    name: str
    date: dt.date | None = None  # 칸 이름이 date라 타입은 dt.date로 쓴다


class Certificates(BaseModel):
    """6. 자격증"""

    acquired: list[AcquiredCertificate] = Field(default_factory=list)
    pending: list[PendingCertificate] = Field(default_factory=list)


class Award(BaseModel):
    """8. 수상. 이력서 방향에만 쓴다."""

    name: str
    host: str = ""
    date: dt.date | None = None


class Link(BaseModel):
    """10. 포트폴리오, 링크"""

    kind: str = Field("", description="예: GitHub, 블로그, 포트폴리오")
    url: str


class EmploymentSupport(BaseModel):
    """11. 취업 우대사항. 병역 외에는 모두 민감 정보이고 기본값 None(입력 안 함)."""

    veteran: bool | None = Field(None, repr=False, description="국가보훈 취업지원대상자")
    disabled: bool | None = Field(None, repr=False, description="장애인")
    national_employment_support: bool | None = Field(None, repr=False, description="국민취업지원제도 참여")
    others: list[str] | None = Field(
        None, repr=False, description="북한이탈주민, 다문화가족, 경력단절, 저소득층, 한부모 등"
    )
    military: Literal["군필", "면제", "미필", "해당 없음"] | None = None


class Desired(BaseModel):
    """12. 희망 근무 조건. 지역과 고용형태는 자격 필터, 나머지는 선호 n/m."""

    regions: list[str] = Field(default_factory=list, description="비우면 제한 없음")
    excluded_employment_types: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list, description="IT 직무 분류 중")
    min_salary: int | None = Field(None, description="만원 단위")
    want_mentor: bool = False
    company_stages: list[str] = Field(default_factory=list)
    work_modes: list[str] = Field(default_factory=list)
    overseas_travel_ok: bool | None = None


class Profile(BaseModel):
    basic: BasicInfo = Field(default_factory=BasicInfo, repr=False)
    education: list[Education] = Field(default_factory=list)
    careers: list[Career] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list, description="4. 프로젝트 (경력기술서)")
    trainings: list[Training] = Field(default_factory=list)
    activities: list[Activity] = Field(default_factory=list)
    certificates: Certificates = Field(default_factory=Certificates)
    languages: list[LanguageAbility] = Field(default_factory=list)
    awards: list[Award] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)
    employment_support: EmploymentSupport = Field(default_factory=EmploymentSupport, repr=False)
    desired: Desired = Field(default_factory=Desired)
    intro: str = Field("", description="13. 간단 소개 (선택). 이력서 방향에만")
    experience_years_manual: float | None = Field(None, description="경력 기간을 직접 입력할 때 (계산보다 우선)")
    intern_counts_as_experience: bool = False

    # ── 판정 코드가 쓰는 값 ──────────────────────

    def age(self, on: date) -> int | None:
        return man_age(self.basic.birth_date, on) if self.basic.birth_date else None

    def highest_education(self) -> Education | None:
        done = [e for e in self.education if e.status in ("졸업", "졸업예정")]
        return max(done, key=lambda e: DEGREE_ORDER[e.degree], default=None)

    def skills(self) -> list[str]:
        """9. 스킬: 따로 받지 않고 프로젝트의 기술을 모아 보여준다 (D4)"""
        seen: list[str] = []
        for p in self.projects:
            for s in [*p.skills, *p.touched_skills]:
                if s not in seen:
                    seen.append(s)
        return seen

    def constraints(self, today: date) -> UserConstraints:
        edu = self.highest_education()
        years = (
            self.experience_years_manual
            if self.experience_years_manual is not None
            else experience_years(self.careers, today, include_intern=self.intern_counts_as_experience)
        )
        return UserConstraints(
            experience_years=years,
            degree=edu.degree if edu else "고졸",
            major=edu.major if edu else "",
            graduation_date=edu.graduation_date if edu else None,
            graduated=edu.status == "졸업" if edu else True,
            military_done=self.employment_support.military in DONE_MILITARY,
            certificates=[c.name for c in self.certificates.acquired],
            pending_certificates=list(self.certificates.pending),
            languages=list(self.languages),
            allowed_regions=list(self.desired.regions),
            excluded_employment_types=list(self.desired.excluded_employment_types),
        )

    def preferences(self) -> UserPreferences:
        d = self.desired
        return UserPreferences(
            min_salary=d.min_salary,
            want_mentor=d.want_mentor,
            company_stages=list(d.company_stages),
            work_modes=list(d.work_modes),
        )

    def competency_db(self) -> CompetencyDB:
        return CompetencyDB(projects=list(self.projects))

    def judgement_payload(self) -> dict:
        """역량 지문에 넣는 항목: 판정에 쓰는 것만. 수상, 활동, 간단 소개는 바뀌어도 재판정하지 않는다."""
        return self.model_dump(
            mode="json",
            include={"basic", "education", "trainings", "employment_support", "desired", "links"},
        )


# ──────────────────────────────────────────────
# 계산
# ──────────────────────────────────────────────


def man_age(birth: date, on: date) -> int:
    return on.year - birth.year - ((on.month, on.day) < (birth.month, birth.day))


def experience_years(careers: list[Career], today: date, *, include_intern: bool = False) -> float:
    """정규직, 계약직(옵션으로 인턴) 기간을 합친다. 겹치는 기간은 한 번만 센다. 소수 첫째 자리"""
    kinds = COUNTED_KINDS | ({"인턴"} if include_intern else set())
    spans = sorted((c.start, min(c.end or today, today)) for c in careers if c.kind in kinds)
    days = 0
    cur_start = cur_end = None
    for s, e in spans:
        if e < s:
            continue
        if cur_end is None or s > cur_end:
            if cur_end is not None:
                days += (cur_end - cur_start).days + 1
            cur_start, cur_end = s, e
        else:
            cur_end = max(cur_end, e)
    if cur_end is not None:
        days += (cur_end - cur_start).days + 1
    return round(days / DAYS_PER_YEAR, 1)


# ──────────────────────────────────────────────
# 불러오기
# ──────────────────────────────────────────────


def _from_legacy(raw: dict) -> dict:
    """예전 구조(constraints, preferences, projects)를 이력서 구조로"""
    c = raw.get("constraints") or {}
    pr = raw.get("preferences") or {}
    edu = {
        "school": "(학교명 미입력)",
        "degree": c.get("degree", "학사"),
        "major": c.get("major", ""),
        "status": "졸업" if c.get("graduated", True) else "졸업예정",
        "graduation_date": c.get("graduation_date"),
    }
    return {
        "education": [edu],
        "careers": [],
        "experience_years_manual": c.get("experience_years") or None,
        "projects": raw.get("projects", []),
        "certificates": {
            "acquired": [{"name": n} for n in c.get("certificates", [])],
            "pending": c.get("pending_certificates", []),
        },
        "languages": c.get("languages", []),
        "employment_support": {"military": "군필" if c.get("military_done") else None},
        "desired": {
            "regions": c.get("allowed_regions", []),
            "excluded_employment_types": c.get("excluded_employment_types", []),
            "min_salary": pr.get("min_salary"),
            "want_mentor": pr.get("want_mentor", False),
            "company_stages": pr.get("company_stages", []),
            "work_modes": pr.get("work_modes", []),
        },
    }


def _describe(e: ValidationError) -> str:
    lines = [f"- {'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors(include_input=False)]
    return "역량 파일 형식이 맞지 않아요.\n" + "\n".join(lines)


class _Loader(yaml.SafeLoader):
    """YAML은 2025.10을 소수 2025.1로 읽어 10월이 1월이 된다. '네 자리.한두 자리'는 글자 그대로 둔다."""


def _float_or_month(loader: yaml.SafeLoader, node: yaml.ScalarNode):
    if _MONTH.match(node.value):
        return node.value
    return loader.construct_yaml_float(node)


_Loader.add_constructor("tag:yaml.org,2002:float", _float_or_month)


def _timestamp_or_text(loader: yaml.SafeLoader, node: yaml.ScalarNode):
    """1987-13-45처럼 날짜 모양이지만 틀린 값은 글자로 남겨, 검증 단계에서 칸 이름과 함께 알린다"""
    try:
        return loader.construct_yaml_timestamp(node)
    except ValueError:
        return node.value


_Loader.add_constructor("tag:yaml.org,2002:timestamp", _timestamp_or_text)


def load_profile_file(path: str | Path) -> Profile:
    raw = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=_Loader) or {}  # noqa: S506 (SafeLoader 기반)
    if "constraints" in raw:
        warnings.warn(
            "역량 파일이 예전 구조(constraints, preferences)예요. 이번에는 바꿔서 읽었어요. "
            "competencies.example.yaml을 참고해 이력서 구조(학력, 경력, 교육 이수, 취업 우대사항 등)로 옮겨 주세요. "
            "학교명, 경력 기간, 교육 이수는 예전 구조에 없어서 비어 있어요.",
            LegacyProfileWarning,
            stacklevel=2,
        )
        raw = _from_legacy(raw)
    try:
        return Profile.model_validate(raw)
    except ValidationError as e:
        raise ProfileError(_describe(e)) from None
