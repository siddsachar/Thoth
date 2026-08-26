from __future__ import annotations

import pytest

from row_bot.computer_use.service import (
    ComputerUseError,
    ComputerUseService,
    LeaseOwner,
    Observation,
)


OWNER = LeaseOwner("text-thread", "text-generation", "text-task")


class _Vision:
    def __init__(self) -> None:
        self.questions: list[str] = []

    def analyze(self, _image: bytes, question: str) -> str:
        self.questions.append(question)
        return "Original content remains and the requested insertion is visible."


def _notepad_target(service, fake_transport) -> tuple[str, Observation]:
    fake_transport.scenario.windows = ({
        "window_id": 404,
        "pid": 5404,
        "app_name": "Notepad",
        "title": "target-a.txt - Notepad",
        "bounds": {"x": 10, "y": 10, "width": 800, "height": 600},
        "is_on_screen": True,
    },)
    service.acquire(OWNER, validate_context=False)
    target = service.list_windows(OWNER, app="Notepad", window_hint="target-a.txt")[0]["target_id"]
    return target, service.capture(target, OWNER)


def _selected_caret_surface(fake_transport) -> None:
    fake_transport.scenario.semantic_elements = (
        {
            "role": "TextField",
            "label": "Document editor",
            "enabled": True,
            "selected": True,
            "frame": {"x": 20, "y": 30, "w": 500, "h": 300},
        },
    )
    fake_transport.scenario.rotate_element_tokens = True


def test_type_token_fresh_rematches_and_uses_exact_driver_target(
    service,
    fake_transport,
) -> None:
    original = "TARGET A"
    fake_transport.scenario.document_value = original
    fake_transport.document_value = original
    fake_transport.scenario.delivery_profile = "background_refused"
    _selected_caret_surface(fake_transport)
    target, observation = _notepad_target(service, fake_transport)

    result = service.act(
        "type",
        target,
        OWNER,
        element_token=observation.elements[0].token,
        text="\nVERIFIED A",
        capture_after=True,
    )

    type_calls = [args for name, args in fake_transport.calls if name == "type_text"]
    assert len(type_calls) == 2
    assert all("element_token" in args and "element_index" not in args for args in type_calls)
    assert type_calls[0]["element_token"] != observation.elements[0].token
    assert type_calls[1]["element_token"] != type_calls[0]["element_token"]
    assert "delivery_mode" not in type_calls[0]
    assert type_calls[1]["delivery_mode"] == "foreground"
    assert fake_transport.document_value == original + "\nVERIFIED A"
    assert isinstance(result, Observation)
    assert all(name != "bring_to_front" for name, _args in fake_transport.calls)


def test_post_action_visual_question_runs_in_the_same_type_call(fake_client, fake_transport) -> None:
    vision = _Vision()
    service = ComputerUseService(
        client_factory=lambda: fake_client,
        approval_callback=lambda _payload: True,
        vision_service=vision,
    )
    fake_transport.scenario.document_value = "TARGET A"
    fake_transport.document_value = "TARGET A"
    _selected_caret_surface(fake_transport)
    target, observation = _notepad_target(service, fake_transport)

    result = service.act(
        "type",
        target,
        OWNER,
        element_token=observation.elements[0].token,
        text="\nVERIFIED A",
        capture_after=True,
        visual_question="Confirm the original visible content remains and the insertion is present.",
    )

    assert isinstance(result, Observation)
    assert len(vision.questions) == 1
    assert "Original content remains" in result.vision_text
    assert [name for name, _args in fake_transport.calls].count("get_window_state") == 3


