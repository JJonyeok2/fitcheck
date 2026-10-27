"""내 조건과 역량 불러오기

competencies.local.yaml (내 실제 정보, Git 제외)이 있으면 그걸 쓰고,
없으면 competencies.example.yaml (공개용 예시)을 쓴다.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .schemas import CompetencyDB, UserConstraints, UserPreferences

ROOT = Path(__file__).resolve().parent.parent
LOCAL = ROOT / "competencies.local.yaml"
EXAMPLE = ROOT / "competencies.example.yaml"


def load_profile(path: str | Path | None = None) -> tuple[UserConstraints, UserPreferences, CompetencyDB]:
    src = Path(path) if path else (LOCAL if LOCAL.exists() else EXAMPLE)
    raw = yaml.safe_load(src.read_text(encoding="utf-8"))
    return (
        UserConstraints(**raw["constraints"]),
        UserPreferences(**raw.get("preferences", {})),
        CompetencyDB(projects=raw.get("projects", [])),
    )
