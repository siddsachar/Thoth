from __future__ import annotations

import concurrent.futures

import pytest

from row_bot.computer_use.client import CuaClient
from row_bot.computer_use.policy import PolicyOutcome, classify_action
from row_bot.computer_use.service import (
    ActionReceipt,
    ComputerUseError,
    ComputerUseService,
    LeaseOwner,
    Observation,
)
from row_bot.tools.computer_use_tool import _computer_error_payload
from tests.fixtures.fake_cua import FakeCuaTransport


OWNER = LeaseOwner("thin-thread", "thin-generation", "thin-task")


def _window(
    window_id: int,
    *,
    app: str = "Media Client.exe",
    pid: int = 7001,
    on_screen: bool = True,
    active: bool = False,
) -> dict[str, object]:
    return {
        "window_id": window_id,
        "pid": pid,
        "app_name": app,
        "title": f"Window {window_id}",
        "bounds": {"x": 0, "y": 0, "width": 900, "height": 700},
        "is_on_screen": on_screen,
        "active": active,
    }


def _capture_generic(
    service: ComputerUseService,
    transport: FakeCuaTransport,
    elements: tuple[dict[str, object], ...],
    *,
    app: str = "Media Client.exe",
) -> tuple[str, Observation]:
    transport.scenario.apps = (
        {"name": app, "pid": 7001, "running": True, "active": True},
    )
    transport.scenario.windows = (_window(1, app=app),)
    transport.scenario.capture_pid = 7001
    transport.scenario.capture_window_id = 1
    transport.scenario.semantic_elements = elements
    observed = service.capture(owner=OWNER, app=app)
    return observed.target.target_id, observed


def _mutation_calls(transport: FakeCuaTransport) -> list[tuple[str, dict[str, object]]]:
    return [
        (name, args)
        for name, args in transport.calls
        if name
        in {
            "bring_to_front",
            "click",
            "double_click",
            "drag",
            "hotkey",
            "press_key",
            "right_click",
            "scroll",
            "set_value",
            "type_text",
        }
    ]


def test_friendly_name_resolves_single_active_matching_window_in_one_capture_call(
    service: ComputerUseService,
    fake_transport: FakeCuaTransport,
) -> None:
    fake_transport.scenario.apps = (
        {
            "name": "media-client.exe",
            "pid": 7001,
            "running": True,
            "active": True,
        },
    )
    fake_transport.scenario.windows = (_window(1, app="media-client.exe"),)
    fake_transport.scenario.capture_pid = 7001
    fake_transport.scenario.capture_window_id = 1

    observed = service.capture(owner=OWNER, app="Media Client")

    assert observed.target.window_id == 1
    names = [name for name, _args in fake_transport.calls]
    assert names.count("list_apps") == 1
    assert names.count("list_windows") == 1
    assert names.count("get_window_state") == 1


def test_single_on_screen_window_wins_over_off_screen_sibling(
    service: ComputerUseService,
    fake_transport: FakeCuaTransport,
) -> None:
    fake_transport.scenario.apps = (
        {"name": "Media Client.exe", "pid": 7001, "running": True, "active": True},
    )
    fake_transport.scenario.windows = (
        _window(1, on_screen=False),
        _window(2, on_screen=True),
    )
    fake_transport.scenario.capture_pid = 7001
    fake_transport.scenario.capture_window_id = 2

    observed = service.capture(owner=OWNER, app="Media Client")

    assert observed.target.window_id == 2
    assert [name for name, _args in fake_transport.calls].count("get_window_state") == 1


def test_single_active_window_wins_over_visible_sibling(
    service: ComputerUseService,
    fake_transport: FakeCuaTransport,
) -> None:
    fake_transport.scenario.apps = (
        {"name": "Media Client.exe", "pid": 7001, "running": True, "active": True},
    )
    fake_transport.scenario.windows = (
        _window(1),
        _window(2, active=True),
    )
    fake_transport.scenario.capture_pid = 7001
    fake_transport.scenario.capture_window_id = 2

    observed = service.capture(owner=OWNER, app="Media Client")

    assert observed.target.window_id == 2


