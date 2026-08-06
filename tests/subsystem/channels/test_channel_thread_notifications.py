from __future__ import annotations

import importlib
import sys

import pytest

from tests.fixtures.channels import FakeChannel


pytestmark = pytest.mark.subsystem


@pytest.fixture(autouse=True)
def _reset_channel_registry_after_test():
    yield
    try:
        from row_bot.channels import registry

        registry._reset()
    except Exception:
        pass


def _fresh_modules(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "row-bot-data"))
    for name in (
        "row_bot.tasks",
        "row_bot.threads",
        "row_bot.agent_runs",
        "row_bot.channels.registry",
        "row_bot.channels.thread_notifications",
        "row_bot.agent_run_messages",
    ):
        sys.modules.pop(name, None)

    tasks = importlib.import_module("row_bot.tasks")
    threads = importlib.import_module("row_bot.threads")
    agent_runs = importlib.import_module("row_bot.agent_runs")
    registry = importlib.import_module("row_bot.channels.registry")
    thread_notifications = importlib.import_module("row_bot.channels.thread_notifications")
    registry._reset()
    return tasks, threads, agent_runs, registry, thread_notifications


def test_child_agent_terminal_notification_delivers_once_and_persists_checkpoint(tmp_path, monkeypatch):
    tasks, threads, agent_runs, registry, thread_notifications = _fresh_modules(tmp_path, monkeypatch)
    channel = FakeChannel(name="fake")
    channel._running = True
    registry.register(channel)
    tasks.record_thread_channel_ref(
        "parent-thread",
        channel="fake",
        target="conversation-1",
        external_conversation_id="conversation-1",
    )
    agent_runs.create_agent_run(
        run_id="child-run",
        kind="subagent",
        status="running",
        parent_thread_id="parent-thread",
        display_name="Checkpoint child",
    )

    agent_runs.finish_agent_run("child-run", "completed", summary="child result")

    expected = "Done. Checkpoint child completed.\n\nchild result"
    assert [(msg.target, msg.text) for msg in channel.messages] == [("conversation-1", expected)]
    notification = tasks.get_channel_thread_notification("agent_run_terminal:child-run")
    assert notification and notification["status"] == "delivered"

    checkpoint_messages = threads.get_latest_checkpoint_messages("parent-thread")
    ai_messages = [msg for msg in checkpoint_messages if getattr(msg, "type", "") == "ai"]
    assert [getattr(msg, "content", "") for msg in ai_messages] == [expected]
    metadata = ai_messages[0].additional_kwargs["row_bot_ui"]
    assert metadata["agent_completion_for"] == "child-run"
    assert metadata["channel_notification_key"] == "agent_run_terminal:child-run"

    assert thread_notifications.notify_agent_run_terminal("child-run") is True
    assert len(channel.messages) == 1
    assert len([
        msg for msg in threads.get_latest_checkpoint_messages("parent-thread")
        if getattr(msg, "type", "") == "ai"
    ]) == 1


def test_unified_parent_final_uses_fake_channel_outbox_exactly_once(
    tmp_path,
    monkeypatch,
):
    tasks, threads, _agent_runs, registry, notifications = _fresh_modules(
        tmp_path,
        monkeypatch,
    )
    channel = FakeChannel(name="fake")
    channel._running = True
    registry.register(channel)
    tasks.record_thread_channel_ref(
        "parent-thread",
        channel="fake",
        target="conversation-1",
        external_conversation_id="conversation-1",
    )
    key = "orchestration:one:parent_final:2"
    from langchain_core.messages import AIMessage

    threads.append_checkpoint_messages(
        "parent-thread",
        [
            AIMessage(
                content="The original parent produced this final.",
                id="parent-checkpoint-final",
            )
        ],
    )

    for _ in range(2):
        assert notifications.deliver_parent_thread_notification(
            key=key,
            thread_id="parent-thread",
            kind="orchestration_final",
            text="The original parent produced this final.",
            ui_metadata={
                "orchestration_id": "one",
                "orchestration_message_kind": "final",
            },
            checkpoint_authoritative=True,
        )

    assert [(msg.target, msg.text) for msg in channel.messages] == [
        ("conversation-1", "The original parent produced this final.")
    ]
    checkpoint = [
        message
        for message in threads.get_latest_checkpoint_messages("parent-thread")
        if getattr(message, "type", "") == "ai"
    ]
    assert len(checkpoint) == 1
    assert checkpoint[0].id == "parent-checkpoint-final"
    assert checkpoint[0].additional_kwargs == {}
    record = tasks.get_channel_thread_notification(key)
    assert record and record["status"] == "delivered"


