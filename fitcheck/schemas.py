"""채용공고 분석 및 지원 관리 에이전트 — 데이터 스키마

구조
- JobPosting      : 공고 파싱 결과 (텍스트 경로 / 이미지 경로 모두 이 스키마로 수렴)
- UserConstraints : 자격 필터에 쓰는 내 조건 (규칙 코드로 판정, LLM 사용 안 함)
- Project         : 역량 DB의 기본 단위. 기술은 반드시 프로젝트 근거에 묶여 있어야 함
- RequirementMatch: 공고 요구사항 하나에 대한 매칭 결과
- GraphState      : LangGraph 전체 상태
"""

from __future__ import annotations

import operator
from datetime import date, datetime
from enum import Enum, StrEnum
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field

# ──────────────────────────────────────────────
# 1. 공고 파싱 결과
# ──────────────────────────────────────────────

Degree = Literal["무관", "고졸", "전문학사", "학사", "석사", "박사"]
DEGREE_ORDER: dict[str, int] = {"무관": 0, "고졸": 1, "전문학사": 2, "학사": 3, "석사": 4, "박사": 5}

InputType = Literal["text", "url", "image"]

# 이 서비스가 분석하는 직무. 여기 없는 분류는 입력 단계에서 거절한다 (filters.check_supported).
# 기준: 제목이 아니라 담당업무가 IT 서비스, 시스템, 데이터를 직접 다루는가.
IT_CATEGORIES: list[str] = [
    # 개발, 기술
    "개발",
    "AI/데이터",
    "AX/자동화",
    "IT인프라/클라우드",
    "보안",
    "QA/테스트",
    # 개발이 아닌 IT 직무
    "서비스기획/PM/PO",  # 서비스 기획, 프로덕트 매니저, PO
    "프로덕트 디자인",  # UI/UX, 프로덕트 디자이너
    "데이터 분석",  # 데이터 분석가, BI, 그로스 분석
    "IT운영/전산",  # 사내 IT 운영, 전산, 헬프데스크, IT 자산 관리
    "IT컨설팅/프리세일즈",  # ISP, 솔루션 컨설턴트, 세일즈 엔지니어
    "IT영업/고객성공",  # SaaS 영업, 기술 영업, 고객성공(CSM), 테크니컬 서포트
    "IT교육/콘텐츠",  # IT 교육 기획, 커리큘럼, 테크니컬 라이터, 기술 콘텐츠
]
NON_IT_CATEGORIES: list[str] = [
    "디자인(편집/광고)",
    "마케팅",
    "영업",
    "경영지원",
    "생산/제조",
    "서비스/운영",
    "기타",
]
JobCategory = Literal[
    "개발",
    "AI/데이터",
    "AX/자동화",
    "IT인프라/클라우드",
    "보안",
    "QA/테스트",
    "서비스기획/PM/PO",
    "프로덕트 디자인",
    "데이터 분석",
    "IT운영/전산",
    "IT컨설팅/프리세일즈",
    "IT영업/고객성공",
    "IT교육/콘텐츠",
    "디자인(편집/광고)",
    "마케팅",
    "영업",
    "경영지원",
    "생산/제조",
    "서비스/운영",
    "기타",
]
assert set(IT_CATEGORIES) | set(NON_IT_CATEGORIES) == set(JobCategory.__args__)


class ExperienceReq(BaseModel):
    level: Literal["신입", "경력", "신입/경력", "무관"] = "무관"
    min_years: int | None = Field(None, description="필수 최소 경력 연수. '3년 이상' → 3")
    max_years: int | None = None
    preferred_only: bool = Field(False, description="'경력자 우대'처럼 필수가 아닌 경우 True")


class EducationReq(BaseModel):
    min_degree: Degree = "무관"
    preferred_only: bool = Field(False, description="'석사 우대'처럼 필수가 아닌 경우 True")
    majors: list[str] = Field(default_factory=list, description="요구 전공. 비어 있으면 무관")


ProficiencyLevel = Literal["기초", "독해/작문", "일상회화", "비즈니스", "원어민"]
PROFICIENCY_ORDER: dict[str, int] = {"기초": 1, "독해/작문": 2, "일상회화": 3, "비즈니스": 4, "원어민": 5}


