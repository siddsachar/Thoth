from __future__ import annotations

import threading
from types import SimpleNamespace

import playwright.sync_api

from row_bot.browser import service as service_module
from row_bot.browser.runtime import BrowserRuntimeReadiness
from row_bot.browser.service import BrowserSession


class _Browser:
    def on(self, _event, _handler):
        return None


class _Context:
    def __init__(self):
        self.browser = _Browser()
        self.pages = []
        self.handlers = {}

    def on(self, event, handler):
        self.handlers[event] = handler


class _Chromium:
    def __init__(self, *, fail_channel: bool):
        self.fail_channel = fail_channel
        self.calls = []
        self.context = _Context()

    def launch_persistent_context(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("channel") and self.fail_channel:
            raise RuntimeError("synthetic selected channel failure")
        return self.context


class _Playwright:
    def __init__(self, chromium):
        self.chromium = chromium


class _Starter:
    def __init__(self, playwright):
        self.playwright = playwright

    def start(self):
        return self.playwright


def _ready(path="C:/synthetic/chrome.exe"):
    return BrowserRuntimeReadiness(
        True, "ready", "ready", path, "C:/synthetic", "1.62.0", "1234", "151"
    )


def test_selected_installed_channel_succeeds_without_probe_launch(monkeypatch, tmp_path) -> None:
    chromium = _Chromium(fail_channel=False)
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: _Starter(_Playwright(chromium)))
    monkeypatch.setattr(service_module, "PROFILE_DIR", tmp_path / "profile")
    monkeypatch.setattr(service_module, "_installed_channel", lambda: "chrome")
    monkeypatch.setattr(service_module, "check_packaged_browser_runtime", lambda: _ready())
    monkeypatch.setattr(service_module, "ensure_profile_engine", lambda *args: None)
    session = BrowserSession()
    session._launch_context()
    assert len(chromium.calls) == 1
    assert chromium.calls[0]["channel"] == "chrome"
    assert chromium.calls[0]["headless"] is False
    assert chromium.calls[0]["no_viewport"] is True
    assert "--start-maximized" in chromium.calls[0]["args"]


def test_channel_failure_falls_back_once_to_ready_matching_managed_chromium(monkeypatch, tmp_path) -> None:
    chromium = _Chromium(fail_channel=True)
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: _Starter(_Playwright(chromium)))
    monkeypatch.setattr(service_module, "PROFILE_DIR", tmp_path / "profile")
    monkeypatch.setattr(service_module, "_installed_channel", lambda: "chrome")
    monkeypatch.setattr(service_module, "check_packaged_browser_runtime", lambda: _ready())
    monkeypatch.setattr(service_module, "ensure_profile_engine", lambda *args: None)
    session = BrowserSession()
    session._launch_context()
    assert len(chromium.calls) == 2
    assert chromium.calls[0]["channel"] == "chrome"
    assert chromium.calls[1]["executable_path"] == "C:/synthetic/chrome.exe"


class _OwnedPage:
    def __init__(self, opener=None):
        self._opener = opener
        self.url = "about:blank"

    def opener(self):
        return self._opener


def test_popup_inherits_only_a_recognized_opener_task() -> None:
    session = BrowserSession()
    context = _Context()
    session._context = context
    owner_page = _OwnedPage()
    session._page_owners[owner_page] = "task-a"
    session._register_context_handlers()
    popup = _OwnedPage(owner_page)
    quarantined = _OwnedPage(_OwnedPage())
    context.handlers["page"](popup)
    context.handlers["page"](quarantined)
    assert session._page_owners[popup] == "task-a"
    assert session._thread_pages["task-a"] is popup
    assert quarantined not in session._page_owners


def test_close_stops_context_and_playwright_on_the_owner_thread(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    class _ClosingContext:
        pages = []

        @staticmethod
        def close() -> None:
            calls.append(("context", threading.current_thread().name))

    class _ClosingPlaywright:
        @staticmethod
        def stop() -> None:
            calls.append(("playwright", threading.current_thread().name))

    session = BrowserSession()

    def _launch() -> None:
        session._context = _ClosingContext()
        session._pw = _ClosingPlaywright()
        session._context_generation += 1
        session._launched = True

    monkeypatch.setattr(session, "_launch_context", _launch)
    session._start()
    owner = session._pw_thread
    assert owner is not None and owner.is_alive()

    session.close()

    assert owner.is_alive() is False
    assert calls == [
        ("context", "row-bot-pw"),
        ("playwright", "row-bot-pw"),
    ]
