from __future__ import annotations

import json
from types import SimpleNamespace
import concurrent.futures
from pathlib import Path
import logging

import pytest

from row_bot.automation.contracts import ActionReceipt, AutomationSurface
from row_bot.computer_use.client import CuaResponse
from row_bot.computer_use.service import (
    ComputerUseError,
    LeaseBusyError,
    LeaseOwner,
    StaleObservationError,
    _classify_driver_result,
)
from row_bot.mcp_client.results import RawCallContent, RawCallResult
from row_bot.tools.computer_use_tool import (
    ComputerUseInput,
    _action_payload,
    _computer_error_payload,
    _observation_payload,
)
from row_bot.ui.tool_trace import display_tool_content
from row_bot.ui.live_control import computer_live_control_view


OWNER = LeaseOwner("verdict-thread", "verdict-generation", "verdict-task")


def _target_and_capture(service):
    service.acquire(OWNER, validate_context=False)
    target_id = service.list_windows(OWNER, app="Calculator")[0]["target_id"]
    return target_id, service.capture(target_id, OWNER)


@pytest.mark.parametrize("action", ["click", "double_click", "right_click", "key", "scroll"])
def test_every_mutation_family_projects_only_safe_driver_result_fields(
    fake_client,
    fake_transport,
    monkeypatch,
    action: str,
) -> None:
    private_driver_prose = "private driver prose with a secret target label"

    def unsafe_result(_name, _arguments=None):
        return RawCallResult(
            (RawCallContent(kind="text", text=private_driver_prose),),
            {
                "effect": "partial",
                "verified": False,
                "delivery_mode": "background",
                "route": "uia",
                "degraded": True,
                "escalation": {
                    "recommended": "foreground",
                    "reason": private_driver_prose,
                    "target": "private window title",
                },
                "typed_value": "private value",
                "arbitrary": {"nested": private_driver_prose},
            },
            False,
        )

    monkeypatch.setattr(fake_transport, "call_raw", unsafe_result)

    response = fake_client.call_action(action, {"pid": 1, "window_id": 2})

    assert response.text == ""
    assert response.structured == {
        "verified": False,
        "effect": "partial",
        "delivery_mode": "background",
        "route": "uia",
        "degraded": True,
        "escalation": {"recommended": "foreground"},
    }
    assert private_driver_prose not in repr(response)
    assert "private value" not in repr(response)


def test_text_mutation_keeps_only_allowlisted_degradation_and_escalation(
    fake_client,
    fake_transport,
    monkeypatch,
) -> None:
    private_value = "private typed value"

    def unsafe_result(_name, _arguments=None):
        return RawCallResult(
            (RawCallContent(kind="text", text=f"failed around {private_value}"),),
            {
                "effect": "suspected_noop",
                "verified": False,
                "delivery": "background",
                "degraded": True,
                "escalation": {
                    "recommended": "px",
                    "reason": f"driver saw {private_value}",
                    "bounds": [1, 2, 3, 4],
                },
                "value": private_value,
            },
            False,
        )

    monkeypatch.setattr(fake_transport, "call_raw", unsafe_result)

    response = fake_client.call_action(
        "type",
        {"pid": 1, "window_id": 2, "text": private_value},
    )

    assert response.structured == {
        "verified": False,
        "effect": "suspected_noop",
        "delivery": "background",
        "degraded": True,
        "escalation": {"recommended": "px"},
    }
    assert private_value not in response.text
    assert private_value not in repr(response.structured)


@pytest.mark.parametrize("recommended", ["foreground", "px", "page"])
def test_allowlisted_escalation_recommendations_remain_observable(
    fake_client,
    fake_transport,
    monkeypatch,
    recommended: str,
) -> None:
    def result(_name, _arguments=None):
        return RawCallResult(
            (RawCallContent(kind="text", text="unsafe prose"),),
            {
                "effect": "unverifiable",
                "escalation": {
                    "recommended": recommended,
                    "reason": "unsafe prose",
                },
            },
            False,
        )

    monkeypatch.setattr(fake_transport, "call_raw", result)

    response = fake_client.call_action("click", {"pid": 1, "window_id": 2})

    assert response.structured["escalation"] == {"recommended": recommended}


