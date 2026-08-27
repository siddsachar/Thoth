from __future__ import annotations

from row_bot.browser.observation import (
    BrowserObservationRegistry,
    PROJECTION_LIMIT,
    StaleBrowserObservation,
    public_aria_snapshot_supported,
)


class _Handle:
    def __init__(self, label: str, *, attached: bool = True) -> None:
        self.label = label
        self.attached = attached
        self.disposed = False

    def evaluate(self, _script):
        return {
            "attached": self.attached,
            "visible": True,
            "tag": "button",
            "role": "button",
            "type": "",
            "label": self.label,
            "href": "https://example.test/path?token=hidden",
            "download": False,
            "form_action": "",
            "disabled": False,
            "value_length": 0,
            "in_dialog": False,
        }

    def dispose(self):
        self.disposed = True


class _Page:
    url = "https://example.test/path?token=page-secret"

    def __init__(self, handles) -> None:
        self.handles = handles

    def query_selector_all(self, _selector):
        return list(self.handles)

    def title(self):
        return "Synthetic page"


def _observe(registry, page, *, task="task", page_id="page", nav=1, context=1):
    return registry.observe(
        page,
        task_id=task,
        page_identity=page_id,
        navigation_generation=nav,
        context_generation=context,
    )


def test_tokens_are_snapshot_bound_opaque_and_old_handles_are_disposed() -> None:
    values = iter(("first", "second"))
    registry = BrowserObservationRegistry(token_factory=lambda: next(values))
    first_handle = _Handle("Continue")
    first = _observe(registry, _Page([first_handle]))
    first_token = next(iter(first.targets))
    assert first_token == "b1_first"
    rendered = registry.format(first)
    assert "token=hidden" not in rendered
    assert "page-secret" not in rendered

    second_handle = _Handle("Continue")
    second = _observe(registry, _Page([second_handle]))
    assert first_handle.disposed
    assert next(iter(second.targets)) == "b2_second"
    try:
        registry.resolve(first_token, task_id="task", page_identity="page", navigation_generation=1, context_generation=1)
    except StaleBrowserObservation:
        pass
    else:
        raise AssertionError("a cross-snapshot token must be stale")


def test_detached_drifted_page_navigation_and_context_targets_fail_before_dispatch() -> None:
    registry = BrowserObservationRegistry(token_factory=lambda: "token")
    handle = _Handle("Original")
    observation = _observe(registry, _Page([handle]))
    token = next(iter(observation.targets))
    handle.label = "Replacement"
    for values in (
        ("task", "page", 1, 1),
        ("task", "other", 1, 1),
        ("task", "page", 2, 1),
        ("task", "page", 1, 2),
    ):
        try:
            registry.resolve(token, task_id=values[0], page_identity=values[1], navigation_generation=values[2], context_generation=values[3])
        except StaleBrowserObservation:
            pass
        else:
            raise AssertionError("drift or generation mismatch must fail stale")


def test_observation_budgets_counts_and_public_aria_gate() -> None:
    handles = [_Handle(f"Button {index}") for index in range(PROJECTION_LIMIT + 25)]
    sequence = iter(str(index) for index in range(PROJECTION_LIMIT + 25))
    registry = BrowserObservationRegistry(token_factory=lambda: next(sequence))
    observation = _observe(registry, _Page(handles))
    assert observation.status.backend_received_count == PROJECTION_LIMIT + 25
    assert observation.status.projected_count == PROJECTION_LIMIT
    assert observation.status.locally_filtered_count == 25
    assert observation.status.provenance == "row_bot"
    assert len(registry.format(observation).encode("utf-8")) <= 32_768
    assert public_aria_snapshot_supported(_Page([])) is False


def test_complete_collector_caps_at_one_thousand_before_projection() -> None:
    handles = [_Handle(f"Action {index}") for index in range(1_025)]
    sequence = iter(str(index) for index in range(1_025))
    registry = BrowserObservationRegistry(token_factory=lambda: next(sequence))
    observation = _observe(registry, _Page(handles))

    assert observation.status.backend_received_count == 1_025
    assert observation.status.locally_validated_count == 1_000
    assert observation.status.projected_count == 160
    assert observation.status.locally_filtered_count == 865
    assert "element_limit" in observation.status.local_limit_reasons


class _LargeHandle(_Handle):
    def evaluate(self, script):
        value = super().evaluate(script)
        value["label"] = "L" * 512
        value["href"] = "https://example.test/" + "h" * 512
        value["form_action"] = "https://example.test/" + "f" * 512
        return value


def test_complete_collector_enforces_one_mibibyte_semantic_envelope() -> None:
    handles = [_LargeHandle(str(index)) for index in range(1_000)]
    sequence = iter(str(index) for index in range(1_000))
    registry = BrowserObservationRegistry(token_factory=lambda: next(sequence))
    observation = _observe(registry, _Page(handles))

    assert observation.status.locally_validated_count < 1_000
    assert observation.status.projected_count <= 160
    assert "semantic_byte_limit" in observation.status.local_limit_reasons
