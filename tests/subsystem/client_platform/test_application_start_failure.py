"""Actual application and legacy admissions settle a proven failed OS start."""
from __future__ import annotations

import threading

import pytest

from tests.contracts.client_platform.test_headless_lifecycle import platform, submit  # noqa: F401
from tests.helpers.client_platform_fakes import ScriptedAgentStream


def _failed_start(_worker):
    raise RuntimeError("cannot start new thread")


def test_headless_failed_start_settles_durable_admission_and_exact_retry(platform, monkeypatch):
    from row_bot.application.client_platform import ClientPlatformError
    fake = ScriptedAgentStream((("done", "Must not dispatch"),))
    monkeypatch.setattr(threading.Thread, "start", _failed_start)
    for _ in range(2):
        with pytest.raises(ClientPlatformError, match="generation_failed"):
            submit(platform, fake, "failed-start")
    assert fake.calls == []
    assert not platform.registry.active()
    assert platform.snapshot("conversation-a")["generation"]["status"] == "interrupted"
    from row_bot.runtime import admissions
    with admissions.transaction() as connection:
        rows = connection.execute("SELECT state FROM generation_passes WHERE conversation_id='conversation-a'").fetchall()
    assert len(rows) == 1 and rows[0][0] == "interrupted"


def test_legacy_failed_start_keeps_error_and_terminal_for_its_consumer(platform, monkeypatch):
    from row_bot.ui.legacy_adapter import generation
    monkeypatch.setattr(generation, "client_platform_service", platform)
    handle = platform.admit_execution("conversation-a", {"configurable": {}}, text="Synthetic user input")
    queue = generation.LegacyEventQueue(handle)
    monkeypatch.setattr(threading.Thread, "start", _failed_start)
    result = generation.launch_legacy_generation(handle, queue, lambda: pytest.fail("Failed legacy start dispatched"))
    try:
        assert result is None
        assert handle.producer_done.is_set()
        assert handle.status == "interrupted"
        assert queue.get_nowait() == ("error", "The generation could not start.")
        assert queue.get_nowait() is None
        assert not platform.registry.active()
    finally:
        queue.close_consumer()


@pytest.mark.parametrize("resume", [False, True])
def test_legacy_resource_entry_failure_is_visible_and_terminal(platform, monkeypatch, resume):
    from row_bot.ui.legacy_adapter import generation
    from row_bot.runtime import admissions
    from row_bot import threads
    import sqlite3
    monkeypatch.setattr(generation, "client_platform_service", platform)
    handle = platform.admit_execution("conversation-a", {"configurable": {}},
                                      text=None if resume else "Synthetic user input")
    queue = generation.LegacyEventQueue(handle)
    with sqlite3.connect(threads.DB_PATH) as connection:
        connection.execute("UPDATE thread_meta SET resource_bindings_json='{' WHERE thread_id='conversation-a'")
    worker = generation.launch_legacy_generation(handle, queue, lambda: pytest.fail("Rejected resources entered the legacy provider"))
    try:
        worker.join(5)
        assert not worker.is_alive()
        assert handle.producer_done.is_set() and handle.status == "interrupted"
        assert queue.get_nowait() == ("error", "The generation could not run.")
        assert queue.get_nowait() is None
        assert queue.empty()
        assert not platform.registry.active()
        with admissions.transaction() as connection:
            row = connection.execute("SELECT state FROM generation_passes WHERE pass_id=?", (handle.pass_id,)).fetchone()
        assert row[0] == "interrupted"
    finally:
        queue.close_consumer()
