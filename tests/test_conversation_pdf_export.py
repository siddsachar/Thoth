from __future__ import annotations

import playwright.sync_api

from row_bot.browser import runtime
from row_bot.ui import helpers


def test_conversation_html_preserves_unicode_and_renders_markdown() -> None:
    html = helpers._build_conversation_html(
        "Unicode — Conversation",
        [
            {
                "role": "assistant",
                "content": "**Bold** — smart quotes “work”\n\n- α\n- β",
            }
        ],
    )

    assert "Unicode — Conversation" in html
    assert "smart quotes “work”" in html
    assert "<strong>Bold</strong>" in html
    assert "<li>α</li>" in html and "<li>β</li>" in html
    assert "**Bold**" not in html
    assert "@page { size: A4; margin: 14mm 16mm 14mm 16mm; }" in html
    assert ".msg:last-child { margin-bottom: 0; }" in html


def test_conversation_pdf_uses_managed_runtime_and_print_layout(monkeypatch) -> None:
    launches: list[dict] = []
    pages: list[object] = []
    stops: list[bool] = []

    class _Page:
        def __init__(self) -> None:
            self.content_wait = ""
            self.media = ""
            self.closed = False

        def set_content(self, _html: str, *, wait_until: str) -> None:
            self.content_wait = wait_until

        def emulate_media(self, *, media: str) -> None:
            self.media = media

        def pdf(self, **kwargs) -> bytes:
            assert kwargs["prefer_css_page_size"] is True
            assert kwargs["margin"]["bottom"] == "14mm"
            return b"%PDF-synthetic"

        def close(self) -> None:
            self.closed = True

    class _Browser:
        def new_page(self, **kwargs):
            assert kwargs["viewport"] == {"width": 794, "height": 1123}
            page = _Page()
            pages.append(page)
            return page

        def close(self) -> None:
            return None

    class _Chromium:
        @staticmethod
        def launch(**kwargs):
            launches.append(kwargs)
            return _Browser()

    class _Playwright:
        chromium = _Chromium()

    class _Starter:
        @staticmethod
        def start():
            return _Playwright()

    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: _Starter())
    monkeypatch.setattr(
        runtime,
        "playwright_chromium_launch_options",
        lambda: {
            "headless": True,
            "executable_path": "C:/synthetic/managed/chrome.exe",
        },
    )
    monkeypatch.setattr(_Playwright, "stop", lambda self: stops.append(True), raising=False)

    result = helpers._render_pdf_playwright(
        "Unicode — Conversation",
        [{"role": "assistant", "content": "**Bold** — α"}],
    )

    assert result == b"%PDF-synthetic"
    assert launches == [
        {
            "headless": True,
            "executable_path": "C:/synthetic/managed/chrome.exe",
        }
    ]
    assert len(pages) == 1
    assert pages[0].content_wait == "load"
    assert pages[0].media == "print"
    assert pages[0].closed is True
    assert stops == [True]
