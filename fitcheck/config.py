"""내 조건과 역량 불러오기

competencies.local.yaml (내 실제 정보, Git 제외)이 있으면 그걸 쓰고,
없으면 competencies.example.yaml (공개용 예시)을 쓴다. 구조는 이력서 구조 (profile.py, Spec 7장). FIT_PROFILE_FILE로 직접 지정할 수 있다 (테스트는 항상 예시).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from .profile import Profile, load_profile_file
from .schemas import CompetencyDB, UserConstraints, UserPreferences

ROOT = Path(__file__).resolve().parent.parent
LOCAL = ROOT / "competencies.local.yaml"
EXAMPLE = ROOT / "competencies.example.yaml"


def profile_path(path: str | Path | None = None) -> Path:
    import os

    override = os.environ.get("FIT_PROFILE_FILE")
    return Path(path) if path else Path(override) if override else (LOCAL if LOCAL.exists() else EXAMPLE)


def load_full_profile(path: str | Path | None = None) -> Profile:
    """이력서 구조 전체 (Spec 7장). 예전 구조면 바꿔서 읽고 LegacyProfileWarning으로 알린다."""
    return load_profile_file(profile_path(path))


def load_profile(
    path: str | Path | None = None, today: date | None = None
) -> tuple[UserConstraints, UserPreferences, CompetencyDB]:
    """판정 코드가 쓰는 값만. 경력 연차는 today 기준으로 계산한다."""
    p = load_full_profile(path)
    return p.constraints(today or date.today()), p.preferences(), p.competency_db()


def load_env(path: str | Path | None = None) -> None:
    """.env 파일을 읽어 환경 변수로 넣는다. 이미 설정된 값은 덮어쓰지 않는다 (CI, 셸에서 준 값이 우선).

    형식: KEY=VALUE, 빈 줄과 #으로 시작하는 줄은 무시, 값의 바깥 따옴표는 벗긴다.
    위치: path 인자 → FIT_ENV_FILE 환경 변수 → 저장소 루트의 .env. FIT_ENV_FILE=none이면 읽지 않는다.
    """
    import os

    if path is None:
        override = os.environ.get("FIT_ENV_FILE")
        if override is not None and override.strip().lower() in ("", "none"):
            return  # 테스트와 CI: 실제 .env를 읽지 않는다
        path = override or ROOT / ".env"
    src = Path(path)
    if not src.exists():
        return
    for raw in src.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)
