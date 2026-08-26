from __future__ import annotations

import base64
import io

import pytest
from PIL import Image, ImageDraw

from row_bot.computer_use.service import ComputerUseError, LeaseOwner, Observation


OWNER = LeaseOwner("visual-thread", "visual-generation", "visual-task")


def _png(*, changed_box: tuple[int, int, int, int] | None = None) -> str:
    image = Image.new("RGB", (64, 64), "white")
    if changed_box is not None:
        ImageDraw.Draw(image).rectangle(changed_box, fill="blue")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _paint_target(service, fake_transport) -> tuple[str, Observation]:
    fake_transport.scenario.windows = ({
        "window_id": 303,
        "pid": 5303,
        "app_name": "Paint",
        "title": "Untitled - Paint",
        "bounds": {"x": 10, "y": 10, "width": 128, "height": 128},
        "is_on_screen": True,
    },)
    service.acquire(OWNER, validate_context=False)
    target = service.list_windows(OWNER, app="Paint")[0]["target_id"]
    return target, service.capture(target, OWNER)


def test_coordinate_drag_uses_screenshot_coordinates_once_with_one_optional_capture(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.capture_dimensions = (64, 64)
    fake_transport.scenario.capture_images = (_png(), _png(changed_box=(8, 8, 42, 42)))
    fake_transport.scenario.effect = "unverifiable"
    target, _observation = _paint_target(service, fake_transport)
    calls_before = len(fake_transport.calls)

    result = service.act(
        "drag",
        target,
        OWNER,
        x=10,
        y=10,
        end_x=40,
        end_y=40,
        capture_after=True,
    )

    calls = fake_transport.calls[calls_before:]
    drag_args = [args for name, args in calls if name == "drag"]
    assert drag_args == [{
        "pid": 5303,
        "window_id": 303,
        "from_x": 10,
        "from_y": 10,
        "to_x": 40,
        "to_y": 40,
        "session": "row-bot-test-session",
    }]
    assert [name for name, _args in calls] == ["drag", "get_window_state"]
    assert isinstance(result, Observation)
    assert result.action_effect == "delivered_unverified"
    assert result.visual_change == "unknown"
    assert result.effect_verified is False


def test_unchanged_background_drag_is_not_replayed_after_driver_acceptance(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.capture_dimensions = (64, 64)
    fake_transport.scenario.capture_images = (
        _png(),
        _png(),
    )
    fake_transport.scenario.effect = "unverifiable"
    fake_transport.scenario.foreground_effect = "unverifiable"
    target, _observation = _paint_target(service, fake_transport)
    calls_before = len(fake_transport.calls)

    result = service.act(
        "drag", target, OWNER,
        x=10, y=10, end_x=40, end_y=40,
        capture_after=True,
    )

    calls = fake_transport.calls[calls_before:]
    drags = [args for name, args in calls if name == "drag"]
    assert len(drags) == 1
    assert "delivery_mode" not in drags[0]
    assert isinstance(result, Observation)
    assert result.visual_change == "unknown"
    assert result.delivery_mode == "background"
    assert service.status_snapshot()["last_visual_change"] == "unknown"


def test_repeated_accepted_no_effect_drags_remain_useful_delivery(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.capture_dimensions = (64, 64)
    fake_transport.scenario.capture_images = tuple(_png() for _ in range(4))
    fake_transport.scenario.effect = "unverifiable"
    fake_transport.scenario.foreground_effect = "unverifiable"
    target, _observation = _paint_target(service, fake_transport)

    for index in range(3):
        result = service.act(
            "drag", target, OWNER,
            x=5 + index, y=5, end_x=35 + index, end_y=35,
            capture_after=True,
        )
        assert isinstance(result, Observation)
        assert result.action_effect == "delivered_unverified"
        assert result.visual_change == "unknown"

    assert service.status_snapshot()["state"] == "observing"
    assert "consecutive_visual_no_effects" not in service.status_snapshot()
    assert [name for name, _args in fake_transport.calls].count("drag") == 3


def test_non_text_background_refusal_is_not_replayed(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.capture_dimensions = (64, 64)
    fake_transport.scenario.capture_images = (_png(), _png(changed_box=(8, 8, 42, 42)))
    fake_transport.scenario.background_unavailable_tools = frozenset({"drag"})
    target, _observation = _paint_target(service, fake_transport)

    with pytest.raises(ComputerUseError) as exc_info:
        service.act(
            "drag", target, OWNER,
            x=10, y=10, end_x=40, end_y=40,
            capture_after=True,
        )

    drags = [args for name, args in fake_transport.calls if name == "drag"]
    assert len(drags) == 1
    assert "delivery_mode" not in drags[0]
    assert exc_info.value.code == "background_unavailable"


def test_semantic_bounds_are_not_presented_as_screenshot_coordinates(service, fake_transport) -> None:
    fake_transport.scenario.include_scale_factor = False
    fake_transport.scenario.element_frame = (3000, 1700, 400, 100)
    target, observation = _paint_target(service, fake_transport)

    rendered = observation.model_text()
    assert observation.scale_factor is None
    assert "scale unknown" in rendered
    assert "3000" not in rendered
    assert "bounds=" not in rendered
    assert target in rendered
