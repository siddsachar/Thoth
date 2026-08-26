from __future__ import annotations

import base64
import concurrent.futures
import io
import json
import logging

import pytest
from PIL import Image, ImageDraw

from row_bot.computer_use.service import (
    MODEL_MAX_ELEMENTS,
    MODEL_MAX_SEMANTIC_BYTES,
    ComputerUseError,
    LeaseOwner,
    Observation,
)
from row_bot.tools.computer_use_tool import _observation_payload
from tests.fixtures.fake_cua import SANITIZED_NATIVE_BROWSER_APPS


OWNER = LeaseOwner("performance-thread", "performance-generation", "performance-task")


def _png(*, changed: bool = False) -> str:
    image = Image.new("RGB", (64, 64), "white")
    if changed:
        ImageDraw.Draw(image).rectangle((8, 8, 42, 42), fill="blue")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _grid_png(*, cursor_badge_only: bool = False, displayed_text: bool = False) -> str:
    image = Image.new("RGB", (320, 120), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 30, 280, 82), outline="black", width=2)
    if displayed_text:
        draw.rectangle((36, 46, 180, 64), fill="navy")
    if cursor_badge_only:
        draw.ellipse((132, 44, 158, 70), fill="deepskyblue")
        draw.rectangle((156, 40, 220, 74), fill="deepskyblue")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _browser_target(service, fake_transport) -> tuple[str, Observation]:
    fake_transport.scenario.apps = SANITIZED_NATIVE_BROWSER_APPS
    fake_transport.scenario.windows = (
        {
            "window_id": 501,
            "pid": 2501,
            "app_name": "msedge.exe",
            "title": "Example media - Microsoft Edge",
            "bounds": {"x": 0, "y": 0, "width": 1280, "height": 720},
            "is_on_screen": True,
        },
    )
    fake_transport.scenario.capture_pid = 2501
    fake_transport.scenario.capture_window_id = 501
    service.acquire(OWNER, validate_context=False)
    target_id = service.list_windows(OWNER, app="Microsoft Edge")[0]["target_id"]
    return target_id, service.capture(target_id, OWNER)


def test_privacy_safe_action_receipt_logs_phase_timings_and_counts_once(
    service,
    fake_transport,
    caplog,
) -> None:
    private_title = "Private workbook alpha.xlsx"
    private_label = "Secret customer balance"
    fake_transport.scenario.windows = (
        {
            "window_id": 701,
            "pid": 2701,
            "app_name": "Notepad",
            "title": private_title,
            "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
            "is_on_screen": True,
        },
    )
    fake_transport.scenario.capture_pid = 2701
    fake_transport.scenario.capture_window_id = 701
    fake_transport.scenario.semantic_elements = (
        {"role": "text", "label": private_label},
    )
    signature = ("capture", "private-token", 987654321, 123456789)

    with caplog.at_level(logging.INFO, logger="row_bot.computer_use.service"):
        service.begin_tool_call(signature)
        observation = service.capture(owner=OWNER, app="Notepad")
        service.end_tool_call(signature, action_family="capture")

    receipts = [
        record.message
        for record in caplog.records
        if record.message.startswith("computer_use.action_receipt ")
    ]
    assert len(receipts) == 1
    receipt = receipts[0]
    assert "action_family=capture" in receipt
    assert "success=true error_code=ok" in receipt
    assert "driver_start_ms=" in receipt
    assert "discovery_ms=" in receipt
    assert "native_capture_ms=" in receipt
    assert "optional_vision_ms=" in receipt
    assert "total_ms=" in receipt
    assert "driver_calls=2" in receipt
    assert "capture_calls=1" in receipt
    assert "semantic_refresh_calls=0" in receipt
    assert "vision_calls=0" in receipt
    forbidden = (
        private_title,
        private_label,
        observation.elements[0].token,
        "private-token",
        "iVBOR",
        "987654321",
        "123456789",
    )
    assert all(value not in receipt for value in forbidden)


