"""T5 완료 조건 테스트 — 입력 정규화 (텍스트, URL)

TASKS.md T5, AGENT_DESIGN 2장, DESIGN_DECISIONS D11
- 붙여넣은 텍스트는 공백만 정리해서 그대로 쓴다
- URL은 실제 브라우저로 열어 본문 글자만 뽑는다 (메뉴, 사이드바, 푸터, 스크립트 제외)
- 본문이 대부분 큰 이미지인 공고는 텍스트 부족으로 판단하고 이미지 주소를 넘긴다
- 로그인 페이지로 가면 멈추고 붙여넣기를 안내한다. 열리지 않는 주소도 안내 후 종료
- http, https만 허용. localhost, 내부망, file: 주소는 막는다
- 입력한 주소 하나만 연다. 링크를 따라가지 않는다

네트워크는 쓰지 않는다. 가짜 https 주소 요청을 가로채 tests/fixtures/pages의 HTML을 돌려준다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fitcheck.nodes.normalize import (
    PlaywrightFetcher,
    UnsafeURL,
    check_url,
    normalize_input,
)

PAGES = Path(__file__).parent / "fixtures" / "pages"
BASE = "https://jobs.example"


def fixture_routes(requested: list[str]):
    """가짜 사이트: 주소별로 고정 HTML을 돌려주고, 요청된 문서 주소를 기록한다."""

    def handler(route, request):
        url = request.url
        if request.resource_type == "document":
            requested.append(url)
        if url == f"{BASE}/posting/text":
            return route.fulfill(
                body=(PAGES / "text_posting.html").read_text(encoding="utf-8"), content_type="text/html"
            )
        if url == f"{BASE}/posting/image":
            return route.fulfill(
                body=(PAGES / "image_posting.html").read_text(encoding="utf-8"), content_type="text/html"
            )
        if url == f"{BASE}/posting/private":
            return route.fulfill(status=302, headers={"location": f"{BASE}/member/login?next=/posting/private"})
        if url.startswith(f"{BASE}/member/login"):
            return route.fulfill(body=(PAGES / "login.html").read_text(encoding="utf-8"), content_type="text/html")
        if url == f"{BASE}/posting/gone":
            return route.fulfill(status=404, body="not found")
        if url.endswith(".png"):
            return route.fulfill(status=200, body=b"", content_type="image/png")
        return route.abort()

    return handler


@pytest.fixture(scope="module")
def fetcher():
    f = PlaywrightFetcher(timeout_ms=8000)
    yield f
    f.close()


@pytest.fixture
def requested(fetcher):
    seen: list[str] = []
    fetcher.set_route_handler(fixture_routes(seen))
    yield seen
    fetcher.set_route_handler(None)


# ── 붙여넣은 텍스트 ──────────────────────────────────────


def test_pasted_text_is_tidied_not_rewritten():
    raw = "  AX 엔지니어 채용\r\n\r\n\r\n\r\n자격요건   \n- Python\t경험  \n" + "업무 설명 " * 40
    r = normalize_input(text=raw)
    assert r.input_type == "text" and r.error is None
    assert "\r" not in r.text and "\n\n\n" not in r.text
    assert r.text.startswith("AX 엔지니어 채용") and "- Python\t경험" in r.text
    assert r.text_is_sufficient


def test_short_pasted_text_is_insufficient_with_message():
    r = normalize_input(text="AI 엔지니어 채용")
    assert not r.text_is_sufficient and r.error == "too_short"
    assert "붙여넣" in r.message


def test_exactly_one_input_required():
    with pytest.raises(ValueError):
        normalize_input()
    with pytest.raises(ValueError):
        normalize_input(text="a", url=f"{BASE}/posting/text")


def test_image_files_go_to_image_path(tmp_path):
    img = tmp_path / "posting.png"
    img.write_bytes(b"\x89PNG\r\n")
    r = normalize_input(image_paths=[str(img)])
    assert r.input_type == "image" and not r.text_is_sufficient and r.image_urls == [str(img)]


# ── URL 안전 검사 ────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/Users/jacob/.env",
        "ftp://jobs.example/a",
        "javascript:alert(1)",
        "http://localhost:8000/admin",
        "http://127.0.0.1/",
        "http://192.168.0.1/",
        "http://10.0.0.5/",
        "http://[::1]/",
        "https:///nohost",
    ],
)
def test_unsafe_urls_rejected(url):
    with pytest.raises(UnsafeURL):
        check_url(url)


def test_public_https_url_allowed():
    assert check_url("https://www.wanted.co.kr/wd/362335") == "https://www.wanted.co.kr/wd/362335"


def test_unsafe_url_returns_message_without_fetching(fetcher, requested):
    r = normalize_input(url="http://localhost/admin", fetcher=fetcher)
    assert r.error == "unsafe_url" and requested == []


# ── URL 가져오기 (실제 브라우저, 가짜 사이트) ─────────────


def test_url_text_posting_extracts_main_only(fetcher, requested):
    r = normalize_input(url=f"{BASE}/posting/text", fetcher=fetcher)
    assert r.error is None and r.input_type == "url" and r.text_is_sufficient
    assert "Python으로 업무 자동화" in r.text and "LangGraph 사용 경험" in r.text
    for noise in ("회원가입", "추천 공고", "이용약관", "숨겨진 스크립트"):
        assert noise not in r.text, f"본문이 아닌 '{noise}'가 들어가면 안 된다"
    assert r.source_url == f"{BASE}/posting/text"


def test_only_the_given_url_is_opened(fetcher, requested):
    normalize_input(url=f"{BASE}/posting/text", fetcher=fetcher)
    assert requested == [f"{BASE}/posting/text"], "링크를 따라가지 않고 입력한 주소 하나만 연다"


def test_image_posting_is_insufficient_and_returns_image_urls(fetcher, requested):
    r = normalize_input(url=f"{BASE}/posting/image", fetcher=fetcher)
    assert r.error is None and not r.text_is_sufficient
    assert r.image_urls == [f"{BASE}/assets/posting-detail.png"]
    assert "이미지테크" in r.text, "이미지 위의 제목 글자는 같이 넘긴다"


def test_login_redirect_stops_with_paste_message(fetcher, requested):
    r = normalize_input(url=f"{BASE}/posting/private", fetcher=fetcher)
    assert r.error == "login_required" and "붙여넣" in r.message
    assert r.text == ""


def test_http_error_is_unreachable(fetcher, requested):
    r = normalize_input(url=f"{BASE}/posting/gone", fetcher=fetcher)
    assert r.error == "unreachable" and "붙여넣" in r.message


def test_network_failure_is_unreachable(fetcher, requested):
    r = normalize_input(url=f"{BASE}/nothing-here", fetcher=fetcher)
    assert r.error == "unreachable"
