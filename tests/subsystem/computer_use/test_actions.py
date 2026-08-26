from __future__ import annotations

import json

import pytest

from row_bot.computer_use.service import (
    ActionReceipt,
    ComputerUseError,
    ComputerUseService,
    LeaseOwner,
    Observation,
    StaleObservationError,
)
from row_bot.tools.computer_use_tool import _observation_payload


OWNER = LeaseOwner("actions-thread", "actions-generation", "actions-task")


def _target_and_capture(service):
    service.acquire(OWNER, validate_context=False)
    target = service.list_windows(OWNER, app="Calculator")[0]["target_id"]
    return target, service.capture(target, OWNER)


@pytest.mark.parametrize(
    ("app_name", "window_title", "ordinary_label"),
    [
        ("Contoso Notes.exe", "Project - Contoso Notes", "System"),
        ("Acme Writer.app", "Draft - Acme Writer", "Assistant"),
        ("org.example.Editor", "System Information", "System Settings"),
    ],
)
def test_ordinary_platform_labels_do_not_block_safe_exact_semantic_actions(
    service,
    fake_transport,
    app_name: str,
    window_title: str,
    ordinary_label: str,
) -> None:
    fake_transport.scenario.windows = (
        {
            "window_id": 301,
            "pid": 5301,
            "app_name": app_name,
            "title": window_title,
            "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
            "is_on_screen": True,
        },
    )
    fake_transport.scenario.semantic_elements = (
        {"role": "MenuItem", "label": ordinary_label},
        {"role": "Button", "label": "Open"},
    )
    service.acquire(OWNER, validate_context=False)
    target = service.list_windows(OWNER, app=app_name)[0]["target_id"]

    observation = service.capture(target, OWNER, approval_mode="allow_all")
    result = service.act(
        "click",
        target,
        OWNER,
        element_token=observation.elements[1].token,
        approval_mode="allow_all",
    )

    assert observation.suspicious is False
    assert isinstance(result, ActionReceipt)
    assert [name for name, _args in fake_transport.calls].count("click") == 1


def test_app_scoped_capture_discovers_and_captures_exactly_once_without_vision(
    fake_client,
    fake_transport,
) -> None:
    class _Vision:
        def __init__(self) -> None:
            self.calls = 0

        def analyze(self, _image: bytes, _question: str) -> str:
            self.calls += 1
            return "unused"

    vision = _Vision()
    service = ComputerUseService(
        client_factory=lambda: fake_client,
        approval_callback=lambda _payload: True,
        vision_service=vision,
    )

    observation = service.capture(
        owner=OWNER,
        app="Calculator",
        visual_question="Premature initial question",
    )

    names = [name for name, _args in fake_transport.calls]
    assert names.count("list_windows") == 1
    assert names.count("get_window_state") == 1
    assert observation.target.app_name == "Calculator"
    assert observation.vision_deferred is True
    assert observation.vision_text == ""
    assert vision.calls == 0
    payload = json.loads(_observation_payload(observation))
    assert payload["visual_analysis_deferred"] is True
    assert "no visual question was answered" in payload["fresh_observation"].casefold()


def test_app_scope_approval_precedes_target_pixel_request(
    fake_client,
    fake_transport,
) -> None:
    calls_at_approval: list[list[str]] = []
    service = ComputerUseService(
        client_factory=lambda: fake_client,
        approval_callback=lambda _payload: calls_at_approval.append(
            [name for name, _args in fake_transport.calls]
        )
        or True,
    )

    service.capture(owner=OWNER, app="Calculator")

    assert len(calls_at_approval) == 1
    assert "list_windows" in calls_at_approval[0]
    assert "get_window_state" not in calls_at_approval[0]


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
    assert result.action_completed is False
    assert result.driver_effect == "confirmed"
    assert result.visual_change == "unknown"
    assert result.effect_verified is False
    assert "private typed value" not in repr(result)
    assert service.ephemeral_screenshot()