class TestScore(BaseModel):
    __test__ = False  # pytest가 테스트 클래스로 수집하지 않게
    test: str = Field(..., description="정규화된 시험명. 예: 'TOEIC', 'TOEIC Speaking', 'OPIc', 'TEPS', 'JLPT'")
    score: float | None = Field(None, description="점수형 시험. 예: TOEIC 800")
    grade: str | None = Field(None, description="등급형 시험. 예: OPIc 'IM2', TOEIC Speaking 'IM3', JLPT 'N2'")


class LanguageReq(BaseModel):
    language: str = Field(..., description="예: '영어', '일본어'")
    accepted_tests: list[TestScore] = Field(
        default_factory=list, description="'TOEIC 800 또는 OPIc IM2 이상'이면 두 개. 비어 있으면 시험 요구 없음"
    )
    min_level: ProficiencyLevel | None = Field(None, description="'비즈니스 회화 가능'처럼 수준만 요구할 때")
    allows_equivalent: bool = Field(False, description="'또는 이에 준하는 능력' 문구가 있으면 True")
    preferred_only: bool = False


class CertificateReq(BaseModel):
    any_of: list[str] = Field(..., description="'정보처리기사 또는 정보보안기사'면 두 개. 하나만 있으면 충족")
    allows_related: bool = Field(False, description="'관련 자격증 보유자'처럼 범위가 열려 있으면 True")
    preferred_only: bool = False


class GraduationReq(BaseModel):
    target: Literal["무관", "기졸업자", "졸업예정자", "기졸업 및 졸업예정자"] = "무관"
    expected_by: date | None = Field(None, description="'2027년 2월 졸업예정자'면 2027-02-28")
    max_years_since: int | None = Field(None, description="'졸업 후 2년 이내'면 2")


CompanyStage = Literal["Seed", "Pre-A", "Series A", "Series B", "Series C 이상", "상장/대기업", "중견/중소", "미상"]
WorkMode = Literal["출근", "하이브리드", "원격", "미상"]


class PostingPerks(BaseModel):
    """선호 조건 판정용 정보. 공고에 없으면 None/미상으로 두고 추측하지 않는다."""

    salary_min: int | None = Field(None, description="연봉 하한, 만원 단위. '회사 내규'면 None")
    salary_max: int | None = None
    has_mentor: bool | None = Field(None, description="사수, 시니어, 리드 존재가 공고에 명시되면 True/False")
    company_stage: CompanyStage = "미상"
    work_mode: WorkMode = "미상"


class Requirement(BaseModel):
    text: str = Field(..., description="공고 원문 표현 그대로")
    category: Literal["skill", "domain", "certificate", "language", "soft", "etc"]
    keywords: list[str] = Field(default_factory=list, description="매칭용 정규화 키워드. 예: ['LangGraph', 'Agent']")
    core: bool = Field(False, description="공고 제목이나 기술 스택에도 나오는 핵심 요건. 가중치 1.5배")


class CriticalField(StrEnum):
    """틀리면 지원 가능한 공고를 버리게 되는 필드. 신뢰도 체크 대상."""

    job_category = "job_category"
    experience = "experience"
    education = "education"
    location = "location"
    employment_type = "employment_type"
    certificates = "certificates"
    languages = "languages"


class ExtractionMeta(BaseModel):
    input_type: InputType
    source_url: str | None = None
    image_paths: list[str] = Field(default_factory=list, description="이미지 경로일 때 분할된 조각 파일들")
    low_confidence_fields: list[CriticalField] = Field(
        default_factory=list, description="비어 있거나 모델이 확신하지 못한 필드 → 사람 확인으로 분기"
    )
    extracted_at: datetime = Field(default_factory=datetime.now)


class JobPosting(BaseModel):
    company: str
    title: str
    job_category: JobCategory = Field(
        "기타",
        description="제목이 아니라 담당업무 기준으로 분류. 예: '마케팅 자동화 개발자'는 개발, '퍼포먼스 마케터'는 마케팅, 'SaaS 기술 영업'은 IT영업/고객성공",
    )
    location: str | None = Field(None, description="근무지 원문. 예: '서울 관악구', '경기 과천'")
    employment_type: Literal["정규직", "계약직", "인턴", "채용전환형 인턴", "파트타임", "기타"] | None = None
    deadline: date | None = Field(None, description="'채용시 마감'이면 None + rolling_deadline=True")
    rolling_deadline: bool = False

    experience: ExperienceReq = Field(default_factory=ExperienceReq)
    education: EducationReq = Field(default_factory=EducationReq)
    graduation: GraduationReq = Field(default_factory=GraduationReq)
    military: Literal["무관", "군필 또는 면제"] = "무관"
    certificates: list[CertificateReq] = Field(default_factory=list)
    languages: list[LanguageReq] = Field(default_factory=list)

    required: list[Requirement] = Field(default_factory=list, description="자격요건 / 필수")
    preferred: list[Requirement] = Field(default_factory=list, description="우대사항")
    tech_stack: list[str] = Field(default_factory=list)
    responsibilities: list[str] = Field(
        default_factory=list, description="담당업무. 필수/우대 요건이 없을 때 충족률 계산에 사용"
    )

    perks: PostingPerks = Field(default_factory=PostingPerks)

    raw_text: str = Field(..., description="파싱 전 원문 (이미지 경로면 비전 모델 추출 텍스트)")
    meta: ExtractionMeta