def test_current_live_target_is_reused_without_rediscovery(
    service: ComputerUseService,
    fake_transport: FakeCuaTransport,
) -> None:
    fake_transport.scenario.apps = (
        {"name": "Media Client.exe", "pid": 7001, "running": True, "active": True},
    )
    fake_transport.scenario.windows = (_window(1),)
    fake_transport.scenario.capture_pid = 7001
    fake_transport.scenario.capture_window_id = 1
    first = service.capture(owner=OWNER, app="Media Client")
    calls_before = len(fake_transport.calls)

    second = service.capture(owner=OWNER, app="Media Client")

    assert second.target.target_id == first.target.target_id
    assert [name for name, _args in fake_transport.calls[calls_before:]] == [
        "get_window_state"
    ]


def test_combobox_token_type_dispatches_once_and_mutates_only_target(
    service: ComputerUseService,
    fake_transport: FakeCuaTransport,
) -> None:
    target_id, observed = _capture_generic(
        service,
        fake_transport,
        (
            {"role": "TextField", "label": "Decoy Field", "value": "decoy"},
            {"role": "ComboBox", "label": "Editable Combo", "value": "query"},
        ),
    )
    calls_before = len(fake_transport.calls)

    receipt = service.act(
        "type",
        target_id,
        OWNER,
        element_token=observed.elements[1].token,
        text=" result",
    )

    assert isinstance(receipt, ActionReceipt)
    calls = fake_transport.calls[calls_before:]
    assert [name for name, _args in calls] == ["type_text"]
    assert fake_transport.value_for_label("Editable Combo") == "query result"
    assert fake_transport.value_for_label("Decoy Field") == "decoy"


@pytest.mark.parametrize("role", ["DataItem", "GridCell"])
def test_grid_cell_roles_reach_cua_once(
    service: ComputerUseService,
    fake_transport: FakeCuaTransport,
    role: str,
) -> None:
    target_id, observed = _capture_generic(
        service,
        fake_transport,
        ({"role": role, "label": "Native Grid R4C2", "value": ""},),
        app="Native Grid.exe",
    )
    calls_before = len(fake_transport.calls)

    service.act(
        "type",
        target_id,
        OWNER,
        element_token=observed.elements[0].token,
        text="bounded value",
    )

    assert [name for name, _args in fake_transport.calls[calls_before:]] == [
        "type_text"
    ]
    assert fake_transport.value_for_label("Native Grid R4C2") == "bounded value"


def test_unknown_interactive_role_reaches_cua_but_explicit_local_blocks_do_not(
    service: ComputerUseService,
    fake_transport: FakeCuaTransport,
) -> None:
    target_id, observed = _capture_generic(
        service,
        fake_transport,
        (
            {"role": "VendorInteractive", "label": "Custom editor"},
            {"role": "Edit", "label": "Disabled", "enabled": False},
            {"role": "Edit", "label": "Read only", "read_only": True},
            {"role": "Pane", "label": "Structural container"},
            {"role": "PasswordField", "label": "Password"},
        ),
    )

    service.act(
        "type",
        target_id,
        OWNER,
        element_token=observed.elements[0].token,
        text="safe",
    )
    assert [name for name, _args in _mutation_calls(fake_transport)].count(
        "type_text"
    ) == 1

    for element in observed.elements[1:]:
        before = len(_mutation_calls(fake_transport))
        with pytest.raises(ComputerUseError) as rejected:
            service.act(
                "type",
                target_id,
                OWNER,
                element_token=element.token,
                text="blocked",
            )
        assert rejected.value.code in {"handoff_required", "invalid_input"}
        assert len(_mutation_calls(fake_transport)) == before


def test_current_token_type_is_one_mutation_and_zero_hidden_captures(
    service: ComputerUseService,
    fake_transport: FakeCuaTransport,
) -> None:
    target_id, observed = _capture_generic(
        service,
        fake_transport,
        ({"role": "TextField", "label": "Search", "value": ""},),
        app="Web Search.exe",
    )
    calls_before = len(fake_transport.calls)

    service.act(
        "type",
        target_id,
        OWNER,
        element_token=observed.elements[0].token,
        text="query",
    )

    assert [name for name, _args in fake_transport.calls[calls_before:]] == [
        "type_text"
    ]