def test_unknown_escalation_recommendation_and_nested_keys_are_dropped(
    fake_client,
    fake_transport,
    monkeypatch,
) -> None:
    def result(_name, _arguments=None):
        return RawCallResult(
            (RawCallContent(kind="text", text="unsafe prose"),),
            {
                "effect": "unverifiable",
                "escalation": {
                    "recommended": "shell",
                    "reason": "unsafe prose",
                },
            },
            False,
        )

    monkeypatch.setattr(fake_transport, "call_raw", result)

    response = fake_client.call_action("click", {"pid": 1, "window_id": 2})

    assert "escalation" not in response.structured
    assert "unsafe prose" not in repr(response)


@pytest.mark.parametrize(
    ("response", "kwargs", "verdict", "next_step"),
    [
        (
            CuaResponse(structured={"effect": "confirmed"}),
            {},
            "done",
            "continue",
        ),
        (
            CuaResponse(structured={"effect": "unverifiable", "verified": True}),
            {},
            "done",
            "continue",
        ),
        (
            CuaResponse(structured={"effect": "partial"}),
            {},
            "verify_fresh_state",
            "capture_same_target",
        ),
        (
            CuaResponse(structured={"effect": "suspected_noop"}),
            {},
            "verify_fresh_state",
            "capture_same_target",
        ),
        (
            CuaResponse(
                structured={"error": {"code": "background_unavailable"}},
                is_error=True,
                error_code="background_unavailable",
            ),
            {"supported_foreground": True},
            "escalate",
            "retry_foreground_once",
        ),
        (
            CuaResponse(
                structured={"error": {"code": "background_unavailable"}},
                is_error=True,
                error_code="background_unavailable",
            ),
            {"supported_foreground": True, "foreground_attempted": True},
            "take_over",
            "take_over",
        ),
        (
            CuaResponse(
                structured={
                    "effect": "unverifiable",
                    "escalation": {"recommended": "page"},
                }
            ),
            {},
            "take_over",
            "unsupported_page_take_over",
        ),
        (
            CuaResponse(is_error=True, error_code="timeout"),
            {},
            "take_over",
            "recapture_before_reissue",
        ),
    ],
)
def test_driver_result_classifier_has_one_bounded_precedence(
    response: CuaResponse,
    kwargs: dict,
    verdict: str,
    next_step: str,
) -> None:
    classified = _classify_driver_result(
        response,
        requested_delivery="auto",
        actual_delivery="background",
        **kwargs,
    )

    assert classified.verdict == verdict
    assert classified.next_step == next_step


def test_receipt_and_observation_payloads_expose_only_safe_classification_fields() -> None:
    receipt = ActionReceipt(
        surface=AutomationSurface.COMPUTER,
        target_id="opaque-target",
        action_family="click",
        revision=7,
        dispatched=True,
        completed=False,
        backend_effect="suspected_noop",
        delivery="background",
        route="accessibility",
        verified_outcome=False,
        requested_delivery="auto",
        degraded=True,
        escalation_recommendation="px",
        verdict="verify_fresh_state",
        next_step="capture_same_target",
    )

    receipt_payload = json.loads(_action_payload(receipt))
    assert receipt_payload["driver_verdict"] == "suspected_noop"
    assert receipt_payload["requested_delivery"] == "auto"
    assert receipt_payload["delivery_mode"] == "background"
    assert receipt_payload["degraded"] is True
    assert receipt_payload["escalation_recommendation"] == "px"
    assert receipt_payload["verdict"] == "verify_fresh_state"
    assert receipt_payload["next_step"] == "capture_same_target"

    observation = SimpleNamespace(
        model_text=lambda: "Fresh exact target; no private content.",
        vision_text="",
        vision_deferred=False,
        action_family="click",
        action_effect="delivered_unverified",
        action_dispatched=True,
        action_completed=False,
        driver_effect="suspected_noop",
        visual_change="unknown",
        native_change="unchanged",
        effect_verified=False,
        outcome="suspected_noop",
        verified_scope="",
        dispatch_state="dispatched",
        driver_verdict="suspected_noop",
        semantic_postcondition="unavailable",
        visual_observation="unavailable",
        delivery_mode="background",
        route="accessibility",
        cause="",
        requested_delivery="auto",
        degraded=True,
        escalation_recommendation="px",
        verdict="escalate",
        next_step="pixel_click_once",
    )
    observation_payload = json.loads(_observation_payload(observation))
    assert observation_payload["requested_delivery"] == "auto"
    assert observation_payload["degraded"] is True
    assert observation_payload["escalation_recommendation"] == "px"
    assert observation_payload["verdict"] == "escalate"
    assert observation_payload["next_step"] == "pixel_click_once"

    visible_trace = display_tool_content(json.dumps(receipt_payload))
    assert "private" not in visible_trace.casefold()
    assert "driver prose" not in visible_trace.casefold()
    assert "verify fresh state" in visible_trace.casefold()