# ──────────────────────────────────────────────
# 2. 자격 필터 (규칙 기반)
# ──────────────────────────────────────────────


class LanguageAbility(BaseModel):
    language: str
    level: ProficiencyLevel = Field(..., description="본인 판단 수준")
    tests: list[TestScore] = Field(default_factory=list, description="유효기간 내 공인 성적만")


class PendingCertificate(BaseModel):
    name: str
    expected_date: date | None = Field(None, description="합격 발표 예정일. 공고 마감일과 비교해 보류/탈락을 가름")


class UserConstraints(BaseModel):
    experience_years: float = 0
    degree: Degree = "학사"
    major: str = ""
    graduation_date: date | None = Field(None, description="졸업일. 졸업예정자면 예정일")
    graduated: bool = True
    military_done: bool = Field(False, description="군필 또는 면제")
    certificates: list[str] = Field(default_factory=list, description="취득 완료한 것만")
    pending_certificates: list[PendingCertificate] = Field(default_factory=list)
    languages: list[LanguageAbility] = Field(default_factory=list)
    allowed_regions: list[str] = Field(default_factory=list, description="지원 가능 지역. 비어 있으면 제한 없음")
    excluded_employment_types: list[str] = Field(default_factory=list)


class UserPreferences(BaseModel):
    """가고 싶은 곳인가. 자격 필터, 라벨과 분리해서 '선호 n/m'으로만 보여준다."""

    min_salary: int | None = Field(None, description="만원 단위")
    want_mentor: bool = False
    company_stages: list[str] = Field(default_factory=list, description="비어 있으면 제한 없음")
    work_modes: list[str] = Field(default_factory=list, description="비어 있으면 제한 없음")


class PreferenceCheck(BaseModel):
    kind: Literal["연봉", "사수", "회사 단계", "근무 방식"]
    label: str
    result: Literal["match", "miss", "unknown"] = Field(..., description="unknown: 공고에 정보 없음")
    detail: str = ""


class PreferenceResult(BaseModel):
    checks: list[PreferenceCheck] = Field(default_factory=list)

    @property
    def matched(self) -> int:
        return sum(c.result == "match" for c in self.checks)

    @property
    def known(self) -> int:
        return sum(c.result != "unknown" for c in self.checks)

    @property
    def summary(self) -> str:
        """예: '선호 2/3 (1개는 공고에 정보 없음)'"""
        unknown = len(self.checks) - self.known
        tail = f" ({unknown}개는 공고에 정보 없음)" if unknown else ""
        return f"선호 {self.matched}/{self.known}{tail}" if self.checks else ""


class ConditionCheck(BaseModel):
    """조건 하나에 대한 판정. UI의 조건 칩 하나가 된다."""

    kind: Literal["마감", "경력", "학력", "졸업", "병역", "자격증", "어학", "근무지", "고용형태", "추출"]
    label: str = Field(..., description="칩에 표시할 짧은 문구. 예: '서울 강남구', 'TOEIC 800'")
    result: Literal["pass", "hold", "fail"]
    detail: str = Field("", description="hold/fail일 때 이유, pass여도 D-3처럼 알릴 것")
    overridden: bool = Field(False, description="보류를 사용자가 직접 '인정됨'으로 바꾼 경우")

    @property
    def id(self) -> str:
        """보류 해제 기록에 쓰는 안정적인 키"""
        return f"{self.kind}:{self.label}"


class EligibilityResult(BaseModel):
    status: Literal["eligible", "ineligible", "ambiguous"]
    reasons: list[str] = Field(default_factory=list, description="탈락 또는 보류 사유 요약")
    checks: list[ConditionCheck] = Field(default_factory=list, description="통과 포함 전체 조건 판정")

    @property
    def pass_ratio(self) -> float:
        return sum(c.result == "pass" for c in self.checks) / len(self.checks) if self.checks else 1.0