def test_unverifiable_text_delivery_is_never_replayed_without_explicit_driver_rejection(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.document_value = "TARGET A"
    fake_transport.document_value = "TARGET A"
    fake_transport.scenario.delivery_profile = "web_targeted_unverifiable"
    _selected_caret_surface(fake_transport)
    target, observation = _notepad_target(service, fake_transport)

    service.act(
        "type",
        target,
        OWNER,
        element_token=observation.elements[0].token,
        text="\nVERIFIED A",
    )

    assert [name for name, _args in fake_transport.calls].count("type_text") == 1
    assert fake_transport.document_value == "TARGET A\nVERIFIED A"
    assert service.computer_use_completion_blocked(OWNER) is True


def test_foreground_focus_refusal_changes_no_text_and_has_no_further_fallback(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.document_value = "original"
    fake_transport.document_value = "original"
    fake_transport.scenario.background_unavailable_tools = frozenset({"type_text"})
    fake_transport.scenario.delivery_profile = "focus_refused"
    _selected_caret_surface(fake_transport)
    target, observation = _notepad_target(service, fake_transport)

    with pytest.raises(ComputerUseError) as refused:
        service.act(
            "type",
            target,
            OWNER,
            element_token=observation.elements[0].token,
            text=" must not appear",
            approval_mode="allow_all",
        )

    assert refused.value.code == "focus_refused"
    assert fake_transport.document_value == "original"
    assert [name for name, _args in fake_transport.calls].count("type_text") == 2
    assert all(
        name not in {"bring_to_front", "click", "press_key", "hotkey"}
        for name, _args in fake_transport.calls
    )
    assert service.computer_use_completion_blocked(OWNER) is False


def test_type_rejects_horizontal_tabs_before_approval_mutation_or_invalidation(
    fake_client,
    fake_transport,
) -> None:
    approvals: list[dict] = []
    service = ComputerUseService(
        client_factory=lambda: fake_client,
        approval_callback=lambda payload: approvals.append(payload) or True,
    )
    target, observation = _notepad_target(service, fake_transport)
    approvals.clear()
    calls_before = len(fake_transport.calls)

    with pytest.raises(ComputerUseError) as rejected:
        service.act(
            "type",
            target,
            OWNER,
            element_token=observation.elements[2].token,
            text="first\tsecond\nthird\tfourth",
            expected_effect="Submit structured content",
        )

    assert rejected.value.code == "invalid_input"
    assert "literal caret insertion" in str(rejected.value)
    assert "clipboard" in str(rejected.value).casefold()
    assert approvals == []
    assert fake_transport.calls[calls_before:] == []
    assert service.current_observation(target) is observation


def test_type_preserves_legitimate_multiline_caret_insertion(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.document_value = "original"
    fake_transport.document_value = "original"
    _selected_caret_surface(fake_transport)
    target, observation = _notepad_target(service, fake_transport)

    service.act(
        "type",
        target,
        OWNER,
        element_token=observation.elements[0].token,
        text="\nline two\nline three",
    )

    assert fake_transport.document_value == "original\nline two\nline three"
    type_call = [args for name, args in fake_transport.calls if name == "type_text"][-1]
    assert type_call["element_token"] != observation.elements[0].token


def test_tokenless_type_preserves_current_caret_insertion_without_a_token(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.document_value = "current caret"
    fake_transport.document_value = "current caret"
    target, _observation = _notepad_target(service, fake_transport)

    service.act("type", target, OWNER, text=" insertion")

    type_call = [args for name, args in fake_transport.calls if name == "type_text"][-1]
    assert "element_token" not in type_call
    assert "element_index" not in type_call
    assert fake_transport.document_value == "current caret insertion"


def test_type_token_accepts_unselected_editable_combo_and_cannot_touch_decoy(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.semantic_elements = (
        {
            "role": "TextField",
            "label": "Decoy Field",
            "value": "decoy stays",
            "enabled": True,
            "selected": False,
            "frame": {"x": 10, "y": 10, "w": 400, "h": 100},
        },
        {
            "role": "ComboBox",
            "label": "Native Editor",
            "value": "query",
            "enabled": True,
            "editable": True,
            "selected": False,
            "frame": {"x": 10, "y": 130, "w": 400, "h": 100},
        },
    )
    fake_transport.scenario.rotate_element_tokens = True
    target, observation = _notepad_target(service, fake_transport)
    service.act(
        "type",
        target,
        OWNER,
        element_token=observation.elements[1].token,
        text=" text",
    )

    calls = [args for name, args in fake_transport.calls if name == "type_text"]
    assert len(calls) == 1
    assert calls[0]["element_token"] != observation.elements[1].token
    assert fake_transport.value_for_label("Native Editor") == "query text"
    assert fake_transport.value_for_label("Decoy Field") == "decoy stays"
    assert all(
        name not in {"click", "bring_to_front", "press_key", "hotkey"}
        for name, _args in fake_transport.calls
    )


def test_type_token_does_not_require_selection_on_fresh_editable_target(
    service,
    fake_transport,
) -> None:
    selected = (
        {
            "role": "Edit",
            "label": "Editor",
            "enabled": True,
            "selected": True,
            "frame": {"x": 10, "y": 10, "w": 400, "h": 200},
        },
    )
    unselected = ({**selected[0], "selected": False},)
    fake_transport.scenario.semantic_snapshots = (selected, unselected)
    fake_transport.scenario.rotate_element_tokens = True
    target, observation = _notepad_target(service, fake_transport)
    calls_before = len(fake_transport.calls)

    service.act(
        "type",
        target,
        OWNER,
        element_token=observation.elements[0].token,
        text="dispatch exactly here",
    )

    calls = fake_transport.calls[calls_before:]
    assert [name for name, _args in calls] == ["get_window_state", "type_text"]
    assert calls[-1][1]["element_token"] != observation.elements[0].token


@pytest.mark.parametrize("role", ["Cell", "DataItem", "GridCell", "TableCell"])
def test_type_token_rejects_selected_document_value_roles(
    service,
    fake_transport,
    role: str,
) -> None:
    fake_transport.scenario.semantic_elements = (
        {
            "role": role,
            "label": "Document item",
            "enabled": True,
            "selected": True,
            "frame": {"x": 10, "y": 10, "w": 120, "h": 40},
        },
    )
    fake_transport.scenario.rotate_element_tokens = True
    target, observation = _notepad_target(service, fake_transport)
    calls_before = len(fake_transport.calls)

    with pytest.raises(ComputerUseError) as rejected:
        service.act(
            "type",
            target,
            OWNER,
            element_token=observation.elements[0].token,
            text="must use exact replacement for a complete value",
        )

    assert rejected.value.code == "invalid_input"
    assert [name for name, _args in fake_transport.calls[calls_before:]] == [
        "get_window_state"
    ]


@pytest.mark.parametrize(
    "fresh_element",
    [
        {"role": "Edit", "label": "Native Editor", "enabled": False},
        {
            "role": "Edit",
            "label": "Native Editor",
            "enabled": True,
            "read_only": True,
        },
        {"role": "Document", "label": "Native Editor", "enabled": True},
        {"role": "Group", "label": "Native Editor", "enabled": True},
        {"role": "Button", "label": "Native Editor", "enabled": True},
        {
            "role": "ComboBox",
            "label": "Native Editor",
            "enabled": True,
            "editable": False,
        },
    ],
)
def test_type_token_rejects_disabled_read_only_container_and_noneditable_targets(
    service,
    fake_transport,
    fresh_element: dict,
) -> None:
    original = {
        **fresh_element,
        "frame": {"x": 10, "y": 10, "w": 400, "h": 100},
    }
    fresh = dict(original)
    fake_transport.scenario.semantic_snapshots = ((original,), (fresh,))
    fake_transport.scenario.rotate_element_tokens = True
    target, observation = _notepad_target(service, fake_transport)
    calls_before = len(fake_transport.calls)

    with pytest.raises(ComputerUseError) as rejected:
        service.act(
            "type",
            target,
            OWNER,
            element_token=observation.elements[0].token,
            text="must not dispatch",
        )

    assert rejected.value.code == "invalid_input"
    assert [name for name, _args in fake_transport.calls[calls_before:]] == [
        "get_window_state"
    ]


def test_type_token_rejects_ambiguous_fresh_exact_match_without_dispatch(
    service,
    fake_transport,
) -> None:
    editable = {
        "role": "Edit",
        "label": "Native Editor",
        "enabled": True,
        "frame": {"x": 10, "y": 10, "w": 400, "h": 100},
    }
    fake_transport.scenario.semantic_snapshots = (
        (editable,),
        (editable, dict(editable)),
    )
    fake_transport.scenario.rotate_element_tokens = True
    target, observation = _notepad_target(service, fake_transport)
    calls_before = len(fake_transport.calls)

    with pytest.raises(ComputerUseError) as rejected:
        service.act(
            "type",
            target,
            OWNER,
            element_token=observation.elements[0].token,
            text="must not dispatch",
        )

    assert rejected.value.code == "ambiguous_target"
    assert [name for name, _args in fake_transport.calls[calls_before:]] == [
        "get_window_state"
    ]
