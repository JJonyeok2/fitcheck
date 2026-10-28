"""입력 정규화 노드 — 붙여넣은 텍스트, URL, 이미지를 파싱할 수 있는 형태로 (TASKS T5, AGENT_DESIGN 2장)

수집 원칙 (DESIGN_DECISIONS D11)
- 사용자가 넣은 주소 하나만 연다. 링크를 따라가거나 목록을 순회하지 않는다
- 로그인이 필요한 페이지는 가져오지 않고 붙여넣기를 안내한다
- http, https만 허용하고 localhost, 내부망 주소는 막는다 (내 컴퓨터의 다른 서비스를 열지 않게)
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

InputKind = Literal["text", "url", "image"]
ErrorCode = Literal["too_short", "unsafe_url", "unreachable", "login_required"]

MIN_TEXT_CHARS = 200  # 이보다 짧으면 공고 본문이 아니라고 본다
IMAGE_POSTING_TEXT_CHARS = 600  # 큰 이미지가 있고 글자가 이보다 적으면 이미지 공고로 본다
BIG_IMAGE_WIDTH, BIG_IMAGE_HEIGHT = 400, 600
LOGIN_PATH_WORDS = ("login", "signin", "sign-in", "auth")

PASTE_HINT = "공고 페이지의 내용을 복사해서 붙여넣어 주세요."


@dataclass
class NormalizeResult:
    input_type: InputKind
    text: str = ""
    text_is_sufficient: bool = False
    source_url: str | None = None
    image_urls: list[str] = field(default_factory=list)
    error: ErrorCode | None = None
    message: str = ""


# ── 텍스트 정리 ─────────────────────────────────────


def tidy(text: str) -> str:
    """줄바꿈 통일, 줄 끝 공백 제거, 빈 줄 3개 이상을 2개로. 내용은 바꾸지 않는다."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ── URL 안전 검사 ───────────────────────────────────


class UnsafeURL(ValueError):
    pass


def check_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURL("http 또는 https 주소만 열 수 있어요.")
    host = (parsed.hostname or "").lower()
    if not host:
        raise UnsafeURL("주소에 사이트 이름이 없어요.")
    if host == "localhost" or host.endswith(".localhost"):
        raise UnsafeURL("내 컴퓨터 주소는 열 수 없어요.")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return url.strip()
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        raise UnsafeURL("내부망 주소는 열 수 없어요.")
    return url.strip()


# ── 페이지 가져오기 (Playwright) ────────────────────


@dataclass
class PageSnapshot:
    final_url: str
    status: int | None
    text: str
    image_urls: list[str]
    has_password_field: bool


_EXTRACT_JS = """
({bigW, bigH}) => {
  const hide = 'script,style,noscript,template,nav,header,footer,aside,form,iframe';
  document.querySelectorAll(hide).forEach(e => { e.style.display = 'none'; });
  const cands = [...document.querySelectorAll(
    'main, article, [role=main], #content, .content, [class*="detail"], [class*="posting"], [class*="content"]')];
  cands.sort((a, b) => b.innerText.length - a.innerText.length);
  const root = cands[0] || document.body;
  const images = [...root.querySelectorAll('img')]
    .filter(img => { const r = img.getBoundingClientRect(); return r.width >= bigW || r.height >= bigH; })
    .map(img => img.src);
  return {
    text: root.innerText || '',
    images: [...new Set(images)],
    hasPassword: !!document.querySelector('input[type=password]'),
  };
}
"""


class PlaywrightFetcher:
    """브라우저 하나를 띄워 두고 주소마다 새 페이지로 연다. 페이지 안에서 아무것도 클릭하지 않는다."""

    def __init__(self, timeout_ms: int = 15000):
        self.timeout_ms = timeout_ms
        self._pw = None
        self._browser = None
        self._context = None
        self._route_handler: Callable[..., Any] | None = None

    def _ensure(self):
        if self._context is None:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch()
            self._context = self._browser.new_context(locale="ko-KR")
        return self._context

    def set_route_handler(self, handler: Callable[..., Any] | None) -> None:
        """테스트용: 네트워크 대신 가짜 사이트로 응답한다"""
        ctx = self._ensure()
        if self._route_handler is not None:
            ctx.unroute("**/*", self._route_handler)
        self._route_handler = handler
        if handler is not None:
            ctx.route("**/*", handler)

    def fetch(self, url: str) -> PageSnapshot:
        page = self._ensure().new_page()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            data = page.evaluate(_EXTRACT_JS, {"bigW": BIG_IMAGE_WIDTH, "bigH": BIG_IMAGE_HEIGHT})
            return PageSnapshot(
                final_url=page.url,
                status=resp.status if resp else None,
                text=data["text"],
                image_urls=data["images"],
                has_password_field=data["hasPassword"],
            )
        finally:
            page.close()

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._pw is not None:
            self._pw.stop()
        self._pw = self._browser = self._context = None


# ── 노드 ────────────────────────────────────────────


def _looks_like_login(snap: PageSnapshot) -> bool:
    path = urlparse(snap.final_url).path.lower()
    return any(w in path for w in LOGIN_PATH_WORDS) or (
        snap.has_password_field and len(tidy(snap.text)) < MIN_TEXT_CHARS
    )


def normalize_input(
    *,
    text: str | None = None,
    url: str | None = None,
    image_paths: list[str] | None = None,
    fetcher: PlaywrightFetcher | None = None,
) -> NormalizeResult:
    given = [x for x in (text, url, image_paths) if x]
    if len(given) != 1:
        raise ValueError("텍스트, URL, 이미지 중 하나만 넣어 주세요.")

    if image_paths:
        return NormalizeResult(input_type="image", image_urls=list(image_paths))

    if text is not None:
        clean = tidy(text)
        if len(clean) < MIN_TEXT_CHARS:
            return NormalizeResult(
                "text", clean, False, error="too_short", message=f"공고 내용이 너무 짧아요. {PASTE_HINT}"
            )
        return NormalizeResult("text", clean, True)

    assert url is not None
    try:
        safe = check_url(url)
    except UnsafeURL as e:
        return NormalizeResult("url", source_url=url, error="unsafe_url", message=str(e))

    own = fetcher is None
    fetcher = fetcher or PlaywrightFetcher()
    try:
        snap = fetcher.fetch(safe)
    except Exception:
        return NormalizeResult(
            "url", source_url=safe, error="unreachable", message=f"페이지를 열 수 없어요. {PASTE_HINT}"
        )
    finally:
        if own:
            fetcher.close()

    if snap.status is not None and snap.status >= 400:
        return NormalizeResult(
            "url", source_url=safe, error="unreachable", message=f"페이지를 열 수 없어요. {PASTE_HINT}"
        )
    if _looks_like_login(snap):
        return NormalizeResult(
            "url", source_url=safe, error="login_required", message=f"로그인이 필요한 페이지예요. {PASTE_HINT}"
        )

    clean = tidy(snap.text)
    if snap.image_urls and len(clean) < IMAGE_POSTING_TEXT_CHARS:
        return NormalizeResult("url", clean, False, source_url=safe, image_urls=snap.image_urls)
    if len(clean) < MIN_TEXT_CHARS:
        return NormalizeResult(
            "url", clean, False, source_url=safe, error="too_short", message=f"본문을 찾지 못했어요. {PASTE_HINT}"
        )
    return NormalizeResult("url", clean, True, source_url=safe)
