from __future__ import annotations

import pytest

from row_bot.computer_use.service import (
    ActionReceipt,
    ComputerUseError,
    LeaseOwner,
    StaleObservationError,
)


OWNER = LeaseOwner("actions-thread", "actions-generation", "actions-task")


def _target_and_capture(service):
    service.acquire(OWNER, validate_context=False)
    target = service.list_windows(OWNER, app="Calculator")[0]["target_id"]
    return target, service.capture(target, OWNER)


@pytest.mark.parametrize(
    ("action", "kwargs", "driver_tool"),
    [
        ("click", {"element": 0}, "click"),
        ("double_click", {"element": 0}, "double_click"),
        ("right_click", {"element": 0}, "right_click"),
        ("type", {"element": 2, "text": "private typed value"}, "type_text"),
        ("key", {"keys": "tab"}, "press_key"),
        ("key", {"keys": "ctrl+a"}, "hotkey"),
        ("scroll", {"direction": "down", "amount": 3}, "scroll"),
        ("drag", {"x": 0, "y": 0, "end_x": 0, "end_y": 0}, "drag"),
    ],
)
def test_every_routine_mutation_maps_once_without_an_implicit_post_capture(service, fake_transport, action, kwargs, driver_tool) -> None:
    target, observation = _target_and_capture(service)
    call_kwargs = dict(kwargs)
    index = call_kwargs.pop("element", None)
    if index is not None:
        call_kwargs["element_token"] = observation.elements[index].token
    result = service.act(action, target, OWNER, **call_kwargs)
    names = [name for name, _arguments in fake_transport.calls]
    mutation_index = max(i for i, name in enumerate(names) if name == driver_tool)
    assert names[mutation_index + 1:] == []
    assert isinstance(result, ActionReceipt)
    assert result.action_dispatched is True
    assert result.action_completed is True
    assert result.driver_effect == "confirmed"
    assert result.visual_change == "unknown"
    assert result.effect_verified is True
    assert "private typed value" not in repr(result)
    assert service.ephemeral_screenshot()


def test_capture_after_performs_exactly_one_fresh_post_action_capture(
    service,
    fake_transport,
) -> None:
    target, observation = _target_and_capture(service)
    calls_before = len(fake_transport.calls)
    captures_before = service.performance_snapshot()["captures"]

    result = service.act(
        "click",
        target,
        OWNER,
        element_token=observation.elements[0].token,
        capture_after=True,
    )

    assert result.screenshot
    assert [name for name, _args in fake_transport.calls[calls_before:]] == [
        "click",
        "get_window_state",
    ]
    assert service.performance_snapshot()["captures"] == captures_before + 1