def test_error_trace_summary_projects_safe_classification_fields() -> None:
    classification = _classify_driver_result(
        CuaResponse(
            structured={"error": {"code": "background_unavailable"}},
            is_error=True,
            error_code="background_unavailable",
        ),
        requested_delivery="auto",
        actual_delivery="foreground",
        supported_foreground=True,
        foreground_attempted=True,
    )
    error = ComputerUseError(
        "private driver prose must stay hidden",
        code="background_unavailable",
        terminal=True,
        classification=classification,
    )

    payload = json.loads(_computer_error_payload("click", error))
    visible_trace = display_tool_content(payload).casefold()

    assert "requested auto" in visible_trace
    assert "delivered foreground" in visible_trace
    assert "driver unverifiable" in visible_trace
    assert "outcome unverified" in visible_trace
    assert "escalation none" in visible_trace
    assert "verdict take over" in visible_trace
    assert "next take over" in visible_trace
    assert "private driver prose" not in visible_trace


@pytest.mark.parametrize(
    ("action", "coordinate"),
    [
        ("click", False),
        ("click", True),
        ("double_click", False),
        ("double_click", True),
        ("right_click", False),
        ("right_click", True),
    ],
)
def test_click_family_background_refusal_has_one_exact_foreground_rung(
    service,
    fake_transport,
    action: str,
    coordinate: bool,
) -> None:
    target_id, observation = _target_and_capture(service)
    fake_transport.scenario.background_unavailable_tools = frozenset({action})
    fake_transport.scenario.foreground_effect = "confirmed"
    calls_before = len(fake_transport.calls)

    kwargs = {"x": 0, "y": 0} if coordinate else {
        "element_token": observation.elements[0].token
    }
    receipt = service.act(
        action,
        target_id,
        OWNER,
        approval_mode="allow_all",
        **kwargs,
    )

    calls = fake_transport.calls[calls_before:]
    assert [name for name, _args in calls] == [action, action]
    assert "delivery_mode" not in calls[0][1]
    assert calls[1][1]["delivery_mode"] == "foreground"
    assert [(call[1]["pid"], call[1]["window_id"]) for call in calls] == [
        (4242, 101),
        (4242, 101),
    ]
    assert receipt.requested_delivery == "auto"
    assert receipt.delivery_mode == "foreground"
    assert receipt.verdict == "done"
    assert all(name != "bring_to_front" for name, _args in calls)


def test_cancellation_between_click_rungs_prevents_foreground_dispatch(
    service,
    fake_transport,
    monkeypatch,
) -> None:
    target_id, observation = _target_and_capture(service)
    fake_transport.scenario.background_unavailable_tools = frozenset({"click"})
    calls_before = len(fake_transport.calls)
    require_owner = service._require_existing_owner

    def cancel_before_foreground(owner=None):
        service._cancel.set()
        return require_owner(owner)

    monkeypatch.setattr(service, "_require_existing_owner", cancel_before_foreground)

    with pytest.raises(concurrent.futures.CancelledError):
        service.act(
            "click",
            target_id,
            OWNER,
            element_token=observation.elements[0].token,
            approval_mode="allow_all",
        )

    assert [name for name, _args in fake_transport.calls[calls_before:]] == ["click"]


