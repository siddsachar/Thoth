from __future__ import annotations

from types import SimpleNamespace

from row_bot.browser import service as service_module
from row_bot.browser.service import BrowserSession, BrowserSessionManager
from row_bot.tools import browser_tool


class _Mouse:
    def wheel(self, _x, _y):
        return None


class _Handle:
    def __init__(self, page, label="Search") -> None:
        self.page = page
        self.label = label
        self.attached = True
        self.clicked = 0
        self.filled: list[str] = []
        self.navigate_then_fail = False

    def evaluate(self, _script):
        return {
            "attached": self.attached,
            "visible": True,
            "tag": "input",
            "role": "textbox",
            "type": "text",
            "label": self.label,
            "href": "",
            "download": False,
            "form_action": "",
            "disabled": False,
            "value_length": 0,
            "in_dialog": False,
        }

    def click(self, timeout):
        self.clicked += 1
        if self.navigate_then_fail:
            self.page.url = "https://example.test/opened"
            raise RuntimeError("synthetic context destroyed after navigation")

    def fill(self, text, timeout):
        self.filled.append(text)
        self.page.filled_values.append(text)

    def press(self, key, timeout):
        assert key == "Enter"
        self.page.url = "https://example.test/results"

    def dispose(self):
        self.attached = False


class _Page:
    def __init__(self) -> None:
        self.url = "https://example.test/"
        self.closed = False
        self.mouse = _Mouse()
        self.filled_values: list[str] = []
        self.handle = _Handle(self)

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True

    def title(self):
        return "Synthetic"

    def query_selector_all(self, _selector):
        self.handle = _Handle(self, self.handle.label)
        return [self.handle]

    def query_selector(self, _selector):
        return None

    def bring_to_front(self):
        return None

    def goto(self, url, *, wait_until, timeout):
        assert wait_until == "domcontentloaded"
        self.url = url

    def go_back(self, *, wait_until, timeout):
        assert wait_until == "domcontentloaded"
        self.url = "https://example.test/previous"


class _Context:
    def __init__(self, page) -> None:
        self.pages = [page]

    def new_page(self):
        page = _Page()
        self.pages.append(page)
        return page


def _service():
    service = BrowserSession()
    page = _Page()
    service._context = _Context(page)
    service._launched = True
    service._context_generation = 1
    service._thread_pages = {"task": page}
    service._page_owners = {page: "task"}
    service._run_on_pw_thread = lambda fn: fn()
    return service, page


def test_type_without_submit_returns_receipt_without_automatic_observation() -> None:
    service, page = _service()
    first = service.snapshot("task")
    token = next(iter(service._observations.current("task").targets))
    before = service._observations.observation_count
    secret = "typed-secret-value"
    result = service.type_text(token, secret, False, "task")
    assert "Action receipt" in result
    assert secret not in result
    assert service._observations.observation_count == before
    assert service._observations.current("task") is None
    assert "typed-secret-value" not in first


def test_non_navigating_click_returns_receipt_and_never_retargets() -> None:
    service, page = _service()
    service.snapshot("task")
    observation = service._observations.current("task")
    token = next(iter(observation.targets))
    exact = observation.targets[token].handle
    before = service._observations.observation_count
    result = service.click(token, "task")
    assert "Action receipt" in result
    assert exact.clicked == 1
    assert service._observations.observation_count == before
    assert service.click(token, "task").startswith("ERROR [stale_observation]")


def test_click_navigation_race_reports_verified_receipt_without_backend_failure() -> None:
    service, page = _service()
    service.snapshot("task")
    observation = service._observations.current("task")
    token = next(iter(observation.targets))
    exact = observation.targets[token].handle
    exact.navigate_then_fail = True

    before = service._observations.observation_count
    result = service.click(token, "task")

    assert "ERROR" not in result
    assert "Page-change receipt" in result
    assert "https://example.test/opened" in result
    assert exact.clicked == 1
    assert service._observations.observation_count == before
    assert service._observations.current("task") is None


def test_navigating_click_returns_page_facts_without_new_target_handles() -> None:
    service, page = _service()
    service.snapshot("task")
    observation = service._observations.current("task")
    token = next(iter(observation.targets))
    target = observation.targets[token].handle
    target.navigate_then_fail = True

    result = service.click(token, "task")

    receipt, page_change = result.splitlines()
    assert '"verified_outcome": true' in receipt
    assert page_change == (
        'Page-change receipt: {"title": "Synthetic", '
        '"url": "https://example.test/opened"}'
    )
    assert service._observations.current("task") is None