class FitBand(StrEnum):
    """사용자에게 보여주는 지원 판단 라벨. 합격 확률이 아니라 '공고 요건에 얼마나 맞는가'를 나타낸다.
    - 합격권 (파랑): 자격 충족 + 필수요건 80% 이상 + 학력에서 불리하지 않음
    - 안정권 (주황): 자격 충족 + 필수요건 50% 초과 80% 미만 (석사 우대 공고는 충족률이 높아도 여기까지)
    - 불합격권(빨강): 필수요건 50% 이하, 또는 자격 판단 보류(취득 예정, '이에 준하는 능력' 등)
    - 지원 불가(회색): 자격 미달 (마감, 경력, 학력, 졸업, 병역, 필수 자격증, 어학, 근무지, 고용형태)
    충족률을 계산할 요건이 하나도 없는 공고는 자격 충족 시 안정권까지만 준다 (검증할 근거가 없으므로).
    """

    PASS = "합격권"
    STABLE = "안정권"
    LOW = "불합격권"
    BLOCKED = "지원 불가"


BAND_ORDER: dict[FitBand, int] = {FitBand.PASS: 1, FitBand.STABLE: 2, FitBand.LOW: 3, FitBand.BLOCKED: 4}


class EducationTier(int, Enum):
    """학력 기준 우선순위. 숫자가 작을수록 먼저 노출.
    - DEGREE_MATCH  : 요구 학력이 내 학력과 정확히 일치 (예: '학사 이상', '대졸')
    - OPEN          : 학력 무관, 또는 내 학력보다 낮은 요건, 또는 내 학력이 '우대'로만 표기
    - DISADVANTAGED : 내 학력보다 높은 학력이 '우대' (지원은 가능하지만 불리)
    - UNKNOWN       : 학력 정보를 확신할 수 없음 (사람 확인 필요)
    """

    DEGREE_MATCH = 1
    OPEN = 2
    DISADVANTAGED = 3
    UNKNOWN = 4


# ──────────────────────────────────────────────
# 3. 역량 DB (프로젝트 중심)
# ──────────────────────────────────────────────


class Project(BaseModel):
    id: str
    name: str
    period: str = Field(..., description="예: '2026.06 ~ 2026.07'")
    team_size: int = 1
    my_role: str = Field(..., description="팀 프로젝트면 본인이 실제로 맡은 부분만")
    contributions: list[str] = Field(default_factory=list, description="본인이 직접 한 일")
    results: list[str] = Field(default_factory=list, description="정량 결과 우선")
    skills: list[str] = Field(default_factory=list, description="이 프로젝트에서 본인이 직접 쓴 기술만")
    touched_skills: list[str] = Field(
        default_factory=list, description="조금 만져본 기술. 매칭에서 '조금 만져봄'(0.3)까지만 인정"
    )


class CompetencyDB(BaseModel):
    projects: list[Project]

    def skill_index(self) -> dict[str, list[str]]:
        """기술(정규화된 이름) → 근거 프로젝트 id 목록. 조금 만져본 기술도 포함. 근거 없는 기술은 없다."""
        from .aliases import canonical

        index: dict[str, list[str]] = {}
        for p in self.projects:
            for s in [*p.skills, *p.touched_skills]:
                ids = index.setdefault(canonical(s), [])
                if p.id not in ids:
                    ids.append(p.id)
        return index


# ──────────────────────────────────────────────
# 4. 매칭 / 갭 리포트
# ──────────────────────────────────────────────

MatchStrength = Literal["직접 구현", "팀에서 담당", "사용 경험", "조금 만져봄", "없음"]


class RequirementMatch(BaseModel):
    requirement: Requirement
    is_required: bool
    status: Literal["matched", "partial", "missing"]
    strength: MatchStrength | None = Field(
        None, description="충족 정도 5단계. 없으면 status로 대신 계산 (matched 1.0, partial 0.5, missing 0)"
    )
    evidence_project_ids: list[str] = Field(default_factory=list, description="matched/partial이면 반드시 1개 이상")
    note: str = ""
    learnable_short_term: bool = Field(False, description="missing이지만 짧게 보완 가능한 것")