def test_unified_parent_delivery_never_invents_missing_checkpoint_output(
    tmp_path,
    monkeypatch,
):
    tasks, threads, _agent_runs, registry, notifications = _fresh_modules(
        tmp_path,
        monkeypatch,
    )
    channel = FakeChannel(name="fake")
    channel._running = True
    registry.register(channel)
    tasks.record_thread_channel_ref(
        "parent-thread",
        channel="fake",
        target="conversation-1",
        external_conversation_id="conversation-1",
    )

    assert notifications.deliver_parent_thread_notification(
        key="orchestration:missing:parent_final:1",
        thread_id="parent-thread",
        kind="orchestration_final",
        text="This output is not checkpointed.",
        checkpoint_authoritative=True,
    ) is False

    assert channel.messages == []
    assert threads.get_latest_checkpoint_messages("parent-thread") == []
    assert tasks.get_channel_thread_notification(
        "orchestration:missing:parent_final:1"
    ) is None


def test_pending_terminal_notification_retries_after_channel_starts(tmp_path, monkeypatch):
    tasks, _threads, agent_runs, registry, thread_notifications = _fresh_modules(tmp_path, monkeypatch)
    channel = FakeChannel(name="fake")
    registry.register(channel)
    tasks.record_thread_channel_ref(
        "parent-thread",
        channel="fake",
        target="conversation-1",
        external_conversation_id="conversation-1",
    )
    agent_runs.create_agent_run(
        run_id="child-run",
        kind="subagent",
        status="running",
        parent_thread_id="parent-thread",
        display_name="Retry child",
    )

    agent_runs.finish_agent_run("child-run", "completed", summary="retry result")

    assert channel.messages == []
    notification = tasks.get_channel_thread_notification("agent_run_terminal:child-run")
    assert notification and notification["status"] == "failed"

    channel._running = True
    assert thread_notifications.reconcile_pending_channel_notifications() == 1

    assert [(msg.target, msg.text) for msg in channel.messages] == [
        ("conversation-1", "Done. Retry child completed.\n\nretry result")
    ]
    notification = tasks.get_channel_thread_notification("agent_run_terminal:child-run")
    assert notification and notification["status"] == "delivered"


def test_wait_mode_child_result_is_not_announced_again(tmp_path, monkeypatch):
    tasks, threads, agent_runs, registry, _thread_notifications = _fresh_modules(tmp_path, monkeypatch)
    from langchain_core.messages import ToolMessage

    channel = FakeChannel(name="fake")
    channel._running = True
    registry.register(channel)
    tasks.record_thread_channel_ref(
        "parent-thread",
        channel="fake",
        target="conversation-1",
        external_conversation_id="conversation-1",
    )
    threads.append_checkpoint_messages(
        "parent-thread",
        [
            ToolMessage(
                name="delegate_work",
                tool_call_id="tool-1",
                content='{"ok": true, "message": "Child Agent completed.", "run": {"id": "wait-child"}}',
            )
        ],
    )
    agent_runs.create_agent_run(
        run_id="wait-child",
        kind="subagent",
        status="running",
        parent_thread_id="parent-thread",
        display_name="Wait child",
    )

    agent_runs.finish_agent_run("wait-child", "completed", summary="already summarized")

    assert channel.messages == []
    assert tasks.get_channel_thread_notification("agent_run_terminal:wait-child") is None


def test_goal_terminal_notification_is_compact_and_deduped(tmp_path, monkeypatch):
    tasks, threads, agent_runs, registry, thread_notifications = _fresh_modules(tmp_path, monkeypatch)
    channel = FakeChannel(name="fake")
    channel._running = True
    registry.register(channel)
    tasks.record_thread_channel_ref(
        "goal-thread",
        channel="fake",
        target="conversation-1",
        external_conversation_id="conversation-1",
    )
    agent_runs.create_agent_run(
        run_id="goal-goal-1",
        kind="goal",
        status="running",
        parent_thread_id="goal-thread",
        thread_id="goal-thread",
        goal_id="goal-1",
        display_name="Goal: ship channel parity",
        prompt="ship channel parity",
    )

    agent_runs.finish_agent_run("goal-goal-1", "completed", summary="The final answer was already sent.")

    assert channel.messages == []
    assert tasks.get_channel_thread_notification("goal_terminal:goal-1:completed") is None
    assert thread_notifications.notify_agent_run_terminal("goal-goal-1") is True

    assert [(msg.target, msg.text) for msg in channel.messages] == [
        ("conversation-1", "Goal completed: ship channel parity")
    ]
    notification = tasks.get_channel_thread_notification("goal_terminal:goal-1:completed")
    assert notification and notification["status"] == "delivered"
    ai_messages = [
        msg for msg in threads.get_latest_checkpoint_messages("goal-thread")
        if getattr(msg, "type", "") == "ai"
    ]
    assert [getattr(msg, "content", "") for msg in ai_messages] == [
        "Goal completed: ship channel parity"
    ]

    assert thread_notifications.notify_agent_run_terminal("goal-goal-1") is True
    assert len(channel.messages) == 1
