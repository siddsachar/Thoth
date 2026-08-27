from __future__ import annotations

import concurrent.futures
import threading

import pytest

from row_bot.computer_use.service import LeaseBusyError, LeaseOwner


OWNER = LeaseOwner("cancel-thread", "cancel-generation", "cancel-task")


def test_stop_ends_blocking_call_and_prevents_next_input(service, fake_transport) -> None:
    service.acquire(OWNER, validate_context=False)
    target_id = service.list_windows(OWNER, app="Calculator")[0]["target_id"]
    observation = service.capture(target_id, OWNER)
    fake_transport.block_action.set()
    finished = threading.Event()

    def _act() -> None:
        try:
            service.act("click", target_id, OWNER, element_token=observation.elements[0].token)
        except BaseException:
            pass
        finally:
            finished.set()

    worker = threading.Thread(target=_act)
    worker.start()
    while not any(name == "click" for name, _args in fake_transport.calls):
        worker.join(timeout=0.01)
    service.stop()
    worker.join(timeout=2)
    assert finished.is_set()
    click_index = next(i for i, (name, _args) in enumerate(fake_transport.calls) if name == "click")
    assert all(name != "click" for name, _args in fake_transport.calls[click_index + 1 :])


def test_stop_between_routine_keys_prevents_remaining_sequence(service, fake_transport) -> None:
    service.acquire(OWNER, validate_context=False)
    target_id = service.list_windows(OWNER, app="Calculator")[0]["target_id"]
    fake_transport.scenario.calculator_semantics = True
    service.capture(target_id, OWNER)
    fake_transport.block_action.set()
    finished = threading.Event()

    def _act() -> None:
        try:
            service.act_key_sequence(target_id, "7,*,8,=", OWNER)
        except BaseException:
            pass
        finally:
            finished.set()

    worker = threading.Thread(target=_act)
    worker.start()
    while not any(name == "click" for name, _args in fake_transport.calls):
        worker.join(timeout=0.01)
    service.stop()
    worker.join(timeout=2)

    assert finished.is_set()
    assert [name for name, _args in fake_transport.calls].count("click") == 1
    assert service.current_observation(target_id) is None


def test_stop_during_one_text_foreground_attempt_prevents_capture_or_further_input(
    service,
    fake_transport,
) -> None:
    service.acquire(OWNER, validate_context=False)
    target_id = service.list_windows(OWNER, app="Calculator")[0]["target_id"]
    service.capture(target_id, OWNER)
    fake_transport.scenario.background_unavailable_tools = frozenset({"type_text"})
    fake_transport.scenario.block_foreground = True
    fake_transport.block_action.set()
    finished = threading.Event()

    def _act() -> None:
        try:
            service.act("type", target_id, OWNER, text="one literal insertion")
        except BaseException:
            pass
        finally:
            finished.set()

    captures_before = [name for name, _args in fake_transport.calls].count("get_window_state")
    worker = threading.Thread(target=_act)
    worker.start()
    while len([name for name, _args in fake_transport.calls if name == "type_text"]) < 2:
        worker.join(timeout=0.01)
    service.stop()
    worker.join(timeout=2)

    assert finished.is_set()
    type_calls = [args for name, args in fake_transport.calls if name == "type_text"]
    assert len(type_calls) == 2
    assert "delivery_mode" not in type_calls[0]
    assert type_calls[1]["delivery_mode"] == "foreground"
    assert [name for name, _args in fake_transport.calls].count("get_window_state") == captures_before


def test_cancellation_after_background_scroll_refusal_prevents_foreground_delivery(
    service,
    fake_transport,
    monkeypatch,
) -> None:
    service.acquire(OWNER, validate_context=False)
    target_id = service.list_windows(OWNER, app="Calculator")[0]["target_id"]
    service.capture(target_id, OWNER)
    fake_transport.scenario.background_unavailable_tools = frozenset({"scroll"})
    calls_before = len(fake_transport.calls)
    require_owner = service._require_existing_owner

    def cancel_before_foreground(owner=None):
        service._cancel.set()
        return require_owner(owner)

    monkeypatch.setattr(service, "_require_existing_owner", cancel_before_foreground)

    with pytest.raises(concurrent.futures.CancelledError):
        service.act(
            "scroll",
            target_id,
            OWNER,
            direction="down",
            amount=2,
        )

    assert fake_transport.calls[calls_before:] == [
        (
            "scroll",
            {
                "pid": 4242,
                "window_id": 101,
                "direction": "down",
                "amount": 2,
                "session": "row-bot-test-session",
            },
        )
    ]


def test_owner_loss_after_background_scroll_refusal_prevents_foreground_delivery(
    service,
    fake_transport,
    monkeypatch,
) -> None:
    service.acquire(OWNER, validate_context=False)
    target_id = service.list_windows(OWNER, app="Calculator")[0]["target_id"]
    service.capture(target_id, OWNER)
    fake_transport.scenario.background_unavailable_tools = frozenset({"scroll"})
    calls_before = len(fake_transport.calls)
    require_owner = service._require_existing_owner
    replacement = LeaseOwner("replacement-thread", "replacement-generation", "replacement-task")

    def replace_owner_before_foreground(owner=None):
        with service._lock:
            service._owner = replacement
        return require_owner(owner)

    monkeypatch.setattr(service, "_require_existing_owner", replace_owner_before_foreground)

    with pytest.raises(LeaseBusyError):
        service.act(
            "scroll",
            target_id,
            OWNER,
            direction="up",
            amount=4,
        )

    assert fake_transport.calls[calls_before:] == [
        (
            "scroll",
            {
                "pid": 4242,
                "window_id": 101,
                "direction": "up",
                "amount": 4,
                "session": "row-bot-test-session",
            },
        )
    ]