def test_owner_loss_between_click_rungs_prevents_foreground_dispatch(
    service,
    fake_transport,
    monkeypatch,
) -> None:
    target_id, observation = _target_and_capture(service)
    fake_transport.scenario.background_unavailable_tools = frozenset({"click"})
    calls_before = len(fake_transport.calls)
    require_owner = service._require_existing_owner
    replacement = LeaseOwner("other-thread", "other-generation", "other-task")

    def replace_owner_before_foreground(owner=None):
        with service._lock:
            service._owner = replacement
        return require_owner(owner)

    monkeypatch.setattr(service, "_require_existing_owner", replace_owner_before_foreground)

    with pytest.raises(LeaseBusyError):
        service.act(
            "click",
            target_id,
            OWNER,
            element_token=observation.elements[0].token,
            approval_mode="allow_all",
        )

    assert [name for name, _args in fake_transport.calls[calls_before:]] == ["click"]


def test_target_invalidation_between_click_rungs_prevents_foreground_dispatch(
    service,
    fake_transport,
    monkeypatch,
) -> None:
    target_id, observation = _target_and_capture(service)
    fake_transport.scenario.background_unavailable_tools = frozenset({"click"})
    calls_before = len(fake_transport.calls)
    require_owner = service._require_existing_owner

    def invalidate_target_before_foreground(owner=None):
        current = require_owner(owner)
        with service._lock:
            service._targets.pop(target_id, None)
        return current

    monkeypatch.setattr(
        service,
        "_require_existing_owner",
        invalidate_target_before_foreground,
    )

    with pytest.raises(ComputerUseError) as invalidated:
        service.act(
            "click",
            target_id,
            OWNER,
            element_token=observation.elements[0].token,
            approval_mode="allow_all",
        )

    assert invalidated.value.code == "target_gone"
    assert [name for name, _args in fake_transport.calls[calls_before:]] == ["click"]


def test_policy_is_rechecked_between_click_rungs_before_foreground_dispatch(
    service,
    fake_transport,
    monkeypatch,
) -> None:
    target_id, observation = _target_and_capture(service)
    fake_transport.scenario.background_unavailable_tools = frozenset({"click"})
    calls_before = len(fake_transport.calls)
    authorize = service._authorize_action
    authorizations = 0

    def refuse_second_authorization(*args, **kwargs):
        nonlocal authorizations
        authorizations += 1
        if authorizations == 2:
            raise ComputerUseError("Policy changed.", code="hard_blocked")
        return authorize(*args, **kwargs)

    monkeypatch.setattr(service, "_authorize_action", refuse_second_authorization)

    with pytest.raises(ComputerUseError) as refused:
        service.act(
            "click",
            target_id,
            OWNER,
            element_token=observation.elements[0].token,
            approval_mode="allow_all",
        )

    assert refused.value.code == "hard_blocked"
    assert authorizations == 2
    assert [name for name, _args in fake_transport.calls[calls_before:]] == ["click"]


@pytest.mark.parametrize("action", ["click", "double_click", "right_click"])
def test_second_click_family_foreground_refusal_is_terminal_takeover(
    service,
    fake_transport,
    action: str,
) -> None:
    target_id, observation = _target_and_capture(service)
    fake_transport.scenario.background_unavailable_tools = frozenset({action})
    fake_transport.scenario.foreground_error_code = "foreground_required"
    calls_before = len(fake_transport.calls)

    with pytest.raises(ComputerUseError) as refused:
        service.act(
            action,
            target_id,
            OWNER,
            element_token=observation.elements[0].token,
            approval_mode="allow_all",
        )

    calls = fake_transport.calls[calls_before:]
    assert [name for name, _args in calls] == [action, action]
    assert refused.value.terminal is True
    payload = json.loads(_computer_error_payload(action, refused.value))
    assert payload["verdict"] == "take_over"
    assert payload["next_step"] == "take_over"
    assert "take over" in payload["remediation"].casefold()


