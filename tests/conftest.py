"""모든 테스트 공통 안전장치

테스트는 내 실제 .env(API 키)와 셸의 키를 절대 쓰지 않는다.
- .env 읽기를 끈다 (FIT_ENV_FILE=none)
- 셸에 있는 OPENAI_API_KEY를 지운다
- 기본 모드는 fake
키가 필요한 테스트는 가짜 키를 monkeypatch로 직접 넣는다.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_from_real_secrets(monkeypatch):
    monkeypatch.setenv("FIT_ENV_FILE", "none")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("FIT_LLM", "fake")
