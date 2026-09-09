"""Bounded real adapter queues retain ownership when a renderer is slow."""
from __future__ import annotations

import threading
import queue as queue_module

import pytest

from tests.contracts.client_platform.test_headless_lifecycle import platform  # noqa: F401


def test_real_legacy_adapter_cancellation_unblocks_full_queue(platform, monkeypatch):
    from row_bot.ui.legacy_adapter import generation
    monkeypatch.setattr(generation, "client_platform_service", platform)
    config = {"configurable": {"thread_id": "conversation-a"}}
    handle = platform.admit_execution("conversation-a", config, text="Synthetic")
    queue = generation.LegacyEventQueue(handle)
    filled, cancelled, release = (threading.Event() for _ in range(3))
    def producer():
        try:
            for _ in range(224):
                queue.put(("token", "x"))
            filled.set()
            queue.put(("token", "blocked"))
        except InterruptedError:
            cancelled.set()
            assert release.wait(10)
        finally:
            platform.finish_execution(handle, "stopped")
    platform.registry.launch(handle, producer)
    try:
        assert filled.wait(10)
        assert queue.qsize() == 224 and queue.byte_size <= 1024 * 1024
        platform.registry.stop("conversation-a")
        assert cancelled.wait(10)
        assert not handle.producer_done.is_set()
        assert handle in platform.registry.active()
        queue.put(("error", "Synthetic cancellation"))
        assert queue.get_nowait() == ("token", "x")  # Control does not overtake accepted data.
    finally:
        queue.close_consumer()
        release.set()
        assert handle.producer_done.wait(10)


def test_oversized_tool_event_uses_owned_spool_and_exact_materialization(platform, monkeypatch):
    from row_bot.ui.legacy_adapter import generation
    monkeypatch.setattr(generation, "client_platform_service", platform)
    handle = platform.admit_execution("conversation-a", {"configurable": {}}, text="Synthetic")
    queue = generation.LegacyEventQueue(handle)
    event = ("tool_done", {"tool_call_id": "native-call", "message_id": "native-tool", "content": "Synthetic" * 200000})
    try:
        queue.put(event)
        assert queue.byte_size < 1024
        queued = queue.get_nowait()
        assert queued[0] == "_platform_spool"
        assert queue.materialize(queued) == event
        assert not queue._spools
    finally:
        queue.close_consumer()
        platform.finish_execution(handle, "interrupted")


def test_spool_capacity_and_failed_enqueue_release_exact_files(platform, monkeypatch):
    from row_bot.ui.legacy_adapter import generation
    from row_bot import threads
    monkeypatch.setattr(generation, "client_platform_service", platform)
    handle = platform.admit_execution("conversation-a", {"configurable": {}}, text="Synthetic")
    queue = generation.LegacyEventQueue(handle)
    event = ("tool_done", {"content": "Synthetic" * 200000})
    before = generation._DISK_BYTES
    try:
        monkeypatch.setattr(queue, "MAX_SPOOL_BYTES", 1024 * 1024)
        with pytest.raises(ValueError, match="legacy_spool_storage_limit"):
            queue.put(event)
        assert generation._DISK_BYTES == before and not queue._spools
        monkeypatch.setattr(queue, "MAX_SPOOL_BYTES", 4 * 1024 * 1024)
        for _ in range(queue.maxsize):
            queue.put(("done", ""), block=False)
        with pytest.raises(queue_module.Full):
            queue.put(event, block=False)
        assert generation._DISK_BYTES == before and not queue._spools
        assert not list((threads._MEDIA_DIR / "conversation-a").glob("runtime-event-*.json"))
    finally:
        queue.close_consumer()
        platform.finish_execution(handle, "interrupted")


def test_adapter_pending_deadline_cancels_owner_and_preserves_accepted_fifo(platform, monkeypatch):
    from row_bot.ui.legacy_adapter import generation
    monkeypatch.setattr(generation, "client_platform_service", platform)
    handle = platform.admit_execution("conversation-a", {"configurable": {}}, text="Synthetic")
    queue = generation.LegacyEventQueue(handle)
    monkeypatch.setattr(queue, "MAX_PENDING_SECONDS", 0)
    try:
        for _ in range(224):
            queue.put(("token", "accepted"))
        with pytest.raises(InterruptedError, match="legacy_queue_deadline"):
            queue.put(("token", "pending"))
        assert handle.cancel_scope.is_cancelled() and handle.status == "stopping"
        assert not handle.producer_done.is_set()
        assert queue.qsize() == 224 and queue.get_nowait() == ("token", "accepted")
    finally:
        queue.close_consumer()
        platform.finish_execution(handle, "stopped")