def test_stale_refusal_preserves_error_and_supplies_one_refreshed_observation(
    service: ComputerUseService,
    fake_transport: FakeCuaTransport,
) -> None:
    target_id, observed = _capture_generic(
        service,
        fake_transport,
        ({"role": "ComboBox", "label": "Web Search", "value": ""},),
    )
    fake_transport.scenario.stale = True
    calls_before = len(fake_transport.calls)

    with pytest.raises(ComputerUseError) as stale:
        service.act(
            "type",
            target_id,
            OWNER,
            element_token=observed.elements[0].token,
            text="query",
        )

    assert stale.value.code == "stale_observation"
    assert isinstance(getattr(stale.value, "observation", None), Observation)
    assert [name for name, _args in fake_transport.calls[calls_before:]] == [
        "type_text",
        "get_window_state",
    ]
    assert service.status_snapshot().get("consecutive_failures", 0) == 0


def test_fresh_token_retry_after_stale_reaches_driver_and_never_becomes_no_progress(
    service: ComputerUseService,
    fake_transport: FakeCuaTransport,
) -> None:
    target_id, observed = _capture_generic(
        service,
        fake_transport,
        ({"role": "ComboBox", "label": "Web Search", "value": ""},),
    )
    fake_transport.scenario.stale = True
    with pytest.raises(ComputerUseError) as stale:
        service.act(
            "type",
            target_id,
            OWNER,
            element_token=observed.elements[0].token,
            text="first",
        )
    fresh = stale.value.observation

    result = service.act(
        "type",
        target_id,
        OWNER,
        element_token=fresh.elements[0].token,
        text="second",
    )

    assert result.action_dispatched is True
    assert [name for name, _args in fake_transport.calls].count("type_text") == 2


def test_three_unrelated_recoverable_errors_never_close_computer_session(
    service: ComputerUseService,
    fake_transport: FakeCuaTransport,
) -> None:
    target_id, observed = _capture_generic(
        service,
        fake_transport,
        ({"role": "TextField", "label": "Field", "value": ""},),
    )
    for action, kwargs in (
        ("click", {"x": -1, "y": 0}),
        ("type", {"element_token": "stale-token", "text": "x"}),
        ("key", {"keys": "ctrl+alt+delete"}),
    ):
        with pytest.raises(ComputerUseError) as failure:
            service.act(action, target_id, OWNER, **kwargs)
        assert failure.value.code != "no_progress"

    result = service.act(
        "type",
        target_id,
        OWNER,
        element_token=observed.elements[0].token,
        text="still open",
    )
    assert result.action_dispatched is True
    assert service.status_snapshot()["active"] is True


def test_delivered_unverified_never_blocks_enter_capture_click_or_another_field(
    service: ComputerUseService,
    fake_transport: FakeCuaTransport,
) -> None:
    fake_transport.scenario.delivery_profile = "web_targeted_unverifiable"
    target_id, observed = _capture_generic(
        service,
        fake_transport,
        (
            {"role": "TextField", "label": "First field", "value": ""},
            {"role": "TextField", "label": "Later field", "value": ""},
            {"role": "Button", "label": "Next"},
        ),
    )

    first = service.act(
        "type",
        target_id,
        OWNER,
        element_token=observed.elements[0].token,
        text="first",
    )
    enter = service.act("key", target_id, OWNER, keys="enter")
    fresh = service.capture(target_id, OWNER)
    clicked = service.act(
        "click",
        target_id,
        OWNER,
        element_token=fresh.elements[2].token,
    )
    later = service.act(
        "type",
        target_id,
        OWNER,
        element_token=fresh.elements[1].token,
        text="later",
    )

    assert first.action_dispatched is True and first.effect_verified is False
    assert enter.action_dispatched is True
    assert clicked.action_dispatched is True
    assert later.action_dispatched is True
    assert [name for name, _args in fake_transport.calls].count("type_text") == 2