def test_general_canvas_drag_uses_one_native_action_and_one_requested_capture(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.windows = (
        {
            "window_id": 303,
            "pid": 5303,
            "app_name": "Paint",
            "title": "Untitled - Paint",
            "bounds": {"x": 10, "y": 10, "width": 800, "height": 600},
            "is_on_screen": True,
        },
    )
    service.acquire(OWNER, validate_context=False)
    target = service.list_windows(OWNER, app="Paint")[0]["target_id"]
    service.capture(target, OWNER)
    calls_before = len(fake_transport.calls)

    result = service.act(
        "drag",
        target,
        OWNER,
        x=0,
        y=0,
        end_x=0,
        end_y=0,
        capture_after=True,
    )

    assert result.target.app_name == "Paint"
    assert [name for name, _args in fake_transport.calls[calls_before:]] == [
        "drag",
        "get_window_state",
    ]


def test_failed_action_never_performs_post_action_capture(service, fake_transport) -> None:
    target, observation = _target_and_capture(service)
    fake_transport.scenario.action_error_code = "action_failed"
    calls_before = len(fake_transport.calls)

    with pytest.raises(ComputerUseError, match="refused the requested action safely"):
        service.act(
            "click",
            target,
            OWNER,
            element_token=observation.elements[0].token,
            capture_after=True,
        )

    assert [name for name, _args in fake_transport.calls[calls_before:]] == ["click"]


def test_three_consecutive_driver_failures_end_with_computer_no_progress(
    service,
    fake_transport,
) -> None:
    target, observation = _target_and_capture(service)
    fake_transport.scenario.action_error_code = "action_failed"

    for attempt in range(3):
        if attempt:
            observation = service.capture(target, OWNER)
        expected = "no progress" if attempt == 2 else "refused the requested action safely"
        with pytest.raises(ComputerUseError, match=expected):
            service.act(
                "click",
                target,
                OWNER,
                element_token=observation.elements[0].token,
            )

    snapshot = service.status_snapshot()
    assert snapshot["state"] == "needs_attention"
    assert snapshot["consecutive_failures"] == 3
    assert [name for name, _args in fake_transport.calls].count("click") == 3


def test_stale_recovery_is_limited_to_one_exact_target_recapture(
    service,
    fake_transport,
) -> None:
    target, observation = _target_and_capture(service)
    fake_transport.scenario.stale = True
    with pytest.raises(StaleObservationError):
        service.act(
            "click",
            target,
            OWNER,
            element_token=observation.elements[0].token,
        )

    observation = service.capture(target, OWNER)
    fake_transport.scenario.stale = True
    with pytest.raises(ComputerUseError, match="no progress") as exc_info:
        service.act(
            "click",
            target,
            OWNER,
            element_token=observation.elements[0].token,
        )

    assert exc_info.value.code == "no_progress"
    assert [name for name, _args in fake_transport.calls].count("click") == 2


def test_focus_is_confirmed_once_and_prepares_without_an_implicit_capture(service, fake_transport) -> None:
    target, _ = _target_and_capture(service)
    result = service.act("focus", target, OWNER, expected_effect="Bring Calculator forward")
    names = [name for name, _args in fake_transport.calls]
    assert names.count("bring_to_front") == 1
    assert names[-1] == "bring_to_front"
    assert isinstance(result, ActionReceipt)
    assert result.action_completed is True
    assert service.status_snapshot()["foreground_prepared"] is True


def test_approval_wait_recaptures_and_rebinds_semantic_target(fake_client, fake_transport) -> None:
    approvals = []
    from row_bot.computer_use.service import ComputerUseService

    service = ComputerUseService(client_factory=lambda: fake_client, approval_callback=lambda payload: approvals.append(payload) or True)
    target, observation = _target_and_capture(service)
    service.act("click", target, OWNER, element_token=observation.elements[1].token, expected_effect="Submit calculation")
    names = [name for name, _args in fake_transport.calls]
    click_index = names.index("click")
    assert names[click_index - 1] == "get_window_state"
    assert approvals[-1]["always_confirm"] is True


def test_stale_driver_token_fails_closed_without_retry(service, fake_transport) -> None:
    target, observation = _target_and_capture(service)
    fake_transport.scenario.stale = True
    with pytest.raises(StaleObservationError):
        service.act("click", target, OWNER, element_token=observation.elements[0].token)
    assert [name for name, _args in fake_transport.calls].count("click") == 1


def test_block_mode_denies_routine_input_after_observation(service) -> None:
    target, observation = _target_and_capture(service)
    with pytest.raises(ComputerUseError, match="Block approval mode"):
        service.act(
            "click",
            target,
            OWNER,
            element_token=observation.elements[0].token,
            approval_mode="block",
        )


def test_launch_requires_app_scope_and_captures_after_launch(fake_client, fake_transport) -> None:
    approvals = []
    from row_bot.computer_use.service import ComputerUseService

    service = ComputerUseService(client_factory=lambda: fake_client, approval_callback=lambda payload: approvals.append(payload) or True)
    service.acquire(OWNER, validate_context=False)
    windows = service.launch_app("Calculator", OWNER)
    assert windows
    names = [name for name, _args in fake_transport.calls]
    assert names[names.index("launch_app") + 1] == "get_window_state"
    assert "list_windows" not in names
    capture_args = next(args for tool, args in fake_transport.calls if tool == "get_window_state")
    assert capture_args["pid"] == 4242
    assert names[-1] == "get_window_state"
    assert approvals[0]["action"] == "task_session_app_permission"


def test_launch_rejects_paths_urls_and_arguments(service) -> None:
    service.acquire(OWNER, validate_context=False)
    for value in ("C:\\Windows\\calc.exe", "https://example.com", "Calculator --unsafe"):
        with pytest.raises(ComputerUseError):
            service.launch_app(value, OWNER)


def test_bounded_routine_key_sequence_checks_each_step_and_captures_once(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.calculator_semantics = True
    target, observation = _target_and_capture(service)
    progress: list[str] = []
    service.add_listener(lambda snapshot: progress.append(str(snapshot["last_action"])))
    calls_before = len(fake_transport.calls)
    captures_before = service.performance_snapshot()["captures"]

    verified = service.act_key_sequence(target, "7,multiply,8,equals", OWNER)

    calls = fake_transport.calls[calls_before:]
    token_by_label = {element.label: element.token for element in observation.elements}
    assert [args["element_token"] for name, args in calls if name == "click"] == [
        token_by_label["Seven"],
        token_by_label["Multiply by"],
        token_by_label["Eight"],
        token_by_label["Equals"],
    ]
    assert not [args for name, args in calls if name == "press_key"]
    assert [name for name, _args in calls][-1] == "get_window_state"
    assert [name for name, _args in calls].count("get_window_state") == 1
    assert service.performance_snapshot()["captures"] == captures_before + 1
    assert "Display 56" in verified.model_text()
    assert "7,multiply,8,equals" not in str(service.status_snapshot())
    assert [item for item in progress if item.startswith("Calculator step")] == [
        "Calculator step 1/4 (values hidden)",
        "Calculator step 2/4 (values hidden)",
        "Calculator step 3/4 (values hidden)",
        "Calculator step 4/4 (values hidden)",
    ]


def test_routine_key_sequence_requires_all_semantic_buttons_before_mutation(
    service,
    fake_transport,
) -> None:
    target, _observation = _target_and_capture(service)
    clicks_before = sum(1 for name, _args in fake_transport.calls if name == "click")

    with pytest.raises(ComputerUseError, match="semantic Calculator button"):
        service.act_key_sequence(target, "7,*,8,=", OWNER)

    assert sum(1 for name, _args in fake_transport.calls if name == "click") == clicks_before


def test_routine_key_sequence_stale_button_fails_without_retry(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.calculator_semantics = True
    target, _observation = _target_and_capture(service)
    fake_transport.scenario.stale = True

    with pytest.raises(StaleObservationError):
        service.act_key_sequence(target, "7,*,8,=", OWNER)

    assert [name for name, _args in fake_transport.calls].count("click") == 1


@pytest.mark.parametrize(
    "keys",
    [
        "",
        "7,enter",
        "7,tab",
        "ctrl+a",
        "secret",
        ",".join("1" for _ in range(17)),
    ],
)
def test_routine_key_sequence_rejects_navigation_text_chords_and_oversize(
    service,
    fake_transport,
    keys: str,
) -> None:
    target, _observation = _target_and_capture(service)
    mutations_before = sum(
        1 for name, _args in fake_transport.calls if name in {"press_key", "click"}
    )
    with pytest.raises(ComputerUseError):
        service.act_key_sequence(target, keys, OWNER)
    mutations_after = sum(
        1 for name, _args in fake_transport.calls if name in {"press_key", "click"}
    )
    assert mutations_after == mutations_before


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        ("7,*,8,=", ("7", "*", "8", "=")),
        ("7,multiply,8,equals", ("7", "*", "8", "=")),
        ("7×8=", ("7", "*", "8", "=")),
        ("123 + 456 =", ("1", "2", "3", "+", "4", "5", "6", "=")),
        ("9÷3=", ("9", "/", "3", "=")),
    ],
)
def test_routine_key_sequence_normalizes_bounded_provider_shapes(
    keys: str,
    expected: tuple[str, ...],
) -> None:
    from row_bot.computer_use.service import ComputerUseService

    assert ComputerUseService.normalize_routine_keys(keys) == expected


@pytest.mark.parametrize("keys", ["7\n+8=", "7\t+8=", "ctrl+a", "hello=", "12345678901234567"])
def test_compact_routine_key_sequence_stays_bounded_and_non_navigational(keys: str) -> None:
    from row_bot.computer_use.service import ComputerUseService

    with pytest.raises(ComputerUseError):
        ComputerUseService.normalize_routine_keys(keys)


def _capability_service(fake_transport, *capabilities: str):
    from row_bot.computer_use.client import CuaClient
    from row_bot.computer_use.service import ComputerUseService

    client = CuaClient(
        "fake-cua-driver.exe",
        session_id="capability-session",
        contract_version="0.19.3",
        capabilities=frozenset(capabilities),
        transport_factory=lambda *_args: fake_transport,
    )
    return ComputerUseService(
        client_factory=lambda: client,
        approval_callback=lambda _payload: True,
    )


def test_service_derived_verify_state_is_exact_bounded_and_screenshot_free(fake_transport) -> None:
    service = _capability_service(fake_transport, "verify_state")
    fake_transport.scenario.apps = (
        {"name": "Notepad", "running": True, "active": False},
    )
    service.acquire(OWNER, validate_context=False)
    service.list_apps(OWNER)
    target = service.list_windows(OWNER, app="Notepad")[0]["target_id"]
    service.capture(target, OWNER)

    service.act("key", target, OWNER, keys="a", approval_mode="allow_all")

    verify_calls = [args for name, args in fake_transport.calls if name == "verify_state"]
    assert verify_calls == [
        {
            "pid": 4343,
            "window_id": 102,
            "expect": [{"window": {"exists": True}}],
            "timeout_ms": 0,
            "stable_samples": 1,
            "include_screenshot": False,
            "session": "capability-session",
        }
    ]


def test_exact_menu_is_capability_gated_and_never_falls_back_to_coordinates(
    service,
    fake_transport,
) -> None:
    target, _observation = _target_and_capture(service)
    with pytest.raises(ComputerUseError) as unavailable:
        service.act_menu(target, ["View", "Zoom In"], OWNER)
    assert unavailable.value.code == "unsupported_capability"
    assert not any(name == "invoke_menu" for name, _args in fake_transport.calls)


def test_exact_safe_menu_invocation_returns_standard_receipt(fake_transport) -> None:
    service = _capability_service(fake_transport, "invoke_menu")
    target, observation = _target_and_capture(service)

    receipt = service.act_menu(
        target,
        ["View", "Zoom In"],
        OWNER,
        approval_mode="allow_all",
    )

    assert isinstance(receipt, ActionReceipt)
    assert receipt.action == "menu"
    assert receipt.driver_effect == "confirmed"
    assert receipt.route == "accessibility"
    assert receipt.delivery_mode == "foreground"
    assert [
        args for name, args in fake_transport.calls if name == "invoke_menu"
    ] == [
        {
            "pid": 4242,
            "window_id": 101,
            "path": ["View", "Zoom In"],
            "session": "capability-session",
        }
    ]
    assert service.current_observation(target) is None
    assert all(name not in {"click", "double_click", "right_click"} for name, _args in fake_transport.calls)
    assert observation.elements


def test_consequential_menu_reproves_exact_target_and_refusal_has_no_pixel_fallback(
    fake_transport,
) -> None:
    approvals: list[dict] = []
    from row_bot.computer_use.client import CuaClient
    from row_bot.computer_use.service import ComputerUseService

    client = CuaClient(
        "fake-cua-driver.exe",
        contract_version="0.19.3",
        capabilities=frozenset({"invoke_menu"}),
        transport_factory=lambda *_args: fake_transport,
    )
    service = ComputerUseService(
        client_factory=lambda: client,
        approval_callback=lambda payload: approvals.append(payload) or True,
    )
    target, _observation = _target_and_capture(service)
    fake_transport.scenario.menu_error_code = "menu_item_disabled"

    with pytest.raises(ComputerUseError, match="unavailable or refused"):
        service.act_menu(target, ["File", "Save"], OWNER, approval_mode="approve")

    assert approvals[-1]["target"] == "File > Save"
    assert [name for name, _args in fake_transport.calls].count("list_windows") == 2
    assert [name for name, _args in fake_transport.calls].count("invoke_menu") == 1
    assert all(name not in {"click", "double_click", "right_click"} for name, _args in fake_transport.calls)