@pytest.mark.parametrize(
    ("role", "selected"),
    [
        ("Edit", False),
        ("TextField", False),
        ("GridCell", True),
    ],
)
def test_replace_text_uses_one_exact_semantic_set_value_delivery(
    service,
    fake_transport,
    role: str,
    selected: bool,
) -> None:
    replacement = f"private replacement for {role}"
    fake_transport.scenario.semantic_elements = (
        {
            "role": role,
            "label": "Current editable item",
            "enabled": True,
            "selected": selected,
        },
    )
    target, observation = _target_and_capture(service)
    calls_before = len(fake_transport.calls)

    result = service.act(
        "replace_text",
        target,
        OWNER,
        element_token=observation.elements[0].token,
        text=replacement,
    )

    set_value_calls = [
        args
        for name, args in fake_transport.calls[calls_before:]
        if name == "set_value"
    ]
    assert set_value_calls == [
        {
            "pid": 4242,
            "window_id": 101,
            "element_token": observation.elements[0].token,
            "value": f"<redacted:{len(replacement)} chars>",
            "session": "row-bot-test-session",
        }
    ]
    assert fake_transport.document_value == replacement
    assert isinstance(result, Observation)
    assert result.outcome == "provider_echo_unverified"
    assert result.effect_verified is False
    assert result.action_completed is False
    assert all(name != "press_key" for name, _args in fake_transport.calls[calls_before:])


def test_replace_text_rejects_missing_token_and_coordinates_before_mutation(
    service,
    fake_transport,
) -> None:
    target, observation = _target_and_capture(service)
    mutations_before = sum(
        name in {"set_value", "type_text"} for name, _args in fake_transport.calls
    )

    with pytest.raises(ComputerUseError) as missing:
        service.act("replace_text", target, OWNER, text="hidden")
    with pytest.raises(ComputerUseError) as coordinates:
        service.act(
            "replace_text",
            target,
            OWNER,
            element_token=observation.elements[2].token,
            text="hidden",
            x=0,
            y=0,
        )

    assert missing.value.code == "invalid_input"
    assert coordinates.value.code == "invalid_input"
    assert sum(
        name in {"set_value", "type_text"} for name, _args in fake_transport.calls
    ) == mutations_before


@pytest.mark.parametrize(
    "element",
    [
        {"role": "Edit", "label": "Disabled field", "enabled": False},
        {"role": "Button", "label": "Ordinary control", "enabled": True},
    ],
)
def test_replace_text_rejects_disabled_and_unsupported_controls_without_mutation(
    service,
    fake_transport,
    element: dict,
) -> None:
    fake_transport.scenario.semantic_elements = (element,)
    target, observation = _target_and_capture(service)
    calls_before = len(fake_transport.calls)

    with pytest.raises(ComputerUseError) as rejected:
        service.act(
            "replace_text",
            target,
            OWNER,
            element_token=observation.elements[0].token,
            text="hidden",
        )

    assert rejected.value.code == "invalid_input"
    assert all(
        name not in {"set_value", "type_text"}
        for name, _args in fake_transport.calls[calls_before:]
    )