@pytest.mark.parametrize(
    ("effect", "error_code"),
    [
        ("suspected_noop", ""),
        ("unverifiable", ""),
        ("confirmed", "timeout"),
    ],
)
def test_post_dispatch_or_uncertain_click_result_is_never_replayed_in_call(
    service,
    fake_transport,
    effect: str,
    error_code: str,
) -> None:
    target_id, observation = _target_and_capture(service)
    fake_transport.scenario.effect = effect
    fake_transport.scenario.action_error_code = error_code
    calls_before = len(fake_transport.calls)

    try:
        service.act(
            "click",
            target_id,
            OWNER,
            element_token=observation.elements[0].token,
            approval_mode="allow_all",
        )
    except ComputerUseError:
        pass

    assert [name for name, _args in fake_transport.calls[calls_before:]] == ["click"]


def test_disconnect_during_click_is_not_replayed(service, fake_transport) -> None:
    target_id, observation = _target_and_capture(service)
    fake_transport.scenario.disconnect = True
    calls_before = len(fake_transport.calls)

    with pytest.raises(ComputerUseError, match="disconnected"):
        service.act(
            "click",
            target_id,
            OWNER,
            element_token=observation.elements[0].token,
            approval_mode="allow_all",
        )

    assert [name for name, _args in fake_transport.calls[calls_before:]] == ["click"]


def test_explicit_foreground_is_first_and_only_exact_target_dispatch(
    service,
    fake_transport,
) -> None:
    target_id, observation = _target_and_capture(service)
    fake_transport.scenario.apps = (
        {"name": "Calculator", "running": True, "active": False},
        {"name": "Notepad", "running": True, "active": True},
    )
    fake_transport.scenario.foreground_effect = "confirmed"
    calls_before = len(fake_transport.calls)

    receipt = service.act(
        "click",
        target_id,
        OWNER,
        element_token=observation.elements[0].token,
        approval_mode="allow_all",
        delivery_mode="foreground",
    )

    calls = fake_transport.calls[calls_before:]
    assert calls == [
        (
            "click",
            {
                "pid": 4242,
                "window_id": 101,
                "element_token": observation.elements[0].token,
                "delivery_mode": "foreground",
                "session": "row-bot-test-session",
            },
        )
    ]
    assert receipt.requested_delivery == "foreground"
    assert receipt.delivery_mode == "foreground"
    assert all(name != "bring_to_front" for name, _args in calls)


def test_explicit_foreground_refusal_has_no_second_or_background_attempt(
    service,
    fake_transport,
) -> None:
    target_id, observation = _target_and_capture(service)
    fake_transport.scenario.foreground_error_code = "foreground_required"
    calls_before = len(fake_transport.calls)

    with pytest.raises(ComputerUseError) as refused:
        service.act(
            "click",
            target_id,
            OWNER,
            element_token=observation.elements[0].token,
            approval_mode="allow_all",
            delivery_mode="foreground",
        )

    calls = fake_transport.calls[calls_before:]
    assert len(calls) == 1
    assert calls[0][1]["delivery_mode"] == "foreground"
    assert refused.value.classification.verdict == "take_over"


def test_explicit_foreground_schema_is_narrow_and_action_scoped() -> None:
    schema = ComputerUseInput.model_json_schema()["properties"]["delivery_mode"]

    assert schema["type"] == "string"
    assert schema["default"] == "auto"
    assert "auto" in schema["description"]
    assert "foreground" in schema["description"]


