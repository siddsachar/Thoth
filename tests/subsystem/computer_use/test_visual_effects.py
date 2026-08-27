from __future__ import annotations

import base64
import io
import json

import pytest
from PIL import Image, ImageDraw

from row_bot.computer_use.client import CuaElement
from row_bot.computer_use.service import (
    ComputerUseError,
    LeaseOwner,
    Observation,
    _semantic_fingerprint,
)
from row_bot.tools.computer_use_tool import _observation_payload


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


def _element(
    token: str,
    index: int,
    role: str,
    label: str,
    *,
    parent_index: int | None = None,
    selected: bool | None = None,
    checked: bool | None = None,
    expanded: bool | None = None,
    pressed: bool | None = None,
    enabled: bool | None = None,
    bounds: tuple[float, float, float, float] = (0, 0, 10, 10),
) -> CuaElement:
    return CuaElement(
        token=token,
        index=index,
        role=role,
        label=label,
        value="hidden value",
        bounds=bounds,
        depth=1 if parent_index is None else 2,
        parent_index=parent_index,
        selected=selected,
        checked=checked,
        expanded=expanded,
        pressed=pressed,
        enabled=enabled,
    )


def test_semantic_fingerprint_ignores_reference_rotation_geometry_and_order() -> None:
    before = (
        _element("old-root", 10, "Group", "Playback controls"),
        _element(
            "old-child",
            11,
            "Button",
            "Play",
            parent_index=10,
            selected=False,
            checked=False,
            expanded=False,
            pressed=False,
            enabled=True,
        ),
    )
    after = (
        _element(
            "new-child",
            41,
            "Button",
            "Play",
            parent_index=40,
            selected=False,
            checked=False,
            expanded=False,
            pressed=False,
            enabled=True,
            bounds=(101.25, 55.5, 11.0, 9.5),
        ),
        _element("new-root", 40, "Group", "Playback controls", bounds=(9, 9, 99, 99)),
    )

    assert _semantic_fingerprint(before) == _semantic_fingerprint(after)


@pytest.mark.parametrize(
    "changed",
    [
        {"role": "CheckBox"},
        {"label": "Pause"},
        {"selected": True},
        {"checked": True},
        {"expanded": True},
        {"pressed": True},
        {"enabled": False},
        {"parent_index": None},
    ],
)
def test_semantic_fingerprint_detects_meaningful_control_or_tree_change(changed) -> None:
    root = _element("root", 0, "Group", "Playback controls")
    baseline_fields = {
        "role": "Button",
        "label": "Play",
        "parent_index": 0,
        "selected": False,
        "checked": False,
        "expanded": False,
        "pressed": False,
        "enabled": True,
    }
    updated_fields = {**baseline_fields, **changed}
    before = (root, _element("before", 1, **baseline_fields))
    after = (root, _element("after", 1, **updated_fields))

    assert _semantic_fingerprint(before) != _semantic_fingerprint(after)


def test_unchanged_post_click_semantics_are_observed_but_not_verified(
    service,
    fake_transport,
) -> None:
    semantic = (
        {
            "role": "Button",
            "label": "Play",
            "selected": False,
            "checked": False,
            "expanded": False,
            "pressed": False,
            "enabled": True,
        },
    )
    fake_transport.scenario.semantic_snapshots = (semantic, semantic)
    fake_transport.scenario.rotate_element_tokens = True
    fake_transport.scenario.effect = "unverifiable"
    target, observation = _paint_target(service, fake_transport)

    result = service.act(
        "click",
        target,
        OWNER,
        element_token=observation.elements[0].token,
        capture_after=True,
    )

    assert result.native_change == "unchanged"
    assert result.effect_verified is False
    assert result.verified_scope == ""
    payload = json.loads(_observation_payload(result))
    assert "one alternative exact route" in payload["next_action"].casefold()
    assert "bounded wait" in payload["next_action"].casefold()


def test_changed_post_click_semantics_do_not_verify_the_intended_outcome(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.semantic_snapshots = (
        ({"role": "Button", "label": "Play", "selected": False},),
        ({"role": "Button", "label": "Play", "selected": True},),
    )
    fake_transport.scenario.rotate_element_tokens = True
    fake_transport.scenario.effect = "unverifiable"
    target, observation = _paint_target(service, fake_transport)

    result = service.act(
        "click",
        target,
        OWNER,
        element_token=observation.elements[0].token,
        expected_effect="Start playback",
        approval_mode="allow_all",
        capture_after=True,
    )

    assert result.native_change == "changed"
    assert result.effect_verified is False
    assert result.verified_scope == ""
    assert result.semantic_postcondition == "unavailable"
    payload = json.loads(_observation_payload(result))
    assert payload["action_dispatched"] is True
    assert payload["native_change"] == "changed"
    assert payload["effect_verified"] is False
    assert "intended outcome" not in payload["display_summary"].casefold()


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
        "delivery_mode": "foreground",
        "session": "row-bot-test-session",
    }]
    assert [name for name, _args in calls] == ["drag", "get_window_state"]
    assert isinstance(result, Observation)
    assert result.action_effect == "delivered_unverified"
    assert result.visual_change == "unknown"
    assert result.effect_verified is False
    assert result.delivery_mode == "foreground"


def test_unchanged_foreground_drag_is_not_replayed_after_driver_acceptance(
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
    assert drags[0]["delivery_mode"] == "foreground"
    assert [name for name, _args in calls] == ["drag", "get_window_state"]
    assert isinstance(result, Observation)
    assert result.visual_change == "unknown"
    assert result.delivery_mode == "foreground"
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


def test_foreground_drag_refusal_is_structured_and_not_replayed(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.capture_dimensions = (64, 64)
    fake_transport.scenario.capture_images = (_png(), _png(changed_box=(8, 8, 42, 42)))
    fake_transport.scenario.foreground_error_code = "focus_refused"
    target, observation = _paint_target(service, fake_transport)
    calls_before = len(fake_transport.calls)

    with pytest.raises(ComputerUseError) as exc_info:
        service.act(
            "drag", target, OWNER,
            x=10, y=10, end_x=40, end_y=40,
            capture_after=True,
        )

    calls = fake_transport.calls[calls_before:]
    assert calls == [
        (
            "drag",
            {
                "pid": 5303,
                "window_id": 303,
                "from_x": 10,
                "from_y": 10,
                "to_x": 40,
                "to_y": 40,
                "delivery_mode": "foreground",
                "session": "row-bot-test-session",
            },
        )
    ]
    assert exc_info.value.code == "focus_refused"
    assert exc_info.value.retryable is False
    assert service.current_observation(target) is observation
    assert observation.target.target_id == target
    assert (observation.target.pid, observation.target.window_id) == (5303, 303)


def test_semantic_paint_toolbar_click_keeps_accessibility_delivery(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.semantic_elements = (
        {"role": "Button", "label": "Rectangle", "enabled": True},
    )
    fake_transport.scenario.action_route = "accessibility"
    target, observation = _paint_target(service, fake_transport)
    calls_before = len(fake_transport.calls)

    result = service.act(
        "click",
        target,
        OWNER,
        element_token=observation.elements[0].token,
    )

    calls = fake_transport.calls[calls_before:]
    assert calls == [
        (
            "click",
            {
                "pid": 5303,
                "window_id": 303,
                "element_token": observation.elements[0].token,
                "session": "row-bot-test-session",
            },
        )
    ]
    assert result.route == "accessibility"
    assert result.delivery_mode == "background"
    assert all(name not in {"drag", "bring_to_front"} for name, _args in calls)


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
