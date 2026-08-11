from __future__ import annotations

import importlib

import pytest

from tests.fixtures.channels import FakeChannel
from tests.fixtures.tasks import fresh_tasks_module


pytestmark = pytest.mark.subsystem


def test_cross_channel_approval_resolution_updates_other_channels(tmp_path, monkeypatch) -> None:
    tasks = fresh_tasks_module(tmp_path, monkeypatch)
    from row_bot.channels import registry

    registry._reset()
    source = FakeChannel(name="source")
    mirror = FakeChannel(name="mirror")
    source._running = True
    mirror._running = True
    registry.register(source)
    registry.register(mirror)

    token, approval_id = tasks.create_approval_request("run-1", "task-1", "approval_1", "Approve?")
    tasks._store_approval_channel_ref(approval_id, "source", "source-ref")
    tasks._store_approval_channel_ref(approval_id, "mirror", "mirror-ref")
    monkeypatch.setattr(tasks, "_resume_pipeline", lambda *_args, **_kwargs: None)

    assert tasks.respond_to_approval(token, True, source="source") is True

    assert source.approval_updates == []
    assert mirror.approval_updates == [("mirror-ref", "approved", "source")]


def test_channel_approval_helpers_round_trip_interrupt_text() -> None:
    from row_bot.channels.approval import extract_interrupt_ids, format_interrupt_text, is_approval_text

    interrupt_data = [
        {"__interrupt_id": "abc", "tool": "developer_apply_patch", "description": "Apply patch"},
        {"__interrupt_id": "def", "tool": "shell", "description": "Run command"},
    ]
    text = format_interrupt_text(interrupt_data)

    assert "Apply patch" in text
    assert extract_interrupt_ids(interrupt_data) == ["abc", "def"]
    assert extract_interrupt_ids([interrupt_data[0]]) == ["abc"]
    assert is_approval_text("approve") is True
    assert is_approval_text("deny") is False
    assert is_approval_text(text) is None


def test_child_agent_approval_routes_to_parent_channel(tmp_path, monkeypatch) -> None:
    tasks = fresh_tasks_module(tmp_path, monkeypatch)
    from row_bot.channels import registry
    from row_bot.channels.thread_notifications import notify_agent_run_approval
    import row_bot.threads as threads
    import row_bot.agent_runner as agent_runner

    threads = importlib.reload(threads)

    registry._reset()
    source = FakeChannel(name="source")
    source._running = True
    registry.register(source)
    resumed: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        agent_runner,
        "resume_agent_run",
        lambda run_id, *, resume_token="", approved=True: resumed.append((run_id, approved)),
    )

    tasks.record_thread_channel_ref(
        "parent-thread",
        channel="source",
        target="conversation-1",
        external_conversation_id="conversation-1",
    )
    token, approval_id = tasks.create_approval_request(
        run_id="child-run",
        task_id="",
        step_id="agent_interrupt",
        message="Child needs approval.",
        agent_run_id="child-run",
        resume_kind="agent_run",
        source_label="Child Agent",
        source_thread_id="child-thread",
        parent_thread_id="parent-thread",
        approval_payload_json={
            "title": "Child Agent needs approval to run a command.",
            "reason": "Check the current branch.",
            "tool": "run_command",
            "raw_action": "git status",
            "source_label": "Child Agent",
        },
    )

    assert notify_agent_run_approval(approval_id) is True
    assert notify_agent_run_approval(approval_id) is True
    assert len(source.approvals) == 1
    sent = source.approvals[0]
    assert sent["target"] == "conversation-1"
    assert sent["config"]["approval_kind"] == "agent_run"
    assert sent["config"]["resume_token"] == token
    assert "Check the current branch." in sent["config"]["message"]
    approval_messages = [
            message
            for message in threads.get_latest_checkpoint_messages("parent-thread")
        if (
            getattr(message, "additional_kwargs", {})
            .get("row_bot_ui", {})
            .get("approval_request_id")
            == approval_id
        )
    ]
    assert len(approval_messages) == 1

    assert tasks.respond_to_approval(token, True, source="web") is True

    assert resumed == [("child-run", True)]
    assert source.approval_updates == [(sent["message_ref"], "approved", "web")]