def test_active_app_metadata_survives_safe_inventory_projection(service, fake_transport) -> None:
    fake_transport.scenario.apps = SANITIZED_NATIVE_BROWSER_APPS
    service.acquire(OWNER, validate_context=False)

    apps = service.list_apps(OWNER)

    assert apps == [
        {"name": "msedge.exe", "running": True, "active": True},
        {"name": "Notepad", "running": True, "active": False},
    ]


def test_duplicate_canonical_app_rows_merge_active_state_in_stable_order(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.apps = (
        {"name": "msedge.exe", "running": True, "active": False},
        {"name": "Microsoft Edge", "running": True, "active": True},
    )
    service.acquire(OWNER, validate_context=False)

    assert service.list_apps(OWNER) == [
        {"name": "msedge.exe", "running": True, "active": True},
    ]


def test_prepared_focus_makes_accepted_enter_effective_without_refocusing(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.accepted_background_noop_tools = frozenset({"press_key"})
    target_id, _observation = _browser_target(service, fake_transport)

    background = service.act("key", target_id, OWNER, keys="enter")
    assert background.action_dispatched is True
    assert fake_transport.effective_keys == []

    service.act("focus", target_id, OWNER)
    service.act("key", target_id, OWNER, keys="enter")
    service.act("scroll", target_id, OWNER, direction="down", amount=3)
    current = service.current_observation(target_id)
    assert current is not None
    service.act("click", target_id, OWNER, element_token=current.elements[0].token)
    service.act("drag", target_id, OWNER, x=0, y=0, end_x=0, end_y=0)

    assert fake_transport.effective_keys == ["enter"]
    calls = [
        (name, args)
        for name, args in fake_transport.calls
        if name in {"bring_to_front", "press_key", "scroll", "click", "drag"}
    ]
    assert [name for name, _args in calls].count("bring_to_front") == 1
    press_keys = [arguments for name, arguments in calls if name == "press_key"]
    assert press_keys[0].get("delivery_mode", "background") == "background"
    assert press_keys[1]["delivery_mode"] == "foreground"
    for name, arguments in calls:
        if name in {"scroll", "click", "drag"}:
            assert arguments["delivery_mode"] == "foreground"
    assert calls[-1][1]["delivery_mode"] == "foreground"


class _UnknownVision:
    def __init__(self) -> None:
        self.calls = 0

    def analyze(self, _image: bytes, _question: str) -> str:
        self.calls += 1
        return "The requested outcome is unknown."


def test_pixel_change_with_unknown_vision_is_not_goal_verification(service, fake_transport) -> None:
    vision = _UnknownVision()
    service._vision_service = vision
    fake_transport.scenario.capture_dimensions = (64, 64)
    fake_transport.scenario.capture_images = (_png(), _png(changed=True))
    fake_transport.scenario.effect = "unverifiable"
    target_id, _observation = _browser_target(service, fake_transport)

    result = service.act(
        "click",
        target_id,
        OWNER,
        x=12,
        y=12,
        capture_after=True,
        visual_question="Is the requested result complete?",
        approval_mode="allow_all",
    )

    assert isinstance(result, Observation)
    assert result.action_dispatched is True
    assert result.action_completed is False
    assert result.driver_effect == "unverifiable"
    assert result.visual_change == "changed"
    assert result.effect_verified is False
    assert vision.calls == 1
    assert service.performance_snapshot()["pixel_captures"] == 2
    assert service.performance_snapshot()["vision_calls"] == 1
    payload = json.loads(_observation_payload(result))
    assert payload["ok"] is True
    assert payload["error"] is False
    assert payload["vision_evidence"]


def test_exact_replacement_ignores_unrelated_tree_churn_and_blocks_dependent_mutation(
    service,
    fake_transport,
) -> None:
    stable_target = {
        "role": "DataItem",
        "label": "Selected item",
        "value": "",
        "enabled": True,
        "selected": True,
        "frame": {"x": 20, "y": 30, "w": 260, "h": 52},
    }
    volatile_before = tuple(
        {"role": "Text", "label": f"Status {index:04d}", "value": ""}
        for index in range(1_800)
    )
    volatile_after = tuple(
        {
            "role": "Text",
            "label": f"Status {index:04d}",
            "value": "changed elsewhere" if index == 1_799 else "",
        }
        for index in range(1_800)
    )
    fake_transport.scenario.apps = (
        {"name": "Grid Editor", "running": True, "active": True},
    )
    fake_transport.scenario.windows = (
        {
            "window_id": 711,
            "pid": 2711,
            "app_name": "Grid Editor",
            "title": "Untitled grid",
            "bounds": {"x": 0, "y": 0, "width": 320, "height": 120},
            "is_on_screen": True,
        },
    )
    fake_transport.scenario.capture_pid = 2711
    fake_transport.scenario.capture_window_id = 711
    fake_transport.scenario.capture_dimensions = (320, 120)
    fake_transport.scenario.capture_images = (
        _grid_png(),
        _grid_png(),
        _grid_png(cursor_badge_only=True),
    )
    fake_transport.scenario.semantic_snapshots = (
        (stable_target, *volatile_before),
        (stable_target, *volatile_before),
        (stable_target, *volatile_after),
    )
    fake_transport.scenario.rotate_element_tokens = True
    service.acquire(OWNER, validate_context=False)
    target_id = service.list_windows(OWNER, app="Grid Editor")[0]["target_id"]
    initial = service.capture(target_id, OWNER)
    replacement = "private requested value"

    result = service.act(
        "replace_text",
        target_id,
        OWNER,
        element_token=initial.elements[0].token,
        text=replacement,
        capture_after=True,
    )

    assert isinstance(result, Observation)
    assert result.action_dispatched is True
    assert result.action_completed is False
    assert result.effect_verified is False
    assert result.native_change == "unchanged"
    assert result.outcome == "suspected_noop"
    assert result.verified_scope == ""
    names_before_block = [name for name, _args in fake_transport.calls]

    with pytest.raises(ComputerUseError) as dependent_text:
        service.act(
            "replace_text",
            target_id,
            OWNER,
            element_token=result.elements[0].token,
            text="second dependent value",
        )
    with pytest.raises(ComputerUseError) as dependent_commit:
        service.act("key", target_id, OWNER, keys="enter")

    assert dependent_text.value.code == "pending_mutation"
    assert dependent_commit.value.code == "pending_mutation"
    assert [name for name, _args in fake_transport.calls] == names_before_block
    assert service.computer_use_completion_blocked(OWNER) is True


def test_provider_echo_without_displayed_target_change_is_unverified(
    service,
    fake_transport,
) -> None:
    old = {
        "role": "Edit",
        "label": "Document field",
        "value": "old",
        "enabled": True,
        "frame": {"x": 20, "y": 30, "w": 260, "h": 52},
    }
    echoed = {**old, "value": "new"}
    fake_transport.scenario.capture_dimensions = (320, 120)
    fake_transport.scenario.capture_images = (
        _grid_png(),
        _grid_png(),
        _grid_png(cursor_badge_only=True),
    )
    fake_transport.scenario.semantic_snapshots = ((old,), (old,), (echoed,))
    fake_transport.scenario.rotate_element_tokens = True
    target_id, initial = _browser_target(service, fake_transport)

    result = service.act(
        "replace_text",
        target_id,
        OWNER,
        element_token=initial.elements[0].token,
        text="new",
        capture_after=True,
    )

    assert result.outcome == "provider_echo_unverified"
    assert result.native_change == "changed"
    assert result.visual_change == "unchanged"
    assert result.effect_verified is False
    assert result.action_completed is False


def test_exact_replacement_with_target_display_change_is_displayed_scope_only(
    service,
    fake_transport,
) -> None:
    old = {
        "role": "Edit",
        "label": "Document field",
        "value": "old",
        "enabled": True,
        "frame": {"x": 20, "y": 30, "w": 260, "h": 52},
    }
    changed = {**old, "value": "new"}
    fake_transport.scenario.capture_dimensions = (320, 120)
    fake_transport.scenario.capture_images = (
        _grid_png(),
        _grid_png(),
        _grid_png(displayed_text=True),
    )
    fake_transport.scenario.semantic_snapshots = ((old,), (old,), (changed,))
    fake_transport.scenario.rotate_element_tokens = True
    target_id, initial = _browser_target(service, fake_transport)

    result = service.act(
        "replace_text",
        target_id,
        OWNER,
        element_token=initial.elements[0].token,
        text="new",
        capture_after=True,
    )

    assert result.outcome == "displayed_postcondition_observed"
    assert result.effect_verified is True
    assert result.action_completed is True
    assert result.verified_scope == "displayed_target"


def test_exact_replacement_already_satisfied_skips_dispatch(service, fake_transport) -> None:
    satisfied = {
        "role": "Edit",
        "label": "Document field",
        "value": "already",
        "enabled": True,
        "frame": {"x": 20, "y": 30, "w": 260, "h": 52},
    }
    fake_transport.scenario.capture_dimensions = (320, 120)
    fake_transport.scenario.capture_images = (_grid_png(), _grid_png())
    fake_transport.scenario.semantic_snapshots = ((satisfied,), (satisfied,))
    fake_transport.scenario.rotate_element_tokens = True
    target_id, initial = _browser_target(service, fake_transport)
    calls_before = len(fake_transport.calls)

    result = service.act(
        "replace_text",
        target_id,
        OWNER,
        element_token=initial.elements[0].token,
        text="already",
        capture_after=True,
    )

    assert result.outcome == "already_satisfied"
    assert result.action_dispatched is False
    assert result.action_completed is True
    assert result.effect_verified is True
    assert result.verified_scope == "displayed_target"
    assert all(name != "set_value" for name, _args in fake_transport.calls[calls_before:])


def test_fresh_exact_capture_clears_pending_only_with_displayed_target_evidence(
    service,
    fake_transport,
) -> None:
    old = {
        "role": "Edit",
        "label": "Document field",
        "value": "old",
        "enabled": True,
        "frame": {"x": 20, "y": 30, "w": 260, "h": 52},
    }
    requested = {**old, "value": "new"}
    fake_transport.scenario.capture_dimensions = (320, 120)
    fake_transport.scenario.capture_images = (
        _grid_png(),
        _grid_png(),
        _grid_png(cursor_badge_only=True),
        _grid_png(displayed_text=True),
    )
    fake_transport.scenario.semantic_snapshots = (
        (old,),
        (old,),
        (requested,),
        (requested,),
    )
    fake_transport.scenario.rotate_element_tokens = True
    target_id, initial = _browser_target(service, fake_transport)

    uncertain = service.act(
        "replace_text",
        target_id,
        OWNER,
        element_token=initial.elements[0].token,
        text="new",
    )
    resolved = service.capture(target_id, OWNER)

    assert uncertain.outcome == "provider_echo_unverified"
    assert uncertain.computer_use_completion_blocked is True
    assert resolved.computer_use_completion_blocked is False
    assert service.computer_use_completion_blocked(OWNER) is False


def test_permitted_recovery_click_invalidates_pending_pixel_comparison(
    service,
    fake_transport,
) -> None:
    old = {
        "role": "Edit",
        "label": "Document field",
        "value": "old",
        "enabled": True,
        "frame": {"x": 20, "y": 30, "w": 260, "h": 52},
    }
    requested = {**old, "value": "new"}
    fake_transport.scenario.capture_dimensions = (320, 120)
    fake_transport.scenario.capture_images = (
        _grid_png(),
        _grid_png(),
        _grid_png(cursor_badge_only=True),
        _grid_png(displayed_text=True),
    )
    fake_transport.scenario.semantic_snapshots = (
        (old,),
        (old,),
        (requested,),
        (requested,),
    )
    fake_transport.scenario.rotate_element_tokens = True
    target_id, initial = _browser_target(service, fake_transport)
    uncertain = service.act(
        "replace_text",
        target_id,
        OWNER,
        element_token=initial.elements[0].token,
        text="new",
    )

    service.act(
        "click",
        target_id,
        OWNER,
        element_token=uncertain.elements[0].token,
        approval_mode="allow_all",
    )
    later = service.capture(target_id, OWNER)

    assert later.computer_use_completion_blocked is True
    assert service.computer_use_completion_blocked(OWNER) is True


def test_single_accepted_unchanged_coordinate_action_is_not_a_tool_error(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.capture_dimensions = (64, 64)
    fake_transport.scenario.capture_images = (_png(), _png())
    fake_transport.scenario.effect = "unverifiable"
    target_id, _observation = _browser_target(service, fake_transport)

    result = service.act("click", target_id, OWNER, x=12, y=12, capture_after=True)
    payload = json.loads(_observation_payload(result))

    assert payload["ok"] is True
    assert payload["error"] is False
    assert payload["action_dispatched"] is True
    assert payload["action_completed"] is False
    assert payload["visual_change"] == "unchanged"
    assert payload["effect_verified"] is False
    assert "error_code" not in payload


def test_model_projection_is_bounded_but_full_validated_elements_remain_ephemeral(
    service,
    fake_transport,
) -> None:
    interactive = tuple(
        {"role": "Button", "label": f"Action {index:03d}"}
        for index in range(100)
    )
    duplicate_text = tuple(
        {"role": "Text", "label": "Repeated status"}
        for _index in range(100)
    )
    fake_transport.scenario.semantic_elements = duplicate_text + interactive
    target_id, observation = _browser_target(service, fake_transport)

    rendered = observation.model_text()

    assert len(observation.elements) == 200
    assert rendered.count(" token=") <= 80
    assert len(rendered.encode("utf-8")) <= 12 * 1024 + 2048
    assert "Action 000" in rendered
    assert "additional semantic elements omitted" in rendered
    assert service.current_observation(target_id) is observation


def test_dense_grid_projection_preserves_selected_item_and_bounded_control_mix(
    service,
    fake_transport,
) -> None:
    private_values = tuple(f"private-value-{index:04d}" for index in range(1_200))
    chrome = tuple(
        {
            "role": "Button",
            "label": f"Application control {index:03d}",
            "visible": True,
        }
        for index in range(400)
    )
    document = tuple(
        {
            "role": "GridCell",
            "label": f"Item {index:04d}",
            "value": private_values[index],
            "visible": True,
            "enabled": index != 7,
            "selected": index == 1_199,
        }
        for index in range(1_200)
    )
    fake_transport.scenario.semantic_elements = chrome + document

    _target_id, observation = _browser_target(service, fake_transport)
    projected, omitted = observation.model_elements()
    rendered = observation.model_text()
    projected_roles = [element.role.casefold() for element in projected]
    semantic_lines = "\n".join(
        line for line in rendered.splitlines() if line.startswith("- token=")
    )

    assert len(observation.elements) == 1_600
    assert len(projected) == MODEL_MAX_ELEMENTS
    assert omitted == len(observation.elements) - MODEL_MAX_ELEMENTS
    assert 8 <= projected_roles.count("gridcell") <= 16
    assert projected_roles.count("button") >= 32
    assert "Item 1199" in rendered
    assert "selected=true" in rendered
    assert "enabled=false" in rendered
    assert all(value not in rendered for value in private_values)
    assert len(semantic_lines.encode("utf-8")) <= MODEL_MAX_SEMANTIC_BYTES
    assert observation.status is not None
    assert observation.status.projected_count == MODEL_MAX_ELEMENTS


def test_deep_current_document_action_survives_chrome_crowd_out(service, fake_transport) -> None:
    chrome = tuple(
        {"role": "Button", "label": f"Chrome {index:03d}", "depth": 2}
        for index in range(160)
    )
    fake_transport.scenario.semantic_elements = chrome + (
        {
            "role": "Button",
            "label": "Deep document action",
            "depth": 25,
            "in_web_content": True,
            "visible": True,
        },
    )
    target_id, observation = _browser_target(service, fake_transport)

    assert len(observation.elements) == 161
    assert "Deep document action" in observation.model_text()
    assert observation.status is not None
    assert observation.status.projected_count == 80


def test_semantic_refresh_requests_no_pixels_and_has_separate_counters(
    service,
    fake_transport,
) -> None:
    target_id, first = _browser_target(service, fake_transport)
    before = service.performance_snapshot()

    refreshed = service.refresh_semantics(target_id, OWNER)
    after = service.performance_snapshot()

    capture_args = [args for name, args in fake_transport.calls if name == "get_window_state"][-1]
    assert capture_args == {
        "pid": 2501,
        "window_id": 501,
        "include_screenshot": False,
        "max_elements": 2_000,
        "max_depth": 25,
        "session": "row-bot-test-session",
    }
    assert refreshed.screenshot == first.screenshot
    assert after["pixel_captures"] == before["pixel_captures"]
    assert after["semantic_refreshes"] == before["semantic_refreshes"] + 1
    assert after["vision_calls"] == before["vision_calls"]


def test_known_background_target_is_focused_once_then_reuses_prepared_delivery(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.apps = (
        {"name": "Notepad", "running": True, "active": False},
    )
    fake_transport.scenario.windows = (
        {
            "window_id": 601,
            "pid": 2601,
            "app_name": "Notepad",
            "title": "Synthetic note - Notepad",
            "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
            "is_on_screen": True,
        },
    )
    fake_transport.scenario.capture_pid = 2601
    fake_transport.scenario.capture_window_id = 601
    service.acquire(OWNER, validate_context=False)
    service.list_apps(OWNER)
    target_id = service.list_windows(OWNER, app="Notepad")[0]["target_id"]
    service.capture(target_id, OWNER)

    service.act("key", target_id, OWNER, keys="a", approval_mode="allow_all")
    service.act("scroll", target_id, OWNER, direction="down", amount=2, approval_mode="allow_all")

    assert [name for name, _args in fake_transport.calls].count("bring_to_front") == 1
    delivered = [
        args
        for name, args in fake_transport.calls
        if name in {"press_key", "scroll"}
    ]
    assert [args["delivery_mode"] for args in delivered] == ["foreground", "foreground"]


def test_tokens_omitted_from_the_model_projection_cannot_be_used(service, fake_transport) -> None:
    fake_transport.scenario.semantic_elements = tuple(
        {"role": "Button", "label": f"Action {index:03d}"}
        for index in range(100)
    )
    target_id, observation = _browser_target(service, fake_transport)
    omitted_token = observation.elements[90].token

    with pytest.raises(ComputerUseError, match="compact model observation"):
        service.act("click", target_id, OWNER, element_token=omitted_token)

    assert [name for name, _args in fake_transport.calls].count("click") == 0


def test_prepared_target_clears_on_takeover_and_stays_clear_after_resume(
    service,
    fake_transport,
) -> None:
    target_id, _observation = _browser_target(service, fake_transport)
    service.act("focus", target_id, OWNER)
    assert service.status_snapshot()["foreground_prepared"] is True

    token = service.take_over()
    assert service.status_snapshot()["foreground_prepared"] is False
    service.resume(OWNER, takeover_token=token)

    assert service.status_snapshot()["foreground_prepared"] is False
    service.act("scroll", target_id, OWNER, direction="down", amount=3)
    scroll_args = [args for name, args in fake_transport.calls if name == "scroll"][-1]
    assert scroll_args.get("delivery_mode", "background") == "background"


def test_prepared_target_clears_on_target_change_focus_failure_and_stop(
    service,
    fake_transport,
) -> None:
    target_id, _observation = _browser_target(service, fake_transport)
    service.act("focus", target_id, OWNER)
    fake_transport.scenario.windows = (
        {
            "window_id": 601,
            "pid": 2601,
            "app_name": "Notepad",
            "title": "Example note - Notepad",
            "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
            "is_on_screen": True,
        },
    )
    fake_transport.scenario.capture_pid = 2601
    fake_transport.scenario.capture_window_id = 601
    other_target = service.list_windows(OWNER, app="Notepad")[0]["target_id"]
    service.capture(other_target, OWNER)
    assert service.status_snapshot()["foreground_prepared"] is False

    fake_transport.scenario.action_error_code = "focus_failed"
    with pytest.raises(ComputerUseError):
        service.act("focus", other_target, OWNER)
    assert service.status_snapshot()["foreground_prepared"] is False

    fake_transport.scenario.action_error_code = ""
    service.act("focus", other_target, OWNER)
    service.stop()
    assert service.status_snapshot()["foreground_prepared"] is False
    assert service.status_snapshot()["active"] is False


def test_prepared_target_clears_on_cancellation_and_driver_disconnect(
    service,
    fake_transport,
    monkeypatch,
) -> None:
    import row_bot.computer_use.service as service_module

    target_id, _observation = _browser_target(service, fake_transport)
    service.act("focus", target_id, OWNER)
    cancelled_scope = type("CancelledScope", (), {"is_cancelled": lambda self: True})()
    monkeypatch.setattr(service_module, "current_cancellation_scope", lambda: cancelled_scope)
    with pytest.raises(concurrent.futures.CancelledError, match="stopped"):
        service.act("scroll", target_id, OWNER, direction="down", amount=3)
    assert service.status_snapshot()["foreground_prepared"] is False

    monkeypatch.setattr(service_module, "current_cancellation_scope", lambda: None)
    service.stop()
    target_id, _observation = _browser_target(service, fake_transport)
    service.act("focus", target_id, OWNER)
    fake_transport.scenario.disconnect = True
    with pytest.raises(ComputerUseError) as exc_info:
        service.act("scroll", target_id, OWNER, direction="down", amount=3)
    assert exc_info.value.code == "driver_unavailable"
    assert service.status_snapshot()["foreground_prepared"] is False
    assert service.status_snapshot()["active"] is False


def test_unknown_visual_comparisons_do_not_consume_no_progress_budget(
    service,
    fake_transport,
    monkeypatch,
) -> None:
    fake_transport.scenario.capture_dimensions = (64, 64)
    fake_transport.scenario.capture_images = tuple(_png() for _ in range(4))
    fake_transport.scenario.effect = "unverifiable"
    target_id, _observation = _browser_target(service, fake_transport)
    monkeypatch.setattr(service, "_visual_effect_in_region", lambda *_args, **_kwargs: "unknown")

    for _attempt in range(3):
        result = service.act(
            "click",
            target_id,
            OWNER,
            x=12,
            y=12,
            capture_after=True,
            approval_mode="allow_all",
        )
        assert result.visual_change == "unknown"
        assert result.action_completed is False

    assert service.status_snapshot()["consecutive_visual_no_effects"] == 0
    assert service.status_snapshot()["state"] == "observing"


def test_generic_three_action_flow_stays_inside_native_budget(service, fake_transport) -> None:
    target_id, observation = _browser_target(service, fake_transport)
    before = service.performance_snapshot()
    service.act("key", target_id, OWNER, keys="tab")
    service.act("scroll", target_id, OWNER, direction="down", amount=2)
    service.act("click", target_id, OWNER, element_token=observation.elements[0].token)
    after = service.performance_snapshot()

    assert after["driver_calls"] - before["driver_calls"] == 3
    assert after["pixel_captures"] - before["pixel_captures"] == 0
    assert after["semantic_refreshes"] - before["semantic_refreshes"] == 0
    assert after["vision_calls"] - before["vision_calls"] == 0


def test_native_browser_four_action_flow_has_exact_bounded_counts(service, fake_transport) -> None:
    target_id, observation = _browser_target(service, fake_transport)
    before = service.performance_snapshot()
    service.act("focus", target_id, OWNER, approval_mode="allow_all")
    service.act("type", target_id, OWNER, element_token=observation.elements[2].token, text="synthetic query")
    service.refresh_semantics(target_id, OWNER)
    current = service.current_observation(target_id)
    service.act("click", target_id, OWNER, element_token=current.elements[0].token)
    service.refresh_semantics(target_id, OWNER)
    service.act("key", target_id, OWNER, keys="space")
    after = service.performance_snapshot()

    assert after["driver_calls"] - before["driver_calls"] == 6
    assert after["pixel_captures"] - before["pixel_captures"] == 0
    assert after["semantic_refreshes"] - before["semantic_refreshes"] == 2
    assert after["vision_calls"] - before["vision_calls"] == 0
    assert [name for name, _args in fake_transport.calls].count("bring_to_front") == 1
