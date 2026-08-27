from __future__ import annotations

import pytest

from row_bot.computer_use.client import CuaClient
from row_bot.computer_use.service import (
    ActionReceipt,
    ComputerUseError,
    ComputerUseService,
    LeaseOwner,
    Observation,
)


OWNER = LeaseOwner("text-thread", "text-generation", "text-task")


def _target(
    service: ComputerUseService,
    fake_transport,
    *elements: dict,
) -> tuple[str, Observation]:
    fake_transport.scenario.apps = (
        {"name": "Document Editor.exe", "pid": 5404, "running": True, "active": True},
    )
    fake_transport.scenario.windows = (
        {
            "window_id": 404,
            "pid": 5404,
            "app_name": "Document Editor.exe",
            "title": "Document",
            "bounds": {"x": 10, "y": 10, "width": 800, "height": 600},
            "is_on_screen": True,
        },
    )
    fake_transport.scenario.capture_pid = 5404
    fake_transport.scenario.capture_window_id = 404
    fake_transport.scenario.semantic_elements = tuple(elements)
    observed = service.capture(owner=OWNER, app="Document Editor")
    return observed.target.target_id, observed


def test_current_token_is_dispatched_directly_without_hidden_capture(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.rotate_element_tokens = True
    target, observed = _target(
        service,
        fake_transport,
        {"role": "TextField", "label": "Editor", "value": "original"},
    )
    calls_before = len(fake_transport.calls)

    result = service.act(
        "type",
        target,
        OWNER,
        element_token=observed.elements[0].token,
        text=" addition",
    )

    assert isinstance(result, ActionReceipt)
    assert [name for name, _args in fake_transport.calls[calls_before:]] == [
        "type_text"
    ]
    sent = fake_transport.calls[-1][1]
    assert sent["element_token"] == observed.elements[0].token
    assert fake_transport.value_for_label("Editor") == "original addition"


def test_explicit_post_action_vision_uses_one_later_capture(
    fake_client: CuaClient,
    fake_transport,
) -> None:
    class Vision:
        def __init__(self) -> None:
            self.questions: list[str] = []

        def analyze(self, _image: bytes, question: str) -> str:
            self.questions.append(question)
            return "visible result"

    vision = Vision()
    service = ComputerUseService(
        client_factory=lambda: fake_client,
        approval_callback=lambda _payload: True,
        vision_service=vision,
    )
    target, observed = _target(
        service,
        fake_transport,
        {"role": "TextField", "label": "Editor", "value": ""},
    )
    calls_before = len(fake_transport.calls)

    result = service.act(
        "type",
        target,
        OWNER,
        element_token=observed.elements[0].token,
        text="value",
        capture_after=True,
        visual_question="What changed visually?",
    )

    assert isinstance(result, Observation)
    assert [name for name, _args in fake_transport.calls[calls_before:]] == [
        "type_text",
        "get_window_state",
    ]
    assert vision.questions == ["What changed visually?"]


def test_unverifiable_delivery_is_not_replayed_and_creates_no_global_latch(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.delivery_profile = "web_targeted_unverifiable"
    target, observed = _target(
        service,
        fake_transport,
        {"role": "TextField", "label": "Editor", "value": ""},
    )

    result = service.act(
        "type",
        target,
        OWNER,
        element_token=observed.elements[0].token,
        text="uncertain",
    )
    followup = service.act("key", target, OWNER, keys="enter")

    assert result.action_dispatched is True
    assert result.effect_verified is False
    assert followup.action_dispatched is True
    assert [name for name, _args in fake_transport.calls].count("type_text") == 1
    assert not hasattr(service, "_pending_mutation")
    assert not hasattr(service, "_completion_ledger")


def test_background_refusal_allows_one_foreground_type_without_focus(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.background_unavailable_tools = frozenset({"type_text"})
    target, observed = _target(
        service,
        fake_transport,
        {"role": "ComboBox", "label": "Editable Combo", "value": ""},
    )
    calls_before = len(fake_transport.calls)

    service.act(
        "type",
        target,
        OWNER,
        element_token=observed.elements[0].token,
        text="query",
        approval_mode="allow_all",
    )

    calls = fake_transport.calls[calls_before:]
    assert [name for name, _args in calls] == ["type_text", "type_text"]
    assert calls[0][1]["element_token"] == observed.elements[0].token
    assert calls[1][1]["element_token"] == observed.elements[0].token
    assert calls[1][1]["delivery_mode"] == "foreground"


def test_foreground_focus_refusal_changes_no_text_and_has_no_third_rung(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.background_unavailable_tools = frozenset({"type_text"})
    fake_transport.scenario.delivery_profile = "focus_refused"
    target, observed = _target(
        service,
        fake_transport,
        {"role": "TextField", "label": "Editor", "value": "original"},
    )
    calls_before = len(fake_transport.calls)

    with pytest.raises(ComputerUseError) as refused:
        service.act(
            "type",
            target,
            OWNER,
            element_token=observed.elements[0].token,
            text=" must not appear",
            approval_mode="allow_all",
        )

    assert refused.value.code == "focus_refused"
    assert [name for name, _args in fake_transport.calls[calls_before:]] == [
        "type_text",
        "type_text",
    ]
    assert fake_transport.value_for_label("Editor") == "original"


def test_tokenless_type_is_one_literal_current_caret_action(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.document_value = "current caret"
    fake_transport.document_value = "current caret"
    target, _observed = _target(
        service,
        fake_transport,
        {"role": "TextField", "label": "Editor"},
    )
    calls_before = len(fake_transport.calls)

    service.act("type", target, OWNER, text="\tline two\nline three")

    assert [name for name, _args in fake_transport.calls[calls_before:]] == [
        "type_text"
    ]
    assert fake_transport.document_value == "current caret\tline two\nline three"
    assert "element_token" not in fake_transport.calls[-1][1]


def test_combo_type_changes_only_the_supplied_token_not_a_decoy(
    service,
    fake_transport,
) -> None:
    target, observed = _target(
        service,
        fake_transport,
        {"role": "TextField", "label": "Decoy Field", "value": "decoy"},
        {"role": "ComboBox", "label": "Editable Combo", "value": "query"},
    )

    service.act(
        "type",
        target,
        OWNER,
        element_token=observed.elements[1].token,
        text=" result",
    )

    assert fake_transport.value_for_label("Editable Combo") == "query result"
    assert fake_transport.value_for_label("Decoy Field") == "decoy"


@pytest.mark.parametrize("role", ["Cell", "DataItem", "GridCell", "TableCell"])
def test_document_value_roles_reach_cua(role, service, fake_transport) -> None:
    target, observed = _target(
        service,
        fake_transport,
        {"role": role, "label": "Named Cell", "value": ""},
    )
    calls_before = len(fake_transport.calls)

    service.act(
        "type",
        target,
        OWNER,
        element_token=observed.elements[0].token,
        text="value",
    )

    assert [name for name, _args in fake_transport.calls[calls_before:]] == [
        "type_text"
    ]


@pytest.mark.parametrize(
    "element",
    [
        {"role": "TextField", "label": "Disabled", "enabled": False},
        {"role": "TextField", "label": "Read only", "read_only": True},
        {"role": "Pane", "label": "Structural"},
        {"role": "Window", "label": "Structural"},
        {"role": "StaticText", "label": "Structural"},
    ],
)
def test_explicit_local_blocks_dispatch_zero_mutations(
    element,
    service,
    fake_transport,
) -> None:
    target, observed = _target(service, fake_transport, element)
    calls_before = len(fake_transport.calls)

    with pytest.raises(ComputerUseError) as rejected:
        service.act(
            "type",
            target,
            OWNER,
            element_token=observed.elements[0].token,
            text="blocked",
        )

    assert rejected.value.code == "invalid_input"
    assert fake_transport.calls[calls_before:] == []