def test_submit_navigation_returns_exactly_one_post_action_observation() -> None:
    service, page = _service()
    service.snapshot("task")
    token = next(iter(service._observations.current("task").targets))
    before = service._observations.observation_count
    result = service.type_text(token, "hidden", True, "task")
    assert "https://example.test/results" in result
    assert service._observations.observation_count == before + 1


def test_approved_submit_reproves_and_executes_once_without_reapproval_loop() -> None:
    service, page = _service()
    service.snapshot("task")
    token = next(iter(service._observations.current("task").targets))
    metadata = service.describe_ref(token, "task")
    service.mark_waiting_approval("task", "Approve form action")

    result = service.type_text_after_approval(
        metadata,
        "hidden-value",
        True,
        "task",
    )

    assert "approval_target_changed" not in result
    assert "https://example.test/results" in result
    assert page.filled_values == ["hidden-value"]
    assert "hidden-value" not in result


def test_approved_submit_fails_stably_when_exact_target_cannot_be_reproved() -> None:
    service, page = _service()
    service.snapshot("task")
    token = next(iter(service._observations.current("task").targets))
    metadata = service.describe_ref(token, "task")
    service.mark_waiting_approval("task", "Approve form action")
    page.handle.label = "Different field"

    result = service.type_text_after_approval(metadata, "hidden-value", True, "task")

    assert result.startswith("ERROR [approval_target_changed]")
    assert page.url == "https://example.test/"
    assert page.filled_values == []


def test_browser_tool_approved_submit_completes_in_the_same_tool_invocation(
    monkeypatch,
) -> None:
    service, page = _service()
    service.snapshot("task")
    token = next(iter(service._observations.current("task").targets))
    monkeypatch.setattr(browser_tool, "_get_thread_id", lambda: "task")
    monkeypatch.setattr(
        browser_tool,
        "_session_manager",
        SimpleNamespace(get_session=lambda _thread_id: service),
    )
    monkeypatch.setattr("row_bot.tools.approval_gate.gate_action", lambda _payload: None)
    tool = next(
        item
        for item in browser_tool.BrowserTool().as_langchain_tools()
        if item.name == "browser_type"
    )

    result = tool.invoke({"ref": token, "text": "hidden-value", "submit": True})

    assert "stale_observation" not in result
    assert "https://example.test/results" in result
    assert page.filled_values == ["hidden-value"]


def test_browser_tool_approval_replay_reuses_only_the_staged_exact_target(
    monkeypatch,
) -> None:
    service, page = _service()
    service.snapshot("task")
    token = next(iter(service._observations.current("task").targets))
    metadata = service.describe_ref(token, "task")
    service.stage_approval_proof("task", "type", token, metadata)
    service.mark_waiting_approval("task", "Approve form action")
    monkeypatch.setattr(browser_tool, "_get_thread_id", lambda: "task")
    monkeypatch.setattr(
        browser_tool,
        "_session_manager",
        SimpleNamespace(get_session=lambda _thread_id: service),
    )
    monkeypatch.setattr("row_bot.tools.approval_gate.gate_action", lambda _payload: None)
    tool = next(
        item
        for item in browser_tool.BrowserTool().as_langchain_tools()
        if item.name == "browser_type"
    )

    result = tool.invoke({"ref": token, "text": "hidden-value", "submit": True})

    assert "stale_observation" not in result
    assert "https://example.test/results" in result
    assert page.filled_values == ["hidden-value"]
    assert service.pending_approval_proof("task", "type", token) is None


def test_browser_tool_approval_replay_does_not_authorize_another_ref() -> None:
    service, _page = _service()
    service.snapshot("task")
    token = next(iter(service._observations.current("task").targets))
    metadata = service.describe_ref(token, "task")
    service.stage_approval_proof("task", "type", token, metadata)
    service.mark_waiting_approval("task", "Approve form action")

    assert service.pending_approval_proof("task", "type", "different-ref") is None
    assert service.pending_approval_proof("other-task", "type", token) is None


def test_takeover_and_approval_invalidate_current_targets() -> None:
    service, page = _service()
    service.snapshot("task")
    assert service._observations.current("task") is not None
    service.mark_waiting_approval("task", "Approve")
    assert service._observations.current("task") is None


def test_tab_close_and_task_release_invalidate_current_targets() -> None:
    service, page = _service()
    second = service._context.new_page()
    service._page_owners[second] = "task"
    service.snapshot("task")
    assert service._observations.current("task") is not None
    service.tab_action("close", 0, thread_id="task")
    assert service._observations.current("task") is not None  # the returned page has one new snapshot
    service.release_thread("task")
    assert service._observations.current("task") is None
    service.snapshot("task")
    assert service.take_over("task")
    assert service._observations.current("task") is None