def test_coordinate_click_rejects_semantic_refresh_with_stale_pixels(
    service,
    fake_transport,
) -> None:
    target_id, _observation = _target_and_capture(service)
    service.refresh_semantics(target_id, OWNER)
    calls_before = len(fake_transport.calls)

    with pytest.raises(StaleObservationError, match="screenshot"):
        service.act(
            "click",
            target_id,
            OWNER,
            x=0,
            y=0,
            approval_mode="allow_all",
        )

    assert all(name != "click" for name, _args in fake_transport.calls[calls_before:])


def test_suspected_noop_px_alternative_requires_fresh_exact_capture_and_is_bounded(
    service,
    fake_transport,
) -> None:
    unchanged = ({"role": "Button", "label": "Reversible action"},)
    fake_transport.scenario.semantic_snapshots = (unchanged, unchanged)
    fake_transport.scenario.effect = "suspected_noop"
    fake_transport.scenario.escalation_recommendation = "px"
    target_id, observation = _target_and_capture(service)
    calls_before = len(fake_transport.calls)

    result = service.act(
        "click",
        target_id,
        OWNER,
        element_token=observation.elements[0].token,
        approval_mode="allow_all",
        capture_after=True,
    )

    assert [name for name, _args in fake_transport.calls[calls_before:]] == [
        "click",
        "get_window_state",
    ]
    assert result.native_change == "unchanged"
    assert result.escalation_recommendation == "px"
    assert result.verdict == "escalate"
    assert result.next_step == "pixel_click_once"
    assert result.screenshot


@pytest.mark.parametrize("action", ["list_apps", "list_windows"])
def test_read_only_discovery_recovers_once_from_explicit_session_ended(
    service,
    fake_client,
    fake_transport,
    action: str,
) -> None:
    service.acquire(OWNER, validate_context=False)
    fake_transport.scenario.ended_session_tools = frozenset({action})
    starts_before = fake_transport.session_start_count
    calls_before = len(fake_transport.calls)
    generation_before = fake_client.connection_generation

    if action == "list_apps":
        result = service.list_apps(OWNER)
    else:
        result = service.list_windows(OWNER, app="Calculator")

    calls = fake_transport.calls[calls_before:]
    assert [name for name, _args in calls] == [
        action,
        "start_session",
        action,
    ]
    assert result
    assert fake_transport.session_start_count == starts_before + 1
    assert fake_client.connection_generation == generation_before + 1
    assert service.status_snapshot()["active"] is True


def test_capture_recovers_once_and_publishes_only_the_new_connection_observation(
    service,
    fake_client,
    fake_transport,
) -> None:
    target_id, first = _target_and_capture(service)
    fake_transport.scenario.ended_session_tools = frozenset({"get_window_state"})
    calls_before = len(fake_transport.calls)
    generation_before = fake_client.connection_generation

    recovered = service.capture(target_id, OWNER)

    assert [name for name, _args in fake_transport.calls[calls_before:]] == [
        "get_window_state",
        "start_session",
        "get_window_state",
    ]
    assert fake_client.connection_generation == generation_before + 1
    assert recovered.connection_generation == fake_client.connection_generation
    assert recovered.generation > first.generation
    assert recovered.elements[0].token != first.elements[0].token
    assert service.current_observation(target_id) is recovered


def test_read_only_session_recovery_stops_after_one_restart(
    service,
    fake_transport,
) -> None:
    service.acquire(OWNER, validate_context=False)
    fake_transport.scenario.persistent_ended_session_tools = frozenset({"list_apps"})
    starts_before = fake_transport.session_start_count
    calls_before = len(fake_transport.calls)

    with pytest.raises(ComputerUseError):
        service.list_apps(OWNER)

    assert [name for name, _args in fake_transport.calls[calls_before:]] == [
        "list_apps",
        "start_session",
        "list_apps",
    ]
    assert fake_transport.session_start_count == starts_before + 1


