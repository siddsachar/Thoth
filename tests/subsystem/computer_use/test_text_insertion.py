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


def test_type_token_validates_target_but_never_uses_whole_value_setvalue(
    service,
    fake_transport,
) -> None:
    original = "TARGET A"
    fake_transport.scenario.document_value = original
    fake_transport.document_value = original
    fake_transport.scenario.background_unavailable_tools = frozenset({"type_text"})
    target, observation = _notepad_target(service, fake_transport)

    result = service.act(
        "type",
        target,
        OWNER,
        element_token=observation.elements[2].token,
        text="\nVERIFIED A",
        capture_after=True,
    )

    type_calls = [args for name, args in fake_transport.calls if name == "type_text"]
    assert len(type_calls) == 2
    assert all("element_token" not in args and "element_index" not in args for args in type_calls)
    assert "delivery_mode" not in type_calls[0]
    assert type_calls[1]["delivery_mode"] == "foreground"
    assert fake_transport.document_value == original + "\nVERIFIED A"
    assert isinstance(result, Observation)


def test_post_action_visual_question_runs_in_the_same_type_call(fake_client, fake_transport) -> None:
    vision = _Vision()
    service = ComputerUseService(
        client_factory=lambda: fake_client,
        approval_callback=lambda _payload: True,
        vision_service=vision,
    )
    fake_transport.scenario.document_value = "TARGET A"
    fake_transport.document_value = "TARGET A"
    target, observation = _notepad_target(service, fake_transport)

    result = service.act(
        "type",
        target,
        OWNER,
        element_token=observation.elements[2].token,
        text="\nVERIFIED A",
        capture_after=True,
        visual_question="Confirm the original visible content remains and the insertion is present.",
    )

    assert isinstance(result, Observation)
    assert len(vision.questions) == 1
    assert "Original content remains" in result.vision_text
    assert [name for name, _args in fake_transport.calls].count("get_window_state") == 2


def test_unverifiable_text_delivery_is_never_replayed_without_explicit_driver_rejection(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.document_value = "TARGET A"
    fake_transport.document_value = "TARGET A"
    fake_transport.scenario.effect = "unverifiable"
    target, observation = _notepad_target(service, fake_transport)

    service.act(
        "type",
        target,
        OWNER,
        element_token=observation.elements[2].token,
        text="\nVERIFIED A",
    )

    assert [name for name, _args in fake_transport.calls].count("type_text") == 1
    assert fake_transport.document_value == "TARGET A\nVERIFIED A"


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
    target, observation = _notepad_target(service, fake_transport)

    service.act(
        "type",
        target,
        OWNER,
        element_token=observation.elements[2].token,
        text="\nline two\nline three",
    )

    assert fake_transport.document_value == "original\nline two\nline three"
    type_call = [args for name, args in fake_transport.calls if name == "type_text"][-1]
    assert "element_token" not in type_call