def test_routine_enter_is_unprompted_but_semantic_submit_enter_still_asks(
    fake_client: CuaClient,
    fake_transport: FakeCuaTransport,
) -> None:
    approvals: list[dict[str, object]] = []
    service = ComputerUseService(
        client_factory=lambda: fake_client,
        approval_callback=lambda payload: approvals.append(payload) or True,
    )
    target_id, observed = _capture_generic(
        service,
        fake_transport,
        (
            {"role": "TextField", "label": "Current edit"},
            {"role": "Button", "label": "Send"},
        ),
    )
    approvals.clear()

    service.act(
        "key",
        target_id,
        OWNER,
        element_token=observed.elements[0].token,
        keys="enter",
        expected_effect="Commit the current edit",
    )
    assert approvals == []
    assert [name for name, _args in fake_transport.calls].count("press_key") == 1

    service.act(
        "key",
        target_id,
        OWNER,
        element_token=observed.elements[1].token,
        keys="enter",
        expected_effect="Send the message",
    )
    assert len(approvals) == 1


def test_replace_text_is_one_call_by_default_and_one_optional_capture_at_most(
    service: ComputerUseService,
    fake_transport: FakeCuaTransport,
) -> None:
    target_id, observed = _capture_generic(
        service,
        fake_transport,
        ({"role": "GridCell", "label": "Native Grid R2C1", "value": "old"},),
        app="Native Grid.exe",
    )
    calls_before = len(fake_transport.calls)

    receipt = service.act(
        "replace_text",
        target_id,
        OWNER,
        element_token=observed.elements[0].token,
        text="new",
    )
    assert isinstance(receipt, ActionReceipt)
    assert [name for name, _args in fake_transport.calls[calls_before:]] == [
        "set_value"
    ]

    fresh = service.capture(target_id, OWNER)
    calls_before = len(fake_transport.calls)
    verified = service.act(
        "replace_text",
        target_id,
        OWNER,
        element_token=fresh.elements[0].token,
        text="newer",
        capture_after=True,
    )
    assert isinstance(verified, Observation)
    assert [name for name, _args in fake_transport.calls[calls_before:]].count(
        "get_window_state"
    ) <= 1
    assert [name for name, _args in fake_transport.calls[calls_before:]].count(
        "set_value"
    ) == 1


@pytest.mark.parametrize(
        ("profile", "in_web", "verified"),
        [
            ("native_exact_set_value", False, True),
            ("web_targeted_unverifiable", True, True),
            ("catalyst_value_unavailable", False, False),
        ],
)
def test_optional_exact_value_verification_profiles_remain_nonblocking(
    service: ComputerUseService,
    fake_transport: FakeCuaTransport,
    profile: str,
    in_web: bool,
    verified: bool,
) -> None:
    fake_transport.scenario.delivery_profile = profile
    target_id, observed = _capture_generic(
        service,
        fake_transport,
        (
            {
                "role": "GridCell",
                "label": "Native Grid R2C1",
                "value": "old",
                "in_web_content": in_web,
            },
        ),
    )

    result = service.act(
        "replace_text",
        target_id,
        OWNER,
        element_token=observed.elements[0].token,
        text="new",
        capture_after=True,
    )
    followup = service.act("key", target_id, OWNER, keys="enter")

    assert result.effect_verified is verified
    assert result.verified_scope == ("exact_value" if verified else "")
    assert followup.action_dispatched is True


def test_routine_capture_has_zero_vision_calls_and_explicit_vision_has_one(
    fake_client: CuaClient,
    fake_transport: FakeCuaTransport,
) -> None:
    class Vision:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def analyze(self, _image: bytes, question: str) -> str:
            self.calls.append(question)
            return "visual answer"

    vision = Vision()
    service = ComputerUseService(
        client_factory=lambda: fake_client,
        approval_callback=lambda _payload: True,
        vision_service=vision,
    )
    target_id, _observed = _capture_generic(
        service,
        fake_transport,
        ({"role": "Button", "label": "Open"},),
    )
    assert vision.calls == []

    service.capture(target_id, OWNER, visual_question="What is visible?")
    assert vision.calls == ["What is visible?"]