def test_raw_backend_errors_are_sanitized() -> None:
    service, page = _service()
    secret = r"C:\Users\private\profile token=secret"
    page.goto = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError(secret))
    result = service.navigate("https://example.test", "task")
    assert secret not in result
    assert result.startswith("ERROR [")


def test_navigate_search_open_budget_has_no_screenshot_or_vision() -> None:
    service, page = _service()
    service.navigate("https://example.test/search", "task")
    search = next(iter(service._observations.current("task").targets))
    service.type_text(search, "synthetic query", True, "task")
    result_target = next(iter(service._observations.current("task").targets))
    service.click(result_target, "task")

    counts = service.performance_snapshot()
    assert counts == {
        "browser_tool_calls": 3,
        "semantic_observations": 2,
        "preview_captures": 0,
        "vision_calls": 0,
    }


def test_one_stale_recovery_stays_inside_architecture_ceiling() -> None:
    service, _page = _service()
    service.navigate("https://example.test/search", "task")
    stale = next(iter(service._observations.current("task").targets))
    service._invalidate("task")

    assert service.click(stale, "task").startswith("ERROR [stale_observation]")
    service.snapshot("task")
    fresh = next(iter(service._observations.current("task").targets))
    assert service.click(fresh, "task").startswith("Action receipt")

    counts = service.performance_snapshot()
    assert counts["browser_tool_calls"] <= 6
    assert counts["semantic_observations"] <= 4
    assert counts["preview_captures"] == 0
    assert counts["vision_calls"] == 0


def test_idle_cleanup_evicts_terminal_task_and_preserves_active_task(monkeypatch) -> None:
    service, idle_page = _service()
    active_page = service._context.new_page()
    service._thread_pages = {"idle": idle_page, "active": active_page}
    service._page_owners = {idle_page: "idle", active_page: "active"}
    service._thread_pages_last_used = {"idle": 1.0, "active": 1.0}
    monkeypatch.setattr(service_module, "_active_generation_thread_ids", lambda: {"active"})
    monkeypatch.setattr(service_module.time, "monotonic", lambda: 1_000.0)

    assert service.evict_idle(ttl_seconds=600.0) == 1
    assert idle_page.closed is True
    assert active_page.closed is False
    assert "idle" not in service._thread_pages
    assert service._thread_pages["active"] is active_page


def test_session_manager_delegates_scheduled_idle_cleanup() -> None:
    calls: list[float] = []
    manager = BrowserSessionManager()
    manager._shared_session = SimpleNamespace(
        evict_idle=lambda *, ttl_seconds: calls.append(ttl_seconds) or 2,
    )

    assert manager.evict_idle(ttl_seconds=321.0) == 2
    assert calls == [321.0]


def test_idle_cleanup_closes_singleton_task_page_and_leaves_fresh_unowned_page(monkeypatch) -> None:
    service, idle_page = _service()
    service._thread_pages_last_used = {"idle": 1.0}
    service._thread_pages = {"idle": idle_page}
    service._page_owners = {idle_page: "idle"}
    monkeypatch.setattr(service_module, "_active_generation_thread_ids", lambda: set())
    monkeypatch.setattr(service_module.time, "monotonic", lambda: 1_000.0)

    assert service.evict_idle(ttl_seconds=600.0) == 1
    assert idle_page.closed is True
    assert "idle" not in service._thread_pages
    assert idle_page not in service._page_owners
    replacement = service._context.pages[-1]
    assert replacement is not idle_page
    assert replacement.closed is False
    assert replacement not in service._page_owners


def test_off_owner_invalidation_queues_handle_disposal(monkeypatch) -> None:
    service, page = _service()
    service.snapshot("task")
    handle = next(iter(service._observations.current("task").targets.values())).handle

    class _Owner:
        @staticmethod
        def is_alive() -> bool:
            return True

    service._pw_thread = _Owner()
    service.mark_waiting_approval("task", "Approve")

    assert handle.attached is True
    cleanup = service._work_q.get_nowait()
    assert cleanup.begin_dispatch() is True
    cleanup.fn()
    assert handle.attached is False


def test_stop_or_context_disconnect_invalidates_task_observations_and_previews() -> None:
    service, page = _service()
    service.snapshot("task")
    service._preview_by_thread["task"] = b"synthetic"
    service._run_on_pw_thread = lambda fn: "Browser action stopped by user."
    assert service.scroll("down", 1, "task") == "Browser action stopped by user."
    assert service._observations.current("task") is None

    service._run_on_pw_thread = lambda fn: fn()
    service.snapshot("task")
    service._preview_by_thread["task"] = b"synthetic"
    service._on_disconnected()
    assert service._observations.current("task") is None
    assert service._preview_by_thread == {}
    assert service._thread_pages == {}
