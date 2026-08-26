from __future__ import annotations

import base64
import io
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
    target_id = service.list_windows(OWNER, app="msedge.exe")[0]["target_id"]
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
    fake_transport.scenario.apps = (
        {"name": "Notepad", "pid": 2701, "running": True, "active": True},
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
    assert "driver_calls=3" in receipt
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
        {"name": "msedge.exe", "running": True, "active": True},
    )
    service.acquire(OWNER, validate_context=False)

    assert service.list_apps(OWNER) == [
        {"name": "msedge.exe", "running": True, "active": True},
    ]




class _UnknownVision:
    def __init__(self) -> None:
        self.calls = 0

    def analyze(self, _image: bytes, _question: str) -> str:
        self.calls += 1
        return "The requested outcome is unknown."






















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


def test_known_background_key_and_scroll_try_background_without_preemptive_focus(
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

    assert [name for name, _args in fake_transport.calls].count("bring_to_front") == 0
    delivered = [
        args
        for name, args in fake_transport.calls
        if name in {"press_key", "scroll"}
    ]
    assert [args.get("delivery_mode", "background") for args in delivered] == [
        "background",
        "background",
    ]


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
    service.act("type", target_id, OWNER, text="synthetic query")
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