def test_large_tree_exact_semantic_filter_returns_named_cell_without_coordinates(
    service: ComputerUseService,
    fake_transport: FakeCuaTransport,
) -> None:
    fake_transport.scenario.semantic_elements = tuple(
        {
            "role": "DataItem",
            "label": f"Native Grid R{index}C1",
            "value": "",
        }
        for index in range(1, 181)
    )
    fake_transport.scenario.apps = (
        {"name": "Native Grid.exe", "pid": 7001, "running": True, "active": True},
    )
    fake_transport.scenario.windows = (_window(1, app="Native Grid.exe"),)
    fake_transport.scenario.capture_pid = 7001
    fake_transport.scenario.capture_window_id = 1

    observed = service.capture(
        owner=OWNER,
        app="Native Grid",
        semantic_label="Native Grid R179C1",
        semantic_role="DataItem",
    )

    projected, omitted = observed.model_elements()
    assert [(item.role, item.label) for item in projected] == [
        ("DataItem", "Native Grid R179C1")
    ]
    assert omitted == 179
    assert "x=" not in observed.model_text()


def test_large_tree_semantic_filter_refuses_multiple_exact_matches(
    service: ComputerUseService,
    fake_transport: FakeCuaTransport,
) -> None:
    target_id, current = _capture_generic(
        service,
        fake_transport,
        ({"role": "Button", "label": "Current action"},),
    )
    fake_transport.scenario.rotate_element_tokens = True
    fake_transport.scenario.semantic_elements = (
        {"role": "Button", "label": "Unrelated action"},
        {"role": "DataItem", "label": "Named item", "selected": True},
        {"role": "DataItem", "label": "Named item", "selected": False},
    )
    generation_before = current.generation
    calls_before = len(fake_transport.calls)

    with pytest.raises(ComputerUseError) as ambiguous:
        service.capture(
            target_id,
            OWNER,
            semantic_label="Named item",
            semantic_role="DataItem",
        )

    assert ambiguous.value.code == "ambiguous_target"
    fresh = service.current_observation(target_id)
    assert fresh is ambiguous.value.observation
    assert fresh is not current
    assert fresh.generation == generation_before + 1
    assert [name for name, _args in fake_transport.calls[calls_before:]] == [
        "get_window_state"
    ]
    assert [candidate["token"] for candidate in ambiguous.value.candidates] == [
        fresh.elements[1].token,
        fresh.elements[2].token,
    ]
    assert all(candidate["label"] == "Named item" for candidate in ambiguous.value.candidates)
    assert all(candidate["role"] == "DataItem" for candidate in ambiguous.value.candidates)
    assert [candidate["selected"] for candidate in ambiguous.value.candidates] == [
        True,
        False,
    ]


def test_semantic_filter_no_match_keeps_fresh_unfiltered_capture_current(
    service: ComputerUseService,
    fake_transport: FakeCuaTransport,
) -> None:
    target_id, current = _capture_generic(
        service,
        fake_transport,
        ({"role": "Button", "label": "Current action"},),
    )
    fake_transport.scenario.rotate_element_tokens = True
    fake_transport.scenario.semantic_elements = (
        {"role": "Button", "label": "Different action"},
        {"role": "Slider", "label": "Level"},
    )
    generation_before = current.generation
    calls_before = len(fake_transport.calls)

    with pytest.raises(ComputerUseError) as missing:
        service.capture(
            target_id,
            OWNER,
            semantic_label="Missing action",
            semantic_role="Button",
        )

    assert missing.value.code == "semantic_no_match"
    fresh = service.current_observation(target_id)
    assert fresh is missing.value.observation
    assert fresh is not current
    assert fresh.generation == generation_before + 1
    assert [element.label for element in fresh.model_elements()[0]] == [
        "Different action",
        "Level",
    ]
    assert [name for name, _args in fake_transport.calls[calls_before:]] == [
        "get_window_state"
    ]


