from __future__ import annotations

from row_bot.tools.browser_tool import BrowserSession, BrowserSessionManager


class _Page:
    def __init__(self, url: str, title: str, *, protected: bool = False) -> None:
        self.url = url
        self._title = title
        self.protected = protected
        self.front_count = 0
        self.screenshot_count = 0
        self.visibility = "visible"

    def is_closed(self) -> bool:
        return False

    def title(self) -> str:
        return self._title

    def bring_to_front(self) -> None:
        self.front_count += 1

    def evaluate(self, _script: str) -> str:
        return self.visibility

    def query_selector(self, _selector: str):
        return object() if self.protected else None

    def screenshot(self, *, type: str) -> bytes:
        assert type == "png"
        self.screenshot_count += 1
        return b"png"


def test_activity_snapshot_is_thread_isolated_and_hides_query_from_site_label() -> None:
    session = BrowserSession()
    page = _Page("https://example.com/private?token=secret", "Private dashboard")
    session._publish_activity("thread-a", state="observing", action="Opened website", page=page)

    active = session.status_snapshot("thread-a")
    other = session.status_snapshot("thread-b")
    assert active["active"] is True
    assert active["site"] == "example.com"
    assert "secret" not in active["site"]
    assert other["active"] is False


def test_activity_listener_is_local_metadata_only() -> None:
    session = BrowserSession()
    events: list[dict] = []
    unsubscribe = session.add_activity_listener(events.append)
    session._publish_activity("thread-a", state="acting", action="Click page control")
    session.end_activity("thread-a")
    unsubscribe()

    assert [event["state"] for event in events] == ["acting", "idle"]
    assert events[-1]["active"] is False
    assert session._work_q.empty()


def test_take_over_foregrounds_only_existing_task_tab_without_creating_one() -> None:
    session = BrowserSession()
    page = _Page("https://example.com", "Example")
    session._launched = True
    session._thread_pages = {"thread-a": page}
    session._run_on_pw_thread = lambda fn: fn()

    assert session.take_over("thread-a") is True
    assert session.take_over("thread-b") is False
    assert page.front_count == 1
    assert session.status_snapshot("thread-a")["state"] == "waiting_user"
    assert "thread-b" not in session._thread_pages


def test_take_over_refuses_to_claim_success_when_window_remains_hidden() -> None:
    session = BrowserSession()
    page = _Page("https://example.com", "Example")
    page.visibility = "hidden"
    session._launched = True
    session._thread_pages = {"thread-a": page}
    session._run_on_pw_thread = lambda fn: fn()

    assert session.take_over("thread-a") is False
    assert session.status_snapshot("thread-a")["state"] == "needs_attention"


def test_manager_listener_registered_before_session_creation_receives_events() -> None:
    manager = BrowserSessionManager()
    events: list[dict] = []
    manager.add_activity_listener(events.append)
    session = manager.get_session("thread-a")
    session._publish_activity("thread-a", state="acting", action="Open website")
    assert events[-1]["thread_id"] == "thread-a"


def test_ephemeral_preview_shields_protected_browser_fields_and_reuses_safe_frame() -> None:
    session = BrowserSession()
    protected = _Page("https://example.com/login", "Login", protected=True)
    safe = _Page("https://example.com/home", "Home")
    session._launched = True
    session._run_on_pw_thread = lambda fn: fn()
    session._thread_pages = {"protected": protected, "safe": safe}
    session._publish_activity(
        "protected",
        state="observing",
        action="Inspect protected page",
        page=protected,
    )
    session._publish_activity(
        "safe",
        state="observing",
        action="Inspect safe page",
        page=safe,
    )

    assert session.take_screenshot("protected") is None
    assert session.take_screenshot("safe") == b"png"
    assert protected.screenshot_count == 0
    assert safe.screenshot_count == 1
    assert session.status_snapshot("protected")["preview_shielded"] is True
    assert session.status_snapshot("safe")["has_thumbnail"] is True
    for _ in range(10):
        assert session.ephemeral_screenshot("safe") == b"png"
    assert safe.screenshot_count == 1

    replacement = _Page("https://example.com/next", "Next")
    session._publish_activity(
        "safe",
        state="observing",
        action="Navigate",
        page=replacement,
    )
    assert session.ephemeral_screenshot("safe") is None
    assert session.status_snapshot("safe")["has_thumbnail"] is False

    session.end_activity("safe")
    assert session.ephemeral_screenshot("safe") is None
    assert session.status_snapshot("safe")["has_thumbnail"] is False
