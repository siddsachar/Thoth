from __future__ import annotations

import base64
import io
import json

import pytest
from PIL import Image

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


def test_app_scoped_capture_honors_explicit_visual_question_once(
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
    assert observation.vision_deferred is False
    assert observation.vision_text.endswith("unused")
    assert vision.calls == 1
    payload = json.loads(_observation_payload(observation))
    assert "visual_analysis_deferred" not in payload
    assert "vision evidence" in payload["fresh_observation"].casefold()


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
        ("type", {"text": "private typed value"}, "type_text"),
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
    assert result.verified_scope == ""
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
    fake_transport.scenario.delivery_profile = "native_exact_set_value"
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
    assert isinstance(result, ActionReceipt)
    assert result.verified_scope == "exact_value"
    assert result.effect_verified is True
    assert result.action_completed is True
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
        {"role": "Pane", "label": "Structural control", "enabled": True},
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
        {
            "role": "Edit",
            "label": "Web Composer",
            "enabled": True,
            "in_web_content": True,
        },
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
    fake_transport.scenario.delivery_profile = "web_targeted_unverifiable"
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
    assert isinstance(result, ActionReceipt)
    assert result.action_dispatched is True
    assert result.verified_scope == ""
    assert result.effect_verified is False
    assert result.driver_effect == "unverifiable"
    assert [name for name, _args in unverified_calls].count("set_value") == 1
    assert all(name != "type_text" for name, _args in unverified_calls)
    assert all(name != "press_key" for name, _args in unverified_calls)


def test_retina_scale_coordinates_remain_screenshot_local_without_double_scaling(
    service,
    fake_transport,
) -> None:
    image = Image.new("RGB", (200, 100), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    fake_transport.scenario.capture_dimensions = (200, 100)
    fake_transport.scenario.capture_images = (
        base64.b64encode(buffer.getvalue()).decode("ascii"),
    )
    fake_transport.scenario.scale_factor = 2.0
    target, observation = _target_and_capture(service)

    service.act(
        "click",
        target,
        OWNER,
        x=120,
        y=60,
        approval_mode="allow_all",
    )

    click = [args for name, args in fake_transport.calls if name == "click"][-1]
    assert observation.scale_factor == 2.0
    assert (click["x"], click["y"]) == (120, 60)


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

    assert isinstance(result, ActionReceipt)
    assert result.verified_scope == "exact_value"
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


def test_exact_web_content_replace_text_readback_verifies_only_the_value(
    service,
    fake_transport,
) -> None:
    replacement = "private exact search value"
    fake_transport.scenario.delivery_profile = "web_targeted_unverifiable"
    fake_transport.scenario.set_value_updates_document = True
    fake_transport.scenario.semantic_elements = (
        {
            "role": "TextField",
            "label": "Search",
            "value": "",
            "enabled": True,
            "in_web_content": True,
        },
    )
    target, observation = _target_and_capture(service)

    result = service.act(
        "replace_text",
        target,
        OWNER,
        element_token=observation.elements[0].token,
        text=replacement,
        capture_after=True,
    )

    assert result.effect_verified is True
    assert result.verified_scope == "exact_value"
    assert result.semantic_postcondition == "matched"
    rendered = result.model_text().casefold()
    assert "submission" not in rendered
    assert "navigation" not in rendered
    assert "search completion" not in rendered
    assert "playback" not in rendered
    assert replacement not in rendered
















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

    assert service.status_snapshot()["state"] == "observing"


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






def test_focus_is_one_confirmed_call_without_an_implicit_capture(service, fake_transport) -> None:
    target, _ = _target_and_capture(service)
    result = service.act("focus", target, OWNER, expected_effect="Bring Calculator forward")
    names = [name for name, _args in fake_transport.calls]
    assert names.count("bring_to_front") == 1
    assert names[-1] == "bring_to_front"
    assert isinstance(result, ActionReceipt)
    assert result.action_completed is True
    assert "foreground_prepared" not in service.status_snapshot()


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
    with pytest.raises(ComputerUseError) as stale:
        service.act("click", target, OWNER, element_token=observation.elements[0].token)
    assert stale.value.code == "stale_observation"
    assert isinstance(stale.value.observation, Observation)
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
    assert service.current_observation(target) is observation
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
    assert [name for name, _args in fake_transport.calls].count("list_windows") == 1
    assert [name for name, _args in fake_transport.calls].count("invoke_menu") == 1
    assert all(name not in {"click", "double_click", "right_click"} for name, _args in fake_transport.calls)