def test_cancellation_after_read_only_session_ended_prevents_restart(
    service,
    fake_transport,
    monkeypatch,
) -> None:
    service.acquire(OWNER, validate_context=False)
    fake_transport.scenario.ended_session_tools = frozenset({"list_apps"})
    original = fake_transport.call_raw
    calls_before = len(fake_transport.calls)
    starts_before = fake_transport.session_start_count

    def cancel_after_refusal(name, arguments=None):
        response = original(name, arguments)
        if name == "list_apps" and response.is_error:
            service._cancel.set()
        return response

    monkeypatch.setattr(fake_transport, "call_raw", cancel_after_refusal)

    with pytest.raises(concurrent.futures.CancelledError):
        service.list_apps(OWNER)

    assert [name for name, _args in fake_transport.calls[calls_before:]] == ["list_apps"]
    assert fake_transport.session_start_count == starts_before


def test_owner_loss_after_read_only_session_ended_prevents_restart(
    service,
    fake_transport,
    monkeypatch,
) -> None:
    service.acquire(OWNER, validate_context=False)
    fake_transport.scenario.ended_session_tools = frozenset({"list_apps"})
    original = fake_transport.call_raw
    calls_before = len(fake_transport.calls)
    starts_before = fake_transport.session_start_count

    def replace_owner_after_refusal(name, arguments=None):
        response = original(name, arguments)
        if name == "list_apps" and response.is_error:
            with service._lock:
                service._owner = LeaseOwner("other", "other", "other")
        return response

    monkeypatch.setattr(fake_transport, "call_raw", replace_owner_after_refusal)

    with pytest.raises(LeaseBusyError):
        service.list_apps(OWNER)

    assert [name for name, _args in fake_transport.calls[calls_before:]] == ["list_apps"]
    assert fake_transport.session_start_count == starts_before


def test_mutation_session_ended_is_never_replayed_and_requires_new_tokens(
    service,
    fake_client,
    fake_transport,
) -> None:
    target_id, observation = _target_and_capture(service)
    old_token = observation.elements[0].token
    fake_transport.scenario.ended_session_tools = frozenset({"click"})
    calls_before = len(fake_transport.calls)
    starts_before = fake_transport.session_start_count

    with pytest.raises(ComputerUseError) as ended:
        service.act(
            "click",
            target_id,
            OWNER,
            element_token=old_token,
            approval_mode="allow_all",
        )

    assert ended.value.code == "stale_observation"
    assert ended.value.retryable is True
    assert ended.value.classification.verdict == "verify_fresh_state"
    assert ended.value.classification.next_step == "recapture_before_reissue"
    assert [name for name, _args in fake_transport.calls[calls_before:]] == ["click"]
    assert fake_transport.session_start_count == starts_before
    assert service.current_observation(target_id) is None

    refreshed = service.capture(target_id, OWNER)
    assert fake_transport.session_start_count == starts_before + 1
    assert refreshed.connection_generation == fake_client.connection_generation
    assert refreshed.elements[0].token != old_token
    clicks_before = [name for name, _args in fake_transport.calls].count("click")
    with pytest.raises(StaleObservationError):
        service.act(
            "click",
            target_id,
            OWNER,
            element_token=old_token,
            approval_mode="allow_all",
        )
    assert [name for name, _args in fake_transport.calls].count("click") == clicks_before


def test_timeout_and_disconnect_do_not_use_ended_session_recovery(
    service,
    fake_transport,
) -> None:
    service.acquire(OWNER, validate_context=False)
    fake_transport.scenario.list_apps_error_code = "timeout"
    starts_before = fake_transport.session_start_count
    calls_before = len(fake_transport.calls)

    with pytest.raises(ComputerUseError) as timed_out:
        service.list_apps(OWNER)

    assert timed_out.value.code == "transient_driver_failure"
    assert [name for name, _args in fake_transport.calls[calls_before:]] == ["list_apps"]
    assert fake_transport.session_start_count == starts_before

    fake_transport.scenario.list_apps_error_code = ""
    fake_transport.scenario.disconnect = True
    calls_before = len(fake_transport.calls)
    with pytest.raises(ComputerUseError, match="disconnected"):
        service.list_apps(OWNER)
    assert [name for name, _args in fake_transport.calls[calls_before:]] == ["list_apps"]
    assert fake_transport.session_start_count == starts_before


