from __future__ import annotations

import queue
import threading

import pytest

from row_bot.ui.legacy_adapter.event_queue import LegacyEventQueue, event_size


def test_data_saturation_reserves_fifo_control_and_terminal_capacity():
    buffer = LegacyEventQueue(4, max_bytes=1024, reserved_items=2, reserved_bytes=128)
    buffer.put(("token", "first"))
    buffer.put(("token", "second"))
    with pytest.raises(queue.Full):
        buffer.put_nowait(("token", "third"))
    buffer.put(("interrupt", {"id": "approval"}))
    buffer.put(None)
    assert [buffer.get_nowait() for _ in range(4)] == [
        ("token", "first"), ("token", "second"), ("interrupt", {"id": "approval"}), None]
    assert buffer.byte_size == 0
    with pytest.raises(queue.Empty):
        buffer.get_nowait()


def test_byte_saturation_bounds_multibyte_data_and_retains_control():
    buffer = LegacyEventQueue(8, max_bytes=512, reserved_items=2, reserved_bytes=128)
    data = ("token", "🙂" * 75)
    buffer.put(data)
    with pytest.raises(queue.Full):
        buffer.put_nowait(data)
    buffer.put(("done", "finished"))
    assert buffer.byte_size <= 512
    assert buffer.get_nowait() == data
    assert buffer.get_nowait() == ("done", "finished")


def test_cancel_unblocks_pending_data_without_discarding_accepted_events():
    buffer = LegacyEventQueue(2, max_bytes=512, reserved_items=1, reserved_bytes=128)
    buffer.put(("token", "accepted"))
    entered = threading.Event()
    result = []
    original_wait = buffer.not_full.wait
    def wait(timeout=None):
        entered.set()
        return original_wait(timeout)
    buffer.not_full.wait = wait
    def produce():
        try:
            buffer.put(("token", "blocked"))
        except InterruptedError:
            result.append("cancelled")
    worker = threading.Thread(target=produce)
    worker.start()
    assert entered.wait(2)
    buffer.cancel_pending_puts()
    worker.join(2)
    assert result == ["cancelled"]
    buffer.put(None)
    assert buffer.get_nowait() == ("token", "accepted")
    assert buffer.get_nowait() is None


def test_oversized_event_is_rejected_before_enqueue_and_cyclic_input_fails():
    buffer = LegacyEventQueue(4, max_bytes=512, reserved_items=1, reserved_bytes=128)
    with pytest.raises(ValueError, match="legacy_event_too_large"):
        buffer.put(("tool_result", "x" * 10000))
    assert buffer.empty() and buffer.byte_size == 0
    cycle = []
    cycle.append(cycle)
    with pytest.raises(ValueError, match="legacy_event_invalid"):
        event_size(cycle)
