from __future__ import annotations

from row_bot.browser.service import BrowserSession


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

    def fill(self, text, timeout):
        self.filled.append(text)

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


def test_submit_navigation_returns_exactly_one_post_action_observation() -> None:
    service, page = _service()
    service.snapshot("task")
    token = next(iter(service._observations.current("task").targets))
    before = service._observations.observation_count
    result = service.type_text(token, "hidden", True, "task")
    assert "https://example.test/results" in result
    assert service._observations.observation_count == before + 1


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
