from __future__ import annotations

import base64
import concurrent.futures
import io
import json

import pytest
from PIL import Image, ImageDraw

from row_bot.computer_use.service import ComputerUseError, LeaseOwner, Observation
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
    assert result.action_completed is True
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
    assert payload["action_completed"] is True
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
        assert result.action_completed is True

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