def test_live_control_status_exposes_safe_route_verdict_and_next_step(
    service,
    fake_transport,
) -> None:
    unchanged = ({"role": "Button", "label": "Synthetic reversible action"},)
    fake_transport.scenario.semantic_snapshots = (unchanged, unchanged)
    fake_transport.scenario.effect = "suspected_noop"
    fake_transport.scenario.action_degraded = True
    fake_transport.scenario.escalation_recommendation = "foreground"
    target_id, observation = _target_and_capture(service)

    service.act(
        "click",
        target_id,
        OWNER,
        element_token=observation.elements[0].token,
        approval_mode="allow_all",
        capture_after=True,
    )

    snapshot = service.status_snapshot()
    assert snapshot["last_requested_delivery"] == "auto"
    assert snapshot["last_delivery_mode"] == "background"
    assert snapshot["last_driver_effect"] == "suspected_noop"
    assert snapshot["last_native_change"] == "unchanged"
    assert snapshot["last_degraded"] is True
    assert snapshot["last_escalation_recommendation"] == "foreground"
    assert snapshot["last_verdict"] == "escalate"
    assert snapshot["last_next_step"] == "retry_foreground_once"

    view = computer_live_control_view(snapshot, OWNER.thread_id)
    visible = view.last_action.casefold()
    assert "requested auto" in visible
    assert "delivered background" in visible
    assert "driver suspected noop" in visible
    assert "native unchanged" in visible
    assert "degraded" in visible
    assert "foreground" in visible
    assert "verdict escalate" in visible
    assert "next retry foreground once" in visible
    assert "synthetic reversible action" not in visible


def test_action_receipt_log_includes_only_safe_classification_fields(
    service,
    fake_transport,
    caplog,
) -> None:
    target_id, observation = _target_and_capture(service)
    fake_transport.scenario.effect = "unverifiable"
    fake_transport.scenario.action_degraded = True
    fake_transport.scenario.escalation_recommendation = "px"
    signature = ("safe-log",)
    service.begin_tool_call(signature)

    receipt = service.act(
        "click",
        target_id,
        OWNER,
        element_token=observation.elements[0].token,
        approval_mode="allow_all",
    )
    with caplog.at_level(logging.INFO, logger="row_bot.computer_use.service"):
        service.end_tool_call(
            signature,
            action_family="click",
            route=receipt.route,
            delivery_mode=receipt.delivery_mode,
            driver_effect=receipt.driver_effect,
            requested_delivery=receipt.requested_delivery,
            degraded=receipt.degraded,
            escalation_recommendation=receipt.escalation_recommendation,
            verdict=receipt.verdict,
            next_step=receipt.next_step,
        )

    logs = [
        record.message
        for record in caplog.records
        if record.message.startswith("computer_use.action_receipt ")
    ]
    assert len(logs) == 1
    assert "requested_delivery=auto" in logs[0]
    assert "delivery_mode=background" in logs[0]
    assert "driver_effect=unverifiable" in logs[0]
    assert "degraded=true" in logs[0]
    assert "escalation_recommendation=px" in logs[0]
    assert "verdict=verify_fresh_state" in logs[0]
    assert "next_step=capture_same_target" in logs[0]
    assert "Synthetic" not in logs[0]


def test_computer_use_guide_encodes_the_bounded_driver_ladder() -> None:
    guide = Path("tool_guides/computer_use_guide/SKILL.md").read_text(
        encoding="utf-8"
    ).casefold()

    assert "start with `delivery_mode=auto`" in guide
    assert "explicit `delivery_mode=foreground`" in guide
    assert "prior structured recommendation" in guide
    assert "fresh exact-target" in guide
    assert "suspected_noop" in guide
    assert "degraded" in guide
    assert "`px`" in guide
    assert "screenshot-grounded" in guide
    assert "`page`" in guide
    assert "unsupported" in guide
    assert "never replay" in guide
    assert "one" in guide