class ScoreItem(BaseModel):
    """적합도 점수의 구성 요소 한 줄. 화면의 '점수 내역'에 그대로 보여준다."""

    text: str
    is_required: bool
    weight: float
    credit: float = Field(..., ge=0, le=1)
    strength: str
    evidence_project_ids: list[str] = Field(default_factory=list)


class GapReport(BaseModel):
    fit_score: float | None = Field(
        None, ge=0, le=1, description="가중 적합도 점수. 라벨 판정과 정렬에 쓴다. 화면에는 정수 %로"
    )
    breakdown: list[ScoreItem] = Field(default_factory=list)
    required_met: tuple[int, int] = Field((0, 0), description="(충족, 전체) 필수요건 개수. 점수 옆에 항상 표시")
    preferred_met: tuple[int, int] = (0, 0)
    required_coverage: float | None = Field(None, ge=0, le=1, description="필수요건 단순 충족률 (참고용)")
    coverage_basis: Literal["required", "preferred", "responsibilities", "none"] = "required"
    preferred_coverage: float | None = Field(None, ge=0, le=1)
    soft_requirements: list[str] = Field(default_factory=list, description="충족률에서 제외한 소프트 스킬 요건")
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    summary: str = ""


# ──────────────────────────────────────────────
# 5. 지원 현황 트래킹
# ──────────────────────────────────────────────


class ApplicationStatus(StrEnum):
    interested = "관심"
    applied = "지원"
    document_passed = "서류합격"
    interview = "면접"
    offer = "최종합격"
    rejected = "탈락"
    on_hold = "보류"
    skipped = "미지원"


class StatusChange(BaseModel):
    status: ApplicationStatus
    at: datetime = Field(default_factory=datetime.now)
    memo: str = ""


class ApplicationRecord(BaseModel):
    posting_id: str
    dedup_key: str = Field(..., description="회사명과 직무명을 정규화한 키. 플랫폼이 달라도 같은 공고면 같은 값")
    company: str
    title: str
    source_urls: list[str] = Field(default_factory=list, description="같은 공고가 올라온 모든 플랫폼 URL")
    deadline: date | None = None
    status: ApplicationStatus = ApplicationStatus.interested
    history: list[StatusChange] = Field(default_factory=list)
    overrides: list[str] = Field(default_factory=list, description="사용자가 직접 인정한 보류 조건 id")
    judged_with: str = Field("", description="판정 당시 역량 지문. 현재 지문과 다르면 재판정 대상")
    imported: bool = Field(False, description="기존 지원 이력에서 이관한 기록 (공고 원문 없음)")


# ──────────────────────────────────────────────
# 6. LangGraph 상태
# ──────────────────────────────────────────────


def _union(a: list[str] | None, b: list[str] | None) -> list[str]:
    """보류 인정 id는 쌓인다 (LangGraph 리듀서)"""
    return sorted(set(a or []) | set(b or []))


EndReason = Literal["input_unreachable", "parse_failed", "image_not_supported_yet", "unsupported", "duplicate", "saved"]


class GraphState(TypedDict, total=False):
    """LangGraph 상태. 필드마다 쓰는 노드는 하나 (AGENT_DESIGN 4.1). 누적 필드만 리듀서를 둔다."""

    # 입력
    input_type: InputType
    input_url: str | None
    input_text: str | None
    input_image_paths: list[str]

    # 입력 정규화 / 이미지 경로
    normalized_text: str
    text_is_sufficient: bool
    image_chunks: list[str]

    # 파싱
    content_hash: str  # 정규화된 원문 해시. 같은 원문이면 저장된 파싱 결과를 재사용
    llm_calls: Annotated[int, operator.add]  # 노드는 이번에 쓴 횟수만 반환
    posting: JobPosting
    needs_human_check: list[CriticalField]
    verify_attempts: int

    # 관문
    supported_verdict: Literal["supported", "verify", "unsupported"]
    unsupported_reason: str
    posting_id: str
    already_applied: list[ApplicationRecord]  # 같은 회사 다른 포지션 지원 이력

    # 필터 / 매칭
    eligibility: EligibilityResult
    education_tier: EducationTier
    matches: list[RequirementMatch]
    gap_report: GapReport
    fit_band: FitBand
    preferences: PreferenceResult
    gap_summary: str

    # 사람 결정 (interrupt)
    overrides: Annotated[list[str], _union]
    decision: Literal["apply", "skip", "hold", "override"]

    # 산출물, 종료
    resume_draft: str
    application: ApplicationRecord
    end_reason: EndReason
    error: str