def test_exact_value_scope_is_not_formula_navigation_or_task_completion(
    service: ComputerUseService,
    fake_transport: FakeCuaTransport,
) -> None:
    fake_transport.scenario.delivery_profile = "native_exact_set_value"
    target_id, observed = _capture_generic(
        service,
        fake_transport,
        ({"role": "GridCell", "label": "Named Cell", "value": ""},),
    )

    result = service.act(
        "replace_text",
        target_id,
        OWNER,
        element_token=observed.elements[0].token,
        text="literal expression",
        capture_after=True,
    )

    assert result.verified_scope == "exact_value"
    rendered = result.model_text().casefold()
    assert "formula" not in rendered
    assert "navigation" not in rendered
    assert "not overall task completion" in rendered


def test_background_refusal_permits_one_foreground_driver_action_without_focus_call(
    service: ComputerUseService,
    fake_transport: FakeCuaTransport,
) -> None:
    fake_transport.scenario.background_unavailable_tools = frozenset({"type_text"})
    target_id, observed = _capture_generic(
        service,
        fake_transport,
        ({"role": "ComboBox", "label": "Web Search", "value": ""},),
    )
    calls_before = len(fake_transport.calls)

    service.act(
        "type",
        target_id,
        OWNER,
        element_token=observed.elements[0].token,
        text="query",
        approval_mode="allow_all",
    )

    calls = fake_transport.calls[calls_before:]
    assert [name for name, _args in calls] == ["type_text", "type_text"]
    assert calls[1][1]["delivery_mode"] == "foreground"
    assert "bring_to_front" not in [name for name, _args in calls]


def test_background_key_refusal_permits_exactly_one_same_action_foreground_retry(
    service: ComputerUseService,
    fake_transport: FakeCuaTransport,
) -> None:
    fake_transport.scenario.background_unavailable_tools = frozenset({"press_key"})
    target_id, _observed = _capture_generic(
        service,
        fake_transport,
        ({"role": "Button", "label": "Current action"},),
    )
    calls_before = len(fake_transport.calls)

    service.act(
        "key",
        target_id,
        OWNER,
        keys="enter",
        approval_mode="allow_all",
    )

    calls = fake_transport.calls[calls_before:]
    assert [name for name, _args in calls] == ["press_key", "press_key"]
    assert calls[1][1]["delivery_mode"] == "foreground"
    assert all(name not in {"bring_to_front", "click", "get_window_state"} for name, _args in calls)


def test_calculator_key_sequence_uses_one_foreground_retry_without_preparation(
    service: ComputerUseService,
    fake_transport: FakeCuaTransport,
) -> None:
    fake_transport.scenario.calculator_semantics = True
    fake_transport.scenario.background_unavailable_tools = frozenset({"click"})
    service.acquire(OWNER, validate_context=False)
    target_id = service.list_windows(OWNER, app="Calculator")[0]["target_id"]
    service.capture(target_id, OWNER)
    calls_before = len(fake_transport.calls)

    service.act_key_sequence(
        target_id,
        "7",
        OWNER,
        approval_mode="allow_all",
    )

    calls = fake_transport.calls[calls_before:]
    assert [name for name, _args in calls] == ["click", "click", "get_window_state"]
    assert calls[1][1]["delivery_mode"] == "foreground"
    assert [name for name, _args in calls].count("click") == 2
    assert "bring_to_front" not in [name for name, _args in calls]


def test_second_foreground_key_refusal_is_terminal_and_requires_takeover(
    service: ComputerUseService,
    fake_transport: FakeCuaTransport,
) -> None:
    fake_transport.scenario.background_unavailable_tools = frozenset({"press_key"})
    fake_transport.scenario.foreground_error_code = "foreground_required"
    target_id, _observed = _capture_generic(
        service,
        fake_transport,
        ({"role": "Button", "label": "Current action"},),
    )
    calls_before = len(fake_transport.calls)

    with pytest.raises(ComputerUseError) as refused:
        service.act(
            "key",
            target_id,
            OWNER,
            keys="enter",
            approval_mode="allow_all",
        )

    calls = fake_transport.calls[calls_before:]
    assert [name for name, _args in calls] == ["press_key", "press_key"]
    assert refused.value.code == "background_unavailable"
    assert refused.value.terminal is True
    payload = __import__("json").loads(_computer_error_payload("key", refused.value))
    assert payload["terminal"] is True
    assert "take over" in payload["remediation"].casefold()