def test_replace_text_rejects_nonprojected_token_without_mutation(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.semantic_elements = tuple(
        {"role": "Edit", "label": f"Field {index:03d}", "enabled": True}
        for index in range(100)
    )
    target, observation = _target_and_capture(service)
    omitted_token = observation.elements[90].token
    calls_before = len(fake_transport.calls)

    with pytest.raises(StaleObservationError):
        service.act(
            "replace_text",
            target,
            OWNER,
            element_token=omitted_token,
            text="hidden",
        )
    assert all(
        name not in {"set_value", "type_text"}
        for name, _args in fake_transport.calls[calls_before:]
    )


def test_replace_text_rejects_stale_token_without_mutation(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.semantic_elements = (
        {"role": "Edit", "label": "Current field", "enabled": True},
    )
    target, observation = _target_and_capture(service)
    calls_before = len(fake_transport.calls)
    service.invalidate_observation("test stale replacement")
    with pytest.raises(StaleObservationError):
        service.act(
            "replace_text",
            target,
            OWNER,
            element_token=observation.elements[0].token,
            text="hidden",
        )
    assert all(
        name not in {"set_value", "type_text"}
        for name, _args in fake_transport.calls[calls_before:]
    )


def test_replace_text_rejects_ambiguous_fresh_exact_match_without_mutation(
    service,
    fake_transport,
) -> None:
    target_element = {
        "role": "Edit",
        "label": "Repeated field",
        "enabled": True,
        "frame": {"x": 20, "y": 30, "w": 260, "h": 52},
    }
    fake_transport.scenario.semantic_snapshots = (
        (target_element,),
        (target_element, dict(target_element)),
    )
    target, observation = _target_and_capture(service)
    calls_before = len(fake_transport.calls)

    with pytest.raises(ComputerUseError) as ambiguous:
        service.act(
            "replace_text",
            target,
            OWNER,
            element_token=observation.elements[0].token,
            text="hidden",
        )

    assert ambiguous.value.code == "ambiguous_target"
    assert all(
        name not in {"set_value", "type_text"}
        for name, _args in fake_transport.calls[calls_before:]
    )


def test_replace_text_unsupported_set_value_has_no_fallback(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.semantic_elements = (
        {"role": "Edit", "label": "Writable field", "enabled": True},
    )
    target, observation = _target_and_capture(service)
    fake_transport.scenario.action_error_code = "unsupported"
    calls_before = len(fake_transport.calls)

    with pytest.raises(ComputerUseError) as unsupported:
        service.act(
            "replace_text",
            target,
            OWNER,
            element_token=observation.elements[0].token,
            text="hidden",
        )

    mutation_calls = [
        name
        for name, _args in fake_transport.calls[calls_before:]
        if name in {"set_value", "type_text", "press_key", "hotkey"}
    ]
    assert unsupported.value.code == "unsupported_capability"
    assert mutation_calls == ["set_value"]


def test_replace_text_driver_refusal_or_unverified_effect_is_never_replayed(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.semantic_elements = (
        {"role": "Edit", "label": "Current value", "enabled": True},
    )
    target, observation = _target_and_capture(service)
    fake_transport.scenario.background_unavailable_tools = frozenset({"set_value"})
    calls_before = len(fake_transport.calls)

    with pytest.raises(ComputerUseError) as refused:
        service.act(
            "replace_text",
            target,
            OWNER,
            element_token=observation.elements[0].token,
            text="first hidden value",
        )

    refused_calls = fake_transport.calls[calls_before:]
    assert refused.value.code == "background_unavailable"
    assert [name for name, _args in refused_calls].count("set_value") == 1
    assert all(name != "type_text" for name, _args in refused_calls)
    assert all(name not in {"bring_to_front", "press_key"} for name, _args in refused_calls)

    fake_transport.scenario.background_unavailable_tools = frozenset()
    fake_transport.scenario.element_type_effect = "unverifiable"
    observation = service.capture(target, OWNER)
    calls_before = len(fake_transport.calls)
    result = service.act(
        "replace_text",
        target,
        OWNER,
        element_token=observation.elements[0].token,
        text="second hidden value",
    )

    unverified_calls = fake_transport.calls[calls_before:]
    assert isinstance(result, Observation)
    assert result.outcome == "provider_echo_unverified"
    assert result.effect_verified is False
    assert result.driver_effect == "unverifiable"
    assert [name for name, _args in unverified_calls].count("set_value") == 1
    assert all(name != "type_text" for name, _args in unverified_calls)
    assert all(name != "press_key" for name, _args in unverified_calls)


def test_replace_text_keeps_secure_handoff_and_consequential_approval(
    fake_client,
    fake_transport,
) -> None:
    approvals: list[dict] = []
    service = ComputerUseService(
        client_factory=lambda: fake_client,
        approval_callback=lambda payload: approvals.append(payload) or True,
    )
    fake_transport.scenario.semantic_elements = (
        {"role": "PasswordField", "label": "Password", "enabled": True},
    )
    target, observation = _target_and_capture(service)
    calls_before = len(fake_transport.calls)

    with pytest.raises(ComputerUseError) as handoff:
        service.act(
            "replace_text",
            target,
            OWNER,
            element_token=observation.elements[0].token,
            text="private credential",
        )

    assert handoff.value.code == "handoff_required"
    assert all(name != "type_text" for name, _args in fake_transport.calls[calls_before:])

    service.stop()
    approvals.clear()
    fake_transport.scenario.semantic_elements = (
        {"role": "Edit", "label": "Send", "enabled": True},
    )
    target, observation = _target_and_capture(service)
    approvals.clear()
    replacement = "private message body"
    result = service.act(
        "replace_text",
        target,
        OWNER,
        element_token=observation.elements[0].token,
        text=replacement,
        approval_mode="approve",
    )

    assert isinstance(result, Observation)
    assert len(approvals) == 1
    assert approvals[0]["action"] == "replace_text"
    assert approvals[0]["data_summary"] == (
        f"Text entry ({len(replacement)} characters; value hidden)"
    )
    assert replacement not in repr(approvals)
    assert [name for name, _args in fake_transport.calls].count("set_value") == 1
    assert [name for name, _args in fake_transport.calls].count("type_text") == 0
    assert [name for name, _args in fake_transport.calls].count("press_key") == 0


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


def test_semantic_after_action_reports_truthful_native_change_and_receipt_fields(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.semantic_snapshots = (
        ({"role": "button", "label": "Play", "enabled": True},),
        ({"role": "button", "label": "Pause", "enabled": True},),
    )
    fake_transport.scenario.effect = "unverifiable"
    fake_transport.scenario.action_route = "accessibility"
    fake_transport.scenario.action_cause = "semantic_target"
    target, observation = _target_and_capture(service)
    before_vision = service.performance_snapshot()["vision_calls"]

    result = service.act(
        "click",
        target,
        OWNER,
        element_token=observation.elements[0].token,
        capture_after=True,
        visual_question="Run the one explicitly requested advisory Vision check",
    )

    assert result.native_change == "changed"
    assert result.visual_change == "unknown"
    assert result.effect_verified is False
    assert result.route == "accessibility"
    assert result.cause == "semantic_target"
    assert result.delivery_mode == "background"
    assert result.driver_effect == "unverifiable"
    assert service.performance_snapshot()["vision_calls"] == before_vision + 1


def test_unchanged_stateful_semantic_action_remains_delivered_but_unverified(
    service,
    fake_transport,
) -> None:
    state = ({"role": "togglebutton", "label": "Playback", "selected": False},)
    fake_transport.scenario.semantic_snapshots = (state, state)
    fake_transport.scenario.effect = "unverifiable"
    target, observation = _target_and_capture(service)

    result = service.act(
        "click",
        target,
        OWNER,
        element_token=observation.elements[0].token,
        capture_after=True,
    )

    assert result.native_change == "unchanged"
    assert result.action_completed is False
    assert result.effect_verified is False


def test_unchanged_momentary_semantic_control_remains_unknown(
    service,
    fake_transport,
) -> None:
    momentary = ({"role": "button", "label": "Next"},)
    fake_transport.scenario.semantic_snapshots = (momentary, momentary)
    fake_transport.scenario.effect = "unverifiable"
    target, observation = _target_and_capture(service)

    result = service.act(
        "click",
        target,
        OWNER,
        element_token=observation.elements[0].token,
        capture_after=True,
    )

    assert result.native_change == "unknown"
    assert service.status_snapshot()["consecutive_visual_no_effects"] == 0


def test_three_conservative_semantic_no_progress_results_stop_blind_attempts(
    service,
    fake_transport,
) -> None:
    state = ({"role": "togglebutton", "label": "Playback", "selected": False},)
    fake_transport.scenario.semantic_snapshots = (state, state, state, state)
    fake_transport.scenario.effect = "unverifiable"
    target, observation = _target_and_capture(service)

    for attempt in range(3):
        if attempt:
            observation = service.current_observation(target)
            assert observation is not None
        if attempt < 2:
            result = service.act(
                "click",
                target,
                OWNER,
                element_token=observation.elements[0].token,
                capture_after=True,
            )
            assert result.native_change == "unchanged"
        else:
            with pytest.raises(ComputerUseError) as stopped:
                service.act(
                    "click",
                    target,
                    OWNER,
                    element_token=observation.elements[0].token,
                    capture_after=True,
                )
            assert stopped.value.code == "no_progress"

    assert [name for name, _args in fake_transport.calls].count("click") == 3
    assert service.status_snapshot()["state"] == "needs_attention"


def test_changed_native_evidence_resets_same_family_no_progress_streak(
    service,
    fake_transport,
) -> None:
    off = ({"role": "togglebutton", "label": "Playback", "selected": False},)
    on = ({"role": "togglebutton", "label": "Playback", "selected": True},)
    fake_transport.scenario.semantic_snapshots = (off, off, on, on)
    fake_transport.scenario.effect = "unverifiable"
    target, observation = _target_and_capture(service)

    changes: list[str] = []
    for _attempt in range(3):
        result = service.act(
            "click",
            target,
            OWNER,
            element_token=observation.elements[0].token,
            capture_after=True,
        )
        changes.append(result.native_change)
        observation = result

    assert changes == ["unchanged", "changed", "unchanged"]
    assert service.status_snapshot()["consecutive_visual_no_effects"] == 1
    assert service.status_snapshot()["state"] == "observing"


def test_semantic_terminal_action_succeeds_when_exact_target_disappears_after_dispatch(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.semantic_elements = (
        {"role": "button", "label": "Don't save"},
    )
    fake_transport.scenario.close_target_after_labels = frozenset({"Don't save"})
    target, observation = _target_and_capture(service)
    calls_before = len(fake_transport.calls)

    result = service.act(
        "click",
        target,
        OWNER,
        element_token=observation.elements[0].token,
        expected_effect="Close without saving",
        approval_mode="allow_all",
        capture_after=True,
    )

    assert isinstance(result, ActionReceipt)
    assert result.action_completed is True
    assert result.effect_verified is True
    assert result.cause == "target_disappeared"
    assert service.current_observation(target) is None
    assert service.status_snapshot()["consecutive_failures"] == 0
    assert [name for name, _args in fake_transport.calls[calls_before:]] == [
        "click",
        "get_window_state",
        "list_windows",
    ]
    with pytest.raises(ComputerUseError) as expired:
        service.capture(target, OWNER)
    assert expired.value.code == "target_gone"


def test_capture_failure_is_not_hidden_for_non_terminal_semantic_action(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.semantic_elements = (
        {"role": "button", "label": "Apply"},
    )
    fake_transport.scenario.close_target_after_labels = frozenset({"Apply"})
    target, observation = _target_and_capture(service)

    with pytest.raises(ComputerUseError, match="could not be observed safely"):
        service.act(
            "click",
            target,
            OWNER,
            element_token=observation.elements[0].token,
            expected_effect="Apply the selected setting",
            approval_mode="allow_all",
            capture_after=True,
        )

    assert service.status_snapshot()["consecutive_failures"] == 1


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
    assert result.action_completed is False
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


def test_block_mode_denies_exact_replacement_without_driver_mutation(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.semantic_elements = (
        {"role": "Edit", "label": "Current value", "enabled": True},
    )
    target, observation = _target_and_capture(service)
    calls_before = len(fake_transport.calls)

    with pytest.raises(ComputerUseError, match="Block approval mode"):
        service.act(
            "replace_text",
            target,
            OWNER,
            element_token=observation.elements[0].token,
            text="hidden",
            approval_mode="block",
        )

    assert all(name != "type_text" for name, _args in fake_transport.calls[calls_before:])


def test_launch_requires_app_scope_and_captures_after_launch(fake_client, fake_transport) -> None:
    approvals = []
    from row_bot.computer_use.service import ComputerUseService

    service = ComputerUseService(client_factory=lambda: fake_client, approval_callback=lambda payload: approvals.append(payload) or True)
    service.acquire(OWNER, validate_context=False)
    windows = service.launch_app("Calculator", OWNER)
    assert windows
    names = [name for name, _args in fake_transport.calls]
    assert names[names.index("launch_app") + 1] == "list_windows"
    assert names[names.index("launch_app") + 1 : names.index("launch_app") + 5] == [
        "list_windows",
        "list_windows",
        "get_window_state",
        "list_windows",
    ]
    capture_args = next(args for tool, args in fake_transport.calls if tool == "get_window_state")
    assert capture_args["pid"] == 4242
    assert names[-1] == "list_windows"
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


def test_routine_key_sequence_refuses_to_claim_unverified_sparse_result(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.calculator_semantics = True
    fake_transport.scenario.calculator_sparse_after_action = True
    target, _observation = _target_and_capture(service)

    with pytest.raises(ComputerUseError) as raised:
        service.act_key_sequence(target, "7,*,8,=", OWNER)

    assert raised.value.code == "driver_failed"
    assert "could not be verified" in str(raised.value)
    assert [name for name, _args in fake_transport.calls].count("click") == 4


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
        contract_version="0.20.0",
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
        contract_version="0.20.0",
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
