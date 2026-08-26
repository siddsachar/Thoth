from __future__ import annotations

import threading

from row_bot.computer_use.service import LeaseOwner


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