def test_stop_takeover_and_cancel_never_rewrite_prior_unverified_receipt(
    service: ComputerUseService,
    fake_transport: FakeCuaTransport,
) -> None:
    fake_transport.scenario.delivery_profile = "web_targeted_unverifiable"
    target_id, observed = _capture_generic(
        service,
        fake_transport,
        ({"role": "TextField", "label": "Field", "value": ""},),
    )
    receipt = service.act(
        "type",
        target_id,
        OWNER,
        element_token=observed.elements[0].token,
        text="uncertain",
    )
    assert receipt.action_dispatched is True
    assert receipt.effect_verified is False

    service.take_over(thread_id=OWNER.thread_id, generation_id=OWNER.generation_id)
    assert receipt.effect_verified is False
    service.stop()
    assert receipt.effect_verified is False

    service.acquire(OWNER, validate_context=False)
    service._cancel.set()
    with pytest.raises(concurrent.futures.CancelledError):
        service._check_cancelled()
    assert receipt.effect_verified is False


def test_latest_failure_shape_fresh_retry_reaches_cua_instead_of_no_progress(
    service: ComputerUseService,
    fake_transport: FakeCuaTransport,
) -> None:
    target_id, observed = _capture_generic(
        service,
        fake_transport,
        (
            {"role": "Pane", "label": "Structural"},
            {"role": "ComboBox", "label": "Web Search", "value": ""},
        ),
        app="Web Search.exe",
    )
    with pytest.raises(ComputerUseError) as invalid:
        service.act(
            "type",
            target_id,
            OWNER,
            element_token=observed.elements[0].token,
            text="invalid",
        )
    assert invalid.value.code == "invalid_input"

    fake_transport.scenario.stale = True
    with pytest.raises(ComputerUseError) as stale:
        service.act(
            "replace_text",
            target_id,
            OWNER,
            element_token=observed.elements[1].token,
            text="stale replacement",
        )
    assert stale.value.code == "stale_observation"

    fresh = service.capture(target_id, OWNER)
    calls_before = len(fake_transport.calls)
    result = service.act(
        "type",
        target_id,
        OWNER,
        element_token=fresh.elements[1].token,
        text="fresh retry",
    )

    assert result.action_dispatched is True
    assert [name for name, _args in fake_transport.calls[calls_before:]] == [
        "type_text"
    ]
    assert service.status_snapshot()["state"] != "needs_attention"


def test_policy_keeps_routine_coordinates_focus_and_enter_but_protects_effects() -> None:
    assert (
        classify_action(
            "click",
            app_name="Media Client",
            coordinate_only=True,
            expected_effect="Select the current item",
        ).outcome
        is PolicyOutcome.ROUTINE
    )
    assert (
        classify_action(
            "focus",
            app_name="Media Client",
            foreground=True,
        ).outcome
        is PolicyOutcome.ROUTINE
    )
    assert (
        classify_action(
            "key",
            app_name="Native Grid",
            role="DataItem",
            label="Current edit",
            keys="enter",
            expected_effect="Commit the current edit",
        ).outcome
        is PolicyOutcome.ROUTINE
    )
    assert (
        classify_action(
            "key",
            app_name="Media Client",
            role="Button",
            label="Send",
            keys="enter",
            expected_effect="Send the message",
        ).outcome
        is PolicyOutcome.CONSEQUENTIAL
    )
    assert (
        classify_action(
            "type",
            app_name="Media Client",
            role="PasswordField",
            label="Password",
        ).outcome
        is PolicyOutcome.HANDOFF
    )
    assert (
        classify_action("click", app_name="Terminal").outcome
        is PolicyOutcome.BLOCKED
    )
