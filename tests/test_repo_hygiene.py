"""T1 완료 조건 테스트 — 저장소 위생 (의존성 고정, lint, CI, 커밋 전 비밀 값 검사)

TASKS.md T1의 완료 조건을 그대로 옮겼다.
- 의존성이 uv.lock으로 고정되어 있다
- ruff check, ruff format --check를 통과한다
- CI가 push, PR마다 ruff, pytest, gitleaks를 돌린다
- 가짜 API 키나 .env, competencies.local.yaml을 커밋하려 하면 거부된다
"""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


# ── 의존성 고정 ─────────────────────────────────────────


def test_pyproject_targets_python_312():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["requires-python"] == ">=3.12"


def test_uv_lock_committed():
    assert (ROOT / "uv.lock").exists(), "uv lock으로 만든 uv.lock이 있어야 한다"


def test_dev_tools_declared():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev = " ".join(data.get("dependency-groups", {}).get("dev", []))
    for tool in ("pytest", "ruff", "pre-commit"):
        assert tool in dev, f"dev 그룹에 {tool}가 있어야 한다"


# ── lint ────────────────────────────────────────────────


def _ruff(*args: str) -> subprocess.CompletedProcess:
    exe = shutil.which("ruff")
    if exe is None:
        pytest.skip("ruff가 설치되지 않음")
    return subprocess.run([exe, *args], cwd=ROOT, capture_output=True, text=True)


def test_ruff_configured():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "ruff" in data.get("tool", {}), "pyproject.toml에 [tool.ruff] 설정이 있어야 한다"


def test_ruff_check_passes():
    r = _ruff("check", ".")
    assert r.returncode == 0, r.stdout + r.stderr


def test_ruff_format_clean():
    r = _ruff("format", "--check", ".")
    assert r.returncode == 0, r.stdout + r.stderr


# ── CI ──────────────────────────────────────────────────


def _workflow() -> dict:
    path = ROOT / ".github" / "workflows" / "ci.yml"
    assert path.exists(), "CI 워크플로 파일이 있어야 한다"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_ci_runs_on_push_and_pr():
    wf = _workflow()
    triggers = wf.get("on", wf.get(True))  # yaml은 'on'을 True로 읽기도 한다
    assert "push" in triggers and "pull_request" in triggers


def test_ci_runs_lint_tests_and_secret_scan():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for needle in ("ruff check", "ruff format --check", "pytest", "gitleaks"):
        assert needle in text, f"CI에 {needle} 단계가 있어야 한다"


def test_ci_never_calls_real_llm():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "FIT_LLM: fake" in text, "CI는 실제 API를 부르지 않도록 FIT_LLM=fake로 돌려야 한다"
    assert "OPENAI_API_KEY" not in text


# ── 커밋 전 비밀 값 검사 ─────────────────────────────────


def test_precommit_config_has_gitleaks_and_file_guard():
    cfg = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hook_ids = {h["id"] for repo in cfg["repos"] for h in repo["hooks"]}
    assert "gitleaks" in hook_ids
    assert "block-private-files" in hook_ids


@pytest.fixture
def sandbox_repo(tmp_path: Path) -> Path:
    """현재 훅 설정을 복사한 임시 저장소. 실제 저장소에는 아무것도 커밋하지 않는다."""
    if shutil.which("pre-commit") is None:
        pytest.skip("pre-commit이 설치되지 않음")
    for name in (".pre-commit-config.yaml", ".gitleaks.toml"):
        if (ROOT / name).exists():
            shutil.copy(ROOT / name, tmp_path / name)
    if (ROOT / "scripts" / "block_private_files.py").exists():
        (tmp_path / "scripts").mkdir()
        shutil.copy(ROOT / "scripts" / "block_private_files.py", tmp_path / "scripts")

    def git(*a: str) -> None:
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.name", "test")
    git("config", "user.email", "test@example.com")
    git("add", "-A")
    git("commit", "-q", "-m", "init", "--no-verify")
    subprocess.run(["pre-commit", "install"], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


def _try_commit(repo: Path, path: str, content: str, force: bool = False) -> subprocess.CompletedProcess:
    (repo / path).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", *(["-f"] if force else []), path], cwd=repo, check=True, capture_output=True)
    return subprocess.run(["git", "commit", "-m", "try"], cwd=repo, capture_output=True, text=True)


FAKE_OPENAI_KEY = "sk-proj-" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF3hJ6kL9nP2rS5uX8aD1gI4lO7qR0tV3w"


def test_commit_with_fake_api_key_is_rejected(sandbox_repo: Path):
    r = _try_commit(sandbox_repo, "config.py", f'OPENAI_API_KEY = "{FAKE_OPENAI_KEY}"\n')
    assert r.returncode != 0, "가짜 키가 들어간 커밋은 거부되어야 한다\n" + r.stdout


def test_commit_of_env_file_is_rejected(sandbox_repo: Path):
    r = _try_commit(sandbox_repo, ".env", "FIT_LLM=fake\n", force=True)
    assert r.returncode != 0, ".env는 강제로 add해도 커밋이 거부되어야 한다\n" + r.stdout


def test_commit_of_local_profile_is_rejected(sandbox_repo: Path):
    r = _try_commit(sandbox_repo, "competencies.local.yaml", "constraints: {}\n", force=True)
    assert r.returncode != 0, "내 실제 역량 파일은 커밋이 거부되어야 한다\n" + r.stdout


def test_normal_commit_is_allowed(sandbox_repo: Path):
    r = _try_commit(sandbox_repo, "notes.md", "평범한 문서\n")
    assert r.returncode == 0, "정상 파일은 커밋되어야 한다\n" + r.stdout + r.stderr
