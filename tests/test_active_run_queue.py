from __future__ import annotations

import ast
import importlib
import inspect
import json
import sys
from types import SimpleNamespace


def _fresh_modules(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(data_dir))
    for name in (
        "row_bot.threads",
        "row_bot.tasks",
        "row_bot.agent_runs",
        "row_bot.channels.thread_notifications",
        "row_bot.ui.streaming",
        "row_bot.ui.transcript",
    ):
        sys.modules.pop(name, None)

    import row_bot.threads as threads
    import row_bot.tasks as tasks
    import row_bot.agent_runs as agent_runs
    import row_bot.ui.streaming as streaming
    import row_bot.ui.transcript as transcript

    threads = importlib.reload(threads)
    tasks = importlib.reload(tasks)
    agent_runs = importlib.reload(agent_runs)
    streaming = importlib.reload(streaming)
    transcript = importlib.reload(transcript)
    return agent_runs, streaming, transcript


def test_queued_control_message_dispatch_updates_render_key(tmp_path, monkeypatch):
    _agent_runs, streaming, transcript = _fresh_modules(tmp_path, monkeypatch)

    msg = streaming._queued_control_message(
        "use another agent for the quantum essay",
        kind="follow_up",
        status="queued_parent_turn",
        label="Queued as your next chat message",
        message_id="queued-1",
    )
    before = transcript.message_key(0, msg)

    changed = streaming._mark_queued_controls_dispatching([msg], ["queued-1"])
    after = transcript.message_key(0, msg)

    assert changed is True
    assert msg["queued_control"]["status"] == "dispatching"
    assert msg["queued_control"]["label"] == "Sent after current response"
    assert before != after


def test_stale_thinking_handle_delete_is_idempotent(tmp_path, monkeypatch):
    _agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)

    class StaleHandle:
        def delete(self):
            raise ValueError("list.remove(x): x not in list")

    streaming._delete_ui_handle_safely(StaleHandle(), "Thinking markdown")


def test_queued_control_settlement_removes_rendered_badge(tmp_path, monkeypatch):
    _agent_runs, streaming, transcript = _fresh_modules(tmp_path, monkeypatch)

    msg = streaming._queued_control_message(
        "use another agent for the quantum essay",
        kind="follow_up",
        status="queued_parent_turn",
        label="Queued as your next chat message",
        message_id="queued-1",
    )
    assert streaming._mark_queued_controls_dispatching([msg], ["queued-1"])
    before = transcript.message_key(0, msg)

    changed = streaming._settle_queued_controls([msg], ["queued-1"])
    after = transcript.message_key(0, msg)

    assert changed is True
    assert "queued_control" not in msg
    assert before != after


def test_assistant_insert_respects_future_and_current_queued_turns(tmp_path, monkeypatch):
    _agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)

    first_user = {"role": "user", "content": "first"}
    queued = streaming._queued_control_message(
        "use another agent",
        kind="follow_up",
        status="queued_parent_turn",
        label="Queued as your next chat message",
        message_id="queued-1",
    )
    messages = [first_user, queued]

    inserted = streaming._insert_assistant_before_future_queued_turns(
        messages,
        {"role": "assistant", "content": "first answer"},
    )

    assert inserted == 1
    assert [msg["role"] for msg in messages] == ["user", "assistant", "user"]
    assert messages[2]["queued_control"]["id"] == "queued-1"

    streaming._mark_queued_controls_dispatching(messages, ["queued-1"])
    inserted = streaming._insert_assistant_before_future_queued_turns(
        messages,
        {"role": "assistant", "content": "second answer"},
        current_queued_ids=["queued-1"],
    )

    assert inserted == 3
    assert [msg["content"] for msg in messages] == [
        "first",
        "first answer",
        "use another agent",
        "second answer",
    ]


def test_assistant_insert_respects_direct_agent_turn_boundaries(tmp_path, monkeypatch):
    _agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)

    messages = [
        {"role": "user", "content": "first"},
        {
            "role": "user",
            "content": "use another agent",
            "turn_boundary": {"after_generation_id": "gen-1"},
        },
        {
            "role": "assistant",
            "content": "Started Agent",
            "agent_run_ids": ["run-1"],
            "agent_run_refresh_key": "queued",
        },
    ]

    inserted = streaming._insert_assistant_before_future_queued_turns(
        messages,
        {"role": "assistant", "content": "first answer"},
        current_generation_id="gen-1",
    )

    assert inserted == 1
    assert [msg["content"] for msg in messages] == [
        "first",
        "first answer",
        "use another agent",
        "Started Agent",
    ]

    inserted = streaming._insert_assistant_before_future_queued_turns(
        messages,
        {"role": "assistant", "content": "later answer"},
        current_generation_id="gen-2",
    )

    assert inserted == 4
    assert messages[-1]["content"] == "later answer"


def test_agent_run_refresh_key_updates_render_key(tmp_path, monkeypatch):
    _agent_runs, _streaming, transcript = _fresh_modules(tmp_path, monkeypatch)

    msg = {
        "role": "assistant",
        "content": "Started Agent",
        "agent_run_ids": ["run-1"],
        "agent_run_refresh_key": "queued",
    }
    before = transcript.message_key(0, msg)
    msg["agent_run_refresh_key"] = "completed"
    after = transcript.message_key(0, msg)

    assert before != after


def test_direct_agent_completion_summary_appends_once(tmp_path, monkeypatch):
    agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)

    agent_runs.create_agent_run(
        run_id="run-1",
        kind="subagent",
        status="running",
        parent_thread_id="parent",
        display_name="PDF Writer",
    )
    messages = [
        {
            "role": "assistant",
            "content": "Started a Worker agent for this. I'll keep this thread updated.",
            "agent_run_ids": ["run-1"],
            "agent_run_refresh_key": "running",
            "agent_lifecycle": {
                "kind": "direct_agent_spawn",
                "run_id": "run-1",
                "completion_summary_emitted": False,
            },
        }
    ]

    assert streaming._append_direct_agent_completion_messages(messages, ["run-1"]) is False

    agent_runs.finish_agent_run(
        "run-1",
        "completed",
        summary="Created C:\\Users\\v_2si\\Documents\\Row-Bot\\ai_agent_smoke.pdf",
    )

    assert streaming._append_direct_agent_completion_messages(messages, ["run-1"]) is True
    assert messages[0]["agent_lifecycle"]["completion_summary_emitted"] is True
    assert messages[1]["role"] == "assistant"
    assert "PDF Writer completed" in messages[1]["content"]
    assert "ai_agent_smoke.pdf" in messages[1]["content"]
    assert streaming._append_direct_agent_completion_messages(messages, ["run-1"]) is False
    assert len(messages) == 2


def test_child_agent_approval_message_appends_and_dedupes(tmp_path, monkeypatch):
    agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)
    import row_bot.tasks as tasks

    agent_runs.create_agent_run(
        run_id="run-approval",
        kind="subagent",
        status="waiting_approval",
        parent_thread_id="parent",
        display_name="Approval Child",
    )
    _token, approval_id = tasks.create_approval_request(
        run_id="run-approval",
        task_id="",
        step_id="agent_interrupt",
        message="Approval Child needs approval.",
        agent_run_id="run-approval",
        resume_kind="agent_run",
        source_label="Approval Child",
        source_thread_id="child-thread",
        parent_thread_id="parent",
        approval_payload_json={
            "title": "Approval Child needs approval to run a command.",
            "reason": "Check the focused tests.",
            "tool": "run_command",
            "raw_action": "uv run python -m pytest tests/test_agent_approvals.py -q",
            "source_label": "Approval Child",
        },
    )
    messages = [
        {
            "role": "assistant",
            "content": "Started child.",
            "agent_run_ids": ["run-approval"],
            "agent_lifecycle": {
                "kind": "delegated_agent_spawn",
                "run_id": "run-approval",
                "completion_summary_emitted": False,
            },
        }
    ]

    assert streaming._append_child_agent_approval_messages(messages, ["run-approval"]) is True
    assert messages[0]["agent_lifecycle"]["approval_id"] == approval_id
    assert messages[1]["approval_request_id"] == approval_id
    assert messages[1]["agent_approval_for"] == "run-approval"
    assert "Check the focused tests." in messages[1]["content"]
    assert streaming._append_child_agent_approval_messages(messages, ["run-approval"]) is False
    assert len([msg for msg in messages if msg.get("approval_request_id") == approval_id]) == 1


def test_retry_child_approval_appends_without_replacement_lifecycle_card(
    tmp_path,
    monkeypatch,
):
    agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)
    import row_bot.tasks as tasks

    agent_runs.create_agent_run(
        run_id="run-original",
        kind="subagent",
        status="stopped",
        parent_thread_id="parent",
        display_name="Original Child",
    )
    agent_runs.create_agent_run(
        run_id="run-retry",
        kind="subagent",
        status="waiting_approval",
        parent_thread_id="parent",
        display_name="Replacement Child",
    )
    _token, approval_id = tasks.create_approval_request(
        run_id="run-retry",
        task_id="",
        step_id="agent_interrupt",
        message="Replacement Child needs approval.",
        agent_run_id="run-retry",
        resume_kind="agent_run",
        source_label="Replacement Child",
        source_thread_id="retry-thread",
        parent_thread_id="parent",
    )
    messages = [
        {
            "role": "assistant",
            "content": "Started original child.",
            "agent_run_ids": ["run-original"],
            "agent_lifecycle": {
                "kind": "delegated_agent_spawn",
                "run_id": "run-original",
            },
        }
    ]

    assert streaming._append_child_agent_approval_messages(
        messages,
        ["run-original", "run-retry"],
    )
    approval_messages = [
        msg for msg in messages if msg.get("approval_request_id") == approval_id
    ]
    assert len(approval_messages) == 1
    assert approval_messages[0]["agent_approval_for"] == "run-retry"
    assert streaming._append_child_agent_approval_messages(
        messages,
        ["run-original", "run-retry"],
    ) is False


def test_child_agent_approval_sync_refreshes_during_live_parent(tmp_path, monkeypatch):
    agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)
    import row_bot.tasks as tasks

    agent_runs.create_agent_run(
        run_id="run-live-approval",
        kind="subagent",
        status="waiting_approval",
        parent_thread_id="parent",
        display_name="Live Approval Child",
    )
    _token, approval_id = tasks.create_approval_request(
        run_id="run-live-approval",
        task_id="",
        step_id="agent_interrupt",
        message="Live Approval Child needs approval.",
        agent_run_id="run-live-approval",
        resume_kind="agent_run",
        source_label="Live Approval Child",
        source_thread_id="child-thread",
        parent_thread_id="parent",
    )
    cached: list[str] = []
    refreshed: list[str] = []
    state = SimpleNamespace(
        thread_id="parent",
        messages=[
            {
                "role": "assistant",
                "content": "Started child.",
                "agent_run_ids": ["run-live-approval"],
                "agent_lifecycle": {
                    "kind": "delegated_agent_spawn",
                    "run_id": "run-live-approval",
                    "source_generation_id": "parent-generation",
                },
            }
        ],
        cache_active_messages=lambda: cached.append("cached"),
    )
    cb = streaming.Callbacks()
    cb.refresh_chat_messages = lambda: refreshed.append("chat")

    assert streaming._sync_child_agent_approval_messages(
        state=state,
        cb=cb,
        thread_id="parent",
        run_ids=["run-live-approval"],
        refresh_transcript=True,
    ) is True
    assert cached == ["cached"]
    assert refreshed == ["chat"]
    assert state.messages[-1]["approval_request_id"] == approval_id

    assert streaming._sync_child_agent_approval_messages(
        state=state,
        cb=cb,
        thread_id="parent",
        run_ids=["run-live-approval"],
        refresh_transcript=True,
    ) is False
    assert cached == ["cached"]
    assert refreshed == ["chat"]


def test_required_delegate_tool_result_registers_durable_parent_card(
    tmp_path,
    monkeypatch,
):
    agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)

    run = agent_runs.create_agent_run(
        run_id="required-child",
        kind="subagent",
        status="running",
        parent_thread_id="parent-thread",
        display_name="Required Child",
    )
    state = SimpleNamespace(
        thread_id="parent-thread",
        messages=[{"role": "user", "content": "delegate this"}],
        message_cache={},
        message_cache_dirty=set(),
        cache_active_messages=lambda: None,
    )
    gen = SimpleNamespace(
        thread_id="parent-thread",
        generation_id="generation-1",
        queued_message_ids=[],
        detached=True,
        tool_col=None,
        live_agent_run_ids=set(),
        live_async_agent_run_ids=set(),
    )
    cb = streaming.Callbacks()
    cb.refresh_chat_messages = lambda: None
    cb.refresh_parent_agent_strip = lambda: None
    monkeypatch.setattr(
        streaming,
        "_schedule_direct_agent_card_refresh",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        streaming,
        "_schedule_parent_agent_strip_refresh",
        lambda *_args, **_kwargs: None,
    )
    payload = json.dumps(
        {
            "ok": True,
            "message": "Child Agent started.",
            "run": {"id": run["id"], "status": "running"},
            "orchestration": {"id": "orchestration-1", "required": True},
        }
    )

    assert streaming._register_delegated_agent_tool_result(
        gen,
        state,
        cb,
        raw_tool_name="delegate_work",
        tool_content=payload,
    ) == ["required-child"]
    assert state.messages[1]["agent_run_ids"] == ["required-child"]
    assert state.messages[1]["agent_lifecycle"]["orchestration_id"] == "orchestration-1"
    assert state.messages[1]["agent_lifecycle"]["required"] is True

    from row_bot.ui.helpers import load_thread_messages

    loaded = load_thread_messages("parent-thread")
    assert [msg.get("agent_run_ids") for msg in loaded] == [["required-child"]]

    assert streaming._register_delegated_agent_tool_result(
        gen,
        state,
        cb,
        raw_tool_name="delegate_work",
        tool_content=payload,
    ) == ["required-child"]
    assert len(
        [
            msg
            for msg in state.messages
            if "required-child" in (msg.get("agent_run_ids") or [])
        ]
    ) == 1
    assert len(
        [
            msg
            for msg in load_thread_messages("parent-thread")
            if "required-child" in (msg.get("agent_run_ids") or [])
        ]
    ) == 1
    assert "_register_delegated_agent_tool_result" in inspect.getsource(
        streaming._handle_tool_done
    )


def test_required_lifecycle_is_republished_without_parent_checkpoint_cleanup(
    tmp_path,
    monkeypatch,
):
    agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)
    import row_bot.agent_orchestrator as orchestrator
    import row_bot.channels.thread_notifications as thread_notifications

    run = agent_runs.create_agent_run(
        run_id="required-after-cleanup",
        kind="subagent",
        status="running",
        parent_thread_id="parent-thread",
        display_name="Required Child",
    )
    published: list[dict] = []
    monkeypatch.setattr(
        orchestrator,
        "list_members",
        lambda orchestration_id: [
            {
                "orchestration_id": orchestration_id,
                "run_id": run["id"],
                "required": True,
                "status": "running",
                "run": run,
            },
            {
                "orchestration_id": orchestration_id,
                "run_id": "optional-child",
                "required": False,
                "status": "running",
                "run": {"id": "optional-child", "parent_thread_id": "parent-thread"},
            },
        ],
    )
    monkeypatch.setattr(
        thread_notifications,
        "append_agent_lifecycle_message_once",
        lambda row, **metadata: published.append(
            {"run": row, **metadata}
        ) or True,
    )

    assert streaming._republish_required_orchestration_lifecycle(
        "orchestration-1",
        source_generation_id="generation-1",
    ) == 1
    assert published == [
        {
            "run": run,
            "source_generation_id": "generation-1",
            "orchestration_id": "orchestration-1",
            "required": True,
            "wait_mode": False,
        }
    ]
    suspension_source = inspect.getsource(streaming.consume_generation)
    republish_index = suspension_source.index(
        "_republish_required_orchestration_lifecycle"
    )
    assert republish_index >= 0
    assert "remove_latest_checkpoint_ai_message" not in suspension_source
    assert "finalize_parent_generation(" not in suspension_source


def test_direct_agent_completion_summary_dedupes_existing_completion_row(tmp_path, monkeypatch):
    agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)

    agent_runs.create_agent_run(
        run_id="run-existing",
        kind="subagent",
        status="completed",
        parent_thread_id="parent",
        display_name="PDF Writer",
        summary="Created report.pdf",
    )
    messages = [
        {
            "role": "assistant",
            "content": "Started a Worker agent for this.",
            "agent_run_ids": ["run-existing"],
            "agent_lifecycle": {
                "kind": "direct_agent_spawn",
                "run_id": "run-existing",
                "completion_summary_emitted": False,
            },
        },
        {
            "role": "assistant",
            "content": "Done. PDF Writer completed.\n\nCreated report.pdf",
            "agent_completion_for": "run-existing",
        },
    ]

    assert streaming._append_direct_agent_completion_messages(messages, ["run-existing"]) is True
    assert messages[0]["agent_lifecycle"]["completion_summary_emitted"] is True
    assert len(messages) == 2


def test_direct_agent_completion_summary_dedupes_reloaded_plain_rows(tmp_path, monkeypatch):
    agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)

    agent_runs.create_agent_run(
        run_id="run-plain",
        kind="subagent",
        status="completed",
        parent_thread_id="parent",
        display_name="PDF Writer",
        summary="Created report.pdf",
    )
    summary = "Done. PDF Writer completed.\n\nCreated report.pdf"
    messages = [
        {
            "role": "assistant",
            "content": "Started a Worker agent for this.",
            "agent_run_ids": ["run-plain"],
            "agent_lifecycle": {
                "kind": "direct_agent_spawn",
                "run_id": "run-plain",
                "completion_summary_emitted": False,
            },
        },
        {"role": "assistant", "content": summary},
        {"role": "assistant", "content": summary},
    ]

    assert streaming._append_direct_agent_completion_messages(messages, ["run-plain"]) is True
    assert messages[0]["agent_lifecycle"]["completion_summary_emitted"] is True
    assert [msg["content"] for msg in messages] == [
        "Started a Worker agent for this.",
        summary,
    ]
    assert messages[1]["agent_completion_for"] == "run-plain"


def test_direct_agent_messages_round_trip_agent_metadata_through_checkpoint(tmp_path, monkeypatch):
    agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)
    import row_bot.ui.helpers as helpers

    helpers = importlib.reload(helpers)
    agent_runs.create_agent_run(
        run_id="run-checkpoint",
        kind="subagent",
        status="running",
        parent_thread_id="parent",
        display_name="Checkpoint Agent",
    )
    user_msg = {
        "role": "user",
        "content": "/agent do checkpoint work",
        "timestamp": "12:00",
    }
    start_msg = {
        "role": "assistant",
        "content": "Started a Worker agent for this. I'll keep this thread updated.",
        "timestamp": "12:00",
        "agent_run_ids": ["run-checkpoint"],
        "agent_run_refresh_key": "run-checkpoint|running",
        "agent_lifecycle": {
            "kind": "direct_agent_spawn",
            "run_id": "run-checkpoint",
            "completion_summary_emitted": False,
        },
    }
    messages = [user_msg, start_msg]
    streaming._append_ui_messages_to_checkpoint("parent", messages)

    agent_runs.finish_agent_run("run-checkpoint", "completed", summary="checkpoint result")
    assert streaming._append_direct_agent_completion_messages(
        messages,
        ["run-checkpoint"],
        checkpoint_thread_id="parent",
    ) is True

    reloaded = helpers.load_thread_messages("parent")

    assert [msg["role"] for msg in reloaded] == ["user", "assistant", "assistant"]
    assert reloaded[1]["agent_run_ids"] == ["run-checkpoint"]
    assert reloaded[1]["agent_lifecycle"]["kind"] == "direct_agent_spawn"
    assert reloaded[1]["agent_lifecycle"]["completion_summary_emitted"] is False
    assert reloaded[2]["agent_completion_for"] == "run-checkpoint"
    assert reloaded[2]["content"] == "Done. Checkpoint Agent completed.\n\ncheckpoint result"


def test_langchain_message_conversion_restores_row_bot_ui_metadata(tmp_path, monkeypatch):
    _agent_runs, _streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)
    import row_bot.ui.helpers as helpers
    from langchain_core.messages import AIMessage

    helpers = importlib.reload(helpers)
    converted = helpers.langchain_messages_to_ui_messages([
        AIMessage(
            content="Started a Worker agent for this.",
            additional_kwargs={
                "row_bot_ui": {
                    "timestamp": "12:00",
                    "agent_run_ids": ["run-meta"],
                    "agent_run_refresh_key": "run-meta|running",
                    "agent_lifecycle": {"kind": "direct_agent_spawn", "run_id": "run-meta"},
                }
            },
        )
    ])

    assert converted == [
        {
            "role": "assistant",
            "content": "Started a Worker agent for this.",
            "timestamp": "12:00",
            "agent_run_ids": ["run-meta"],
            "agent_run_refresh_key": "run-meta|running",
            "agent_lifecycle": {"kind": "direct_agent_spawn", "run_id": "run-meta"},
        }
    ]


def test_async_delegated_tool_result_completion_summary_appends_once(tmp_path, monkeypatch):
    agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)

    agent_runs.create_agent_run(
        run_id="delegated-tool-1",
        kind="subagent",
        status="running",
        parent_thread_id="parent",
        display_name="Child model smoke",
    )
    messages = [
        {"role": "user", "content": "use a child agent in the background"},
        {
            "role": "assistant",
            "content": "Started in the background.",
            "tool_results": [
                {
                    "name": "delegate_work",
                    "content": json.dumps({
                        "ok": True,
                        "message": "Child Agent started.",
                        "run": {"id": "delegated-tool-1", "status": "queued"},
                    }),
                }
            ],
        },
    ]

    assert streaming._append_async_delegated_agent_completion_messages(
        messages,
        ["delegated-tool-1"],
    ) is False

    agent_runs.finish_agent_run(
        "delegated-tool-1",
        "completed",
        summary="child model smoke",
    )

    assert streaming._append_async_delegated_agent_completion_messages(
        messages,
        ["delegated-tool-1"],
    ) is True
    assert messages[2]["agent_completion_for"] == "delegated-tool-1"
    assert "Child model smoke completed" in messages[2]["content"]
    assert "child model smoke" in messages[2]["content"]
    assert streaming._append_async_delegated_agent_completion_messages(
        messages,
        ["delegated-tool-1"],
    ) is False
    assert len(messages) == 3


def test_async_delegated_completion_skips_lifecycle_agent_card(tmp_path, monkeypatch):
    agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)

    agent_runs.create_agent_run(
        run_id="direct-owned",
        kind="subagent",
        status="completed",
        parent_thread_id="parent",
        display_name="Direct Agent",
        summary="direct result",
    )
    messages = [
        {
            "role": "assistant",
            "content": "Started a Worker agent for this.",
            "agent_run_ids": ["direct-owned"],
            "agent_lifecycle": {
                "kind": "direct_agent_spawn",
                "run_id": "direct-owned",
                "completion_summary_emitted": False,
            },
        }
    ]

    assert streaming._append_async_delegated_agent_completion_messages(
        messages,
        candidate_run_ids=["direct-owned"],
    ) is False
    assert len(messages) == 1
    assert messages[0]["agent_lifecycle"]["completion_summary_emitted"] is False


def test_async_delegated_tool_result_completion_can_persist_checkpoint(tmp_path, monkeypatch):
    agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)

    agent_runs.create_agent_run(
        run_id="delegated-tool-checkpoint",
        kind="subagent",
        status="completed",
        parent_thread_id="parent-thread",
        display_name="Checkpoint child",
        summary="checkpoint child result",
    )
    messages = [
        {
            "role": "assistant",
            "content": "Started in the background.",
            "tool_results": [
                {
                    "name": "delegate_work",
                    "content": json.dumps({
                        "ok": True,
                        "message": "Child Agent started.",
                        "run": {"id": "delegated-tool-checkpoint", "status": "queued"},
                    }),
                }
            ],
        },
    ]

    assert streaming._append_async_delegated_agent_completion_messages(
        messages,
        ["delegated-tool-checkpoint"],
        checkpoint_thread_id="parent-thread",
    ) is True

    from row_bot.threads import get_latest_checkpoint_messages

    checkpoint_messages = get_latest_checkpoint_messages("parent-thread")
    assert [getattr(msg, "content", "") for msg in checkpoint_messages] == [
        "Done. Checkpoint child completed.\n\ncheckpoint child result"
    ]


def test_promoted_async_delegated_card_completion_appends_without_tool_result(tmp_path, monkeypatch):
    agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)

    agent_runs.create_agent_run(
        run_id="promoted-async",
        kind="subagent",
        status="completed",
        parent_thread_id="parent",
        display_name="Child model smoke",
        summary="child model smoke",
    )
    messages = [
        {"role": "user", "content": "use an agent in the background"},
        {
            "role": "assistant",
            "content": "Started in the background.",
            "agent_run_ids": ["promoted-async"],
            "agent_run_refresh_key": "promoted-async|completed",
            "tool_results": [
                {"name": "row_bot_status", "content": "Status checked."},
            ],
        },
    ]

    assert streaming._append_async_delegated_agent_completion_messages(
        messages,
        ["promoted-async"],
        candidate_run_ids=["promoted-async"],
    ) is True
    assert messages[2]["agent_completion_for"] == "promoted-async"
    assert messages[2]["content"] == "Done. Child model smoke completed.\n\nchild model smoke"

    assert streaming._append_async_delegated_agent_completion_messages(
        messages,
        ["promoted-async"],
        candidate_run_ids=["promoted-async"],
    ) is False
    assert len(messages) == 3


def test_wait_delegated_tool_result_does_not_append_async_completion(tmp_path, monkeypatch):
    agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)

    agent_runs.create_agent_run(
        run_id="delegated-wait-tool",
        kind="subagent",
        status="completed",
        parent_thread_id="parent",
        display_name="Review child",
        summary="The claim is false.",
    )
    messages = [
        {
            "role": "assistant",
            "content": "The review agent completed.",
            "tool_results": [
                {
                    "name": "delegate_work",
                    "content": json.dumps({
                        "ok": True,
                        "message": "Child Agent completed.",
                        "run": {"id": "delegated-wait-tool", "status": "completed"},
                    }),
                }
            ],
        }
    ]

    assert streaming._is_async_delegated_agent_tool_result(messages[0]["tool_results"][0]) is False
    assert streaming._append_async_delegated_agent_completion_messages(
        messages,
        ["delegated-wait-tool"],
    ) is False
    assert len(messages) == 1


def test_wait_delegated_candidate_does_not_append_async_completion(tmp_path, monkeypatch):
    agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)

    agent_runs.create_agent_run(
        run_id="delegated-wait-candidate",
        kind="subagent",
        status="completed",
        parent_thread_id="parent",
        display_name="Wait Candidate",
        summary="Parent already summarized this.",
    )
    messages = [
        {
            "role": "assistant",
            "content": "The review agent completed.",
            "tool_results": [
                {
                    "name": "delegate_work",
                    "content": json.dumps({
                        "ok": True,
                        "message": "Child Agent completed.",
                        "run": {"id": "delegated-wait-candidate", "status": "completed"},
                    }),
                }
            ],
        }
    ]

    assert streaming._append_async_delegated_agent_completion_messages(
        messages,
        ["delegated-wait-candidate"],
        candidate_run_ids=["delegated-wait-candidate"],
    ) is False
    assert len(messages) == 1


def test_async_delegated_run_ids_from_tool_results_excludes_wait_results(tmp_path, monkeypatch):
    _agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)

    tool_results = [
        {
            "name": "delegate_work",
            "content": json.dumps({
                "ok": True,
                "message": "Child Agent started.",
                "run": {"id": "async-run", "status": "queued"},
            }),
        },
        {
            "name": "delegate_work",
            "content": json.dumps({
                "ok": True,
                "message": "Child Agent completed.",
                "run": {"id": "wait-run", "status": "completed"},
            }),
        },
        {"name": "row_bot_status", "content": "ok"},
    ]

    assert streaming._async_delegated_run_ids_from_tool_results(tool_results) == ["async-run"]


def test_async_child_run_ids_for_generation_uses_new_runs_and_excludes_wait(tmp_path, monkeypatch):
    agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)

    agent_runs.create_agent_run(
        run_id="old-child",
        kind="subagent",
        status="completed",
        parent_thread_id="parent",
        display_name="Old Child",
    )
    agent_runs.create_agent_run(
        run_id="async-child",
        kind="subagent",
        status="completed",
        parent_thread_id="parent",
        display_name="Async Child",
    )
    agent_runs.create_agent_run(
        run_id="wait-child",
        kind="subagent",
        status="completed",
        parent_thread_id="parent",
        display_name="Wait Child",
    )
    wait_result = {
        "name": "delegate_work",
        "content": json.dumps({
            "ok": True,
            "message": "Child Agent completed.",
            "run": {"id": "wait-child", "status": "completed"},
        }),
    }
    gen = SimpleNamespace(
        thread_id="parent",
        baseline_child_agent_run_ids={"old-child"},
        live_async_agent_run_ids=set(),
    )

    assert streaming._wait_delegated_run_ids_from_tool_results([wait_result]) == {"wait-child"}
    assert set(streaming._async_child_agent_run_ids_for_generation(gen, [wait_result])) == {
        "async-child",
    }


def test_async_child_run_ids_for_generation_includes_live_async_ids(tmp_path, monkeypatch):
    _agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)

    gen = SimpleNamespace(
        thread_id="parent",
        baseline_child_agent_run_ids=set(),
        live_async_agent_run_ids={"live-async"},
    )

    assert streaming._async_child_agent_run_ids_for_generation(gen, []) == ["live-async"]


def test_visible_agent_run_ids_collects_direct_and_delegated_cards(tmp_path, monkeypatch):
    _agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)

    messages = [
        {"role": "assistant", "agent_run_ids": ["run-1", ""]},
        {"role": "assistant", "agent_run_ids": ["run-2"]},
        {"role": "user", "agent_run_ids": ["ignored"]},
    ]

    assert streaming._visible_agent_run_ids(messages) == {"run-1", "run-2"}


def test_agent_result_use_prompt_is_explicit_parent_message() -> None:
    from row_bot.ui.render import agent_result_use_available, agent_result_use_prompt

    assert agent_result_use_prompt("run-123") == "what did agent run-123 find? use that result here"
    assert agent_result_use_prompt("  run-123  ") == "what did agent run-123 find? use that result here"
    assert agent_result_use_prompt("") == ""
    assert agent_result_use_available({"id": "run-1", "status": "completed"})
    assert agent_result_use_available({"id": "run-1", "status": "completed_delivery_failed"})
    assert not agent_result_use_available({"id": "run-1", "status": "running"})
    assert not agent_result_use_available({"id": "", "status": "completed"})


def test_delegated_agent_card_message_inserts_before_future_queued_turn(tmp_path, monkeypatch):
    agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)

    run = agent_runs.create_agent_run(
        run_id="delegated-1",
        kind="subagent",
        status="running",
        parent_thread_id="parent",
        display_name="Review Child",
        profile_id="builtin:review",
        profile_snapshot_json={"slug": "review", "display_name": "Review"},
    )
    queued = streaming._queued_control_message(
        "also use another agent",
        kind="follow_up",
        status="queued_parent_turn",
        label="Queued as your next chat message",
        message_id="queued-1",
    )
    state = SimpleNamespace(
        thread_id="parent",
        messages=[{"role": "user", "content": "first"}, queued],
        message_cache={},
        message_cache_dirty=set(),
        cache_active_messages=lambda: None,
    )
    refreshed: list[str] = []
    cb = streaming.Callbacks()
    cb.refresh_chat_messages = lambda: refreshed.append("chat")
    cb.refresh_parent_agent_strip = lambda: refreshed.append("strip")

    assert streaming._append_delegated_agent_card_message(
        state=state,
        cb=cb,
        thread_id="parent",
        run_row=run,
        generation_id="gen-1",
    ) is True

    assert [msg["role"] for msg in state.messages] == ["user", "assistant", "user"]
    assert state.messages[1]["agent_run_ids"] == ["delegated-1"]
    assert state.messages[1]["agent_lifecycle"]["kind"] == "delegated_agent_spawn"
    assert state.messages[1]["agent_lifecycle"]["completion_summary_emitted"] is False
    assert state.messages[1]["agent_lifecycle"]["wait_mode"] is False
    assert state.messages[2]["queued_control"]["id"] == "queued-1"
    assert "chat" in refreshed

    agent_runs.finish_agent_run("delegated-1", "completed", summary="Looks good.")
    assert streaming._append_direct_agent_completion_messages(state.messages, ["delegated-1"]) is True
    assert len(state.messages) == 4
    assert state.messages[1]["agent_lifecycle"]["completion_summary_emitted"] is True
    assert state.messages[2]["agent_completion_for"] == "delegated-1"
    assert "Review Child completed" in state.messages[2]["content"]
    assert "Looks good." in state.messages[2]["content"]
    assert state.messages[3]["queued_control"]["id"] == "queued-1"
    assert streaming._append_direct_agent_completion_messages(state.messages, ["delegated-1"]) is False


def test_delegated_agent_card_messages_insert_during_live_parent_in_order(
    tmp_path,
    monkeypatch,
):
    agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)

    first_run = agent_runs.create_agent_run(
        run_id="delegated-live-1",
        kind="subagent",
        status="running",
        parent_thread_id="parent",
        display_name="First Live Child",
    )
    second_run = agent_runs.create_agent_run(
        run_id="delegated-live-2",
        kind="subagent",
        status="running",
        parent_thread_id="parent",
        display_name="Second Live Child",
    )
    queued = streaming._queued_control_message(
        "queued follow-up",
        kind="follow_up",
        status="queued_parent_turn",
        label="Queued as your next chat message",
        message_id="queued-live",
    )
    state = SimpleNamespace(
        thread_id="parent",
        messages=[{"role": "user", "content": "first"}, queued],
        message_cache={},
        message_cache_dirty=set(),
        cache_active_messages=lambda: None,
    )
    refreshed: list[str] = []
    inserted: list[tuple[str, object]] = []
    cb = streaming.Callbacks()
    cb.refresh_chat_messages = lambda: refreshed.append("chat")
    cb.refresh_parent_agent_strip = lambda: refreshed.append("strip")
    cb.insert_chat_message_before_live_row = lambda msg, row: inserted.append(
        (msg["agent_run_ids"][0], row)
    )
    live_row = object()
    streaming._active_generations["parent"] = SimpleNamespace(
        status="streaming",
        detached=False,
        live_row=live_row,
    )
    try:
        assert streaming._append_delegated_agent_card_message(
            state=state,
            cb=cb,
            thread_id="parent",
            run_row=first_run,
            generation_id="gen-live",
        ) is True
        assert streaming._append_delegated_agent_card_message(
            state=state,
            cb=cb,
            thread_id="parent",
            run_row=second_run,
            generation_id="gen-live",
        ) is True
        assert streaming._append_delegated_agent_card_message(
            state=state,
            cb=cb,
            thread_id="parent",
            run_row=first_run,
            generation_id="gen-live",
        ) is False
    finally:
        streaming._active_generations.pop("parent", None)

    assert "chat" not in refreshed
    assert inserted == [
        ("delegated-live-1", live_row),
        ("delegated-live-2", live_row),
    ]
    assert [msg["role"] for msg in state.messages] == [
        "user",
        "assistant",
        "assistant",
        "user",
    ]
    assert state.messages[1]["agent_run_ids"] == ["delegated-live-1"]
    assert state.messages[2]["agent_run_ids"] == ["delegated-live-2"]
    assert state.messages[3]["queued_control"]["id"] == "queued-live"

    final = {"role": "assistant", "content": "one final parent response"}
    streaming._insert_assistant_before_future_queued_turns(state.messages, final)

    assert [msg["content"] for msg in state.messages] == [
        "first",
        "Started a Subagent agent for this. I'll keep this thread updated.",
        "Started a Subagent agent for this. I'll keep this thread updated.",
        "one final parent response",
        "queued follow-up",
    ]
    assert sum(msg is final for msg in state.messages) == 1


def test_live_agent_run_card_tracks_rendered_ids_once(tmp_path, monkeypatch):
    _agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)

    class DummySlot:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    rendered: list[list[str]] = []
    monkeypatch.setattr(
        streaming,
        "render_agent_run_cards",
        lambda run_ids: rendered.append(list(run_ids)) or True,
    )
    gen = SimpleNamespace(
        detached=False,
        tool_col=DummySlot(),
        live_agent_run_ids=set(),
    )

    assert streaming._render_live_agent_run_card(gen, {"id": "run-live"}) is True
    assert streaming._render_live_agent_run_card(gen, {"id": "run-live"}) is False
    assert rendered == [["run-live"]]
    assert gen.live_agent_run_ids == {"run-live"}


def test_agent_tool_result_already_live_detects_rendered_run(tmp_path, monkeypatch):
    _agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)

    gen = SimpleNamespace(live_agent_run_ids={"run-live"})
    result = {
        "name": "delegate_work",
        "content": json.dumps({
            "message": "Child Agent started.",
            "run": {"id": "run-live", "status": "running"},
        }),
    }
    other = {
        "name": "delegate_work",
        "content": json.dumps({
            "message": "Child Agent started.",
            "run": {"id": "run-other", "status": "running"},
        }),
    }

    assert streaming._agent_tool_result_already_live(gen, result)
    assert not streaming._agent_tool_result_already_live(gen, other)


def test_delegated_wait_mode_skips_async_completion_summary(tmp_path, monkeypatch):
    agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)

    agent_runs.create_agent_run(
        run_id="delegated-wait",
        kind="subagent",
        status="running",
        parent_thread_id="parent",
        display_name="Wait Child",
    )
    messages = [
        {
            "role": "assistant",
            "content": "Started wait child.",
            "agent_run_ids": ["delegated-wait"],
            "agent_lifecycle": {
                "kind": "delegated_agent_spawn",
                "run_id": "delegated-wait",
                "wait_mode": True,
                "completion_summary_emitted": False,
            },
        }
    ]

    agent_runs.finish_agent_run("delegated-wait", "completed", summary="Parent will summarize this.")

    assert streaming._append_direct_agent_completion_messages(messages, ["delegated-wait"]) is False
    assert len(messages) == 1
    assert messages[0]["agent_lifecycle"]["completion_summary_emitted"] is False


def test_visible_agent_tool_results_are_filtered_after_card_is_visible(tmp_path, monkeypatch):
    _agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)

    messages = [
        {
            "role": "assistant",
            "agent_run_ids": ["run-1"],
            "agent_lifecycle": {"kind": "delegated_agent_spawn", "run_id": "run-1"},
        }
    ]
    duplicate_agent_result = {
        "name": "delegate_work",
        "content": json.dumps({
            "message": "Child Agent completed.",
            "run": {"id": "run-1", "status": "completed"},
        }),
    }
    unseen_agent_result = {
        "name": "delegate_work",
        "content": json.dumps({
            "message": "Child Agent started.",
            "run": {"id": "run-2", "status": "queued"},
        }),
    }
    other_result = {"name": "calculator", "content": "4"}

    filtered, removed = streaming._filter_visible_agent_tool_results(
        messages,
        [duplicate_agent_result, unseen_agent_result, other_result],
    )

    assert removed is True
    assert filtered == [unseen_agent_result, other_result]
    assert streaming._ordered_agent_run_ids_from_tool_results([
        duplicate_agent_result,
        unseen_agent_result,
        duplicate_agent_result,
    ]) == ["run-1", "run-2"]


def test_delegated_agent_run_card_refreshes_without_completion_summary(tmp_path, monkeypatch):
    agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)

    run = agent_runs.create_agent_run(
        run_id="delegated-refresh",
        kind="subagent",
        status="running",
        parent_thread_id="parent",
        display_name="Refresh Child",
        profile_id="builtin:review",
        profile_snapshot_json={"slug": "review", "display_name": "Review"},
    )
    messages = [
        {
            "role": "assistant",
            "content": "Child started.",
            "agent_run_ids": ["delegated-refresh"],
            "agent_run_refresh_key": streaming._agent_run_card_refresh_key(run),
        }
    ]

    agent_runs.finish_agent_run("delegated-refresh", "completed", summary="Done.")
    changed, terminal = streaming._update_direct_agent_refresh_keys(messages, ["delegated-refresh"])

    assert changed is True
    assert terminal is True
    assert "completed" in messages[0]["agent_run_refresh_key"]
    assert streaming._append_direct_agent_completion_messages(messages, ["delegated-refresh"]) is False
    assert len(messages) == 1


def test_interrupt_changes_model_setting_detects_status_tool_payload(tmp_path, monkeypatch):
    _agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)

    assert streaming._interrupt_changes_model_setting({
        "tool": "row_bot_update_setting",
        "args": {"setting": "model", "value": "openai:gpt-5"},
    })
    assert streaming._interrupt_changes_model_setting({
        "tool": "row_bot_update_setting",
        "args": {"setting": "thread_model", "value": "anthropic:fable"},
    })
    assert streaming._interrupt_changes_model_setting({
        "tool": "row_bot_update_setting",
        "args": {"setting": "default_model", "value": "codex:gpt-5.5"},
    })
    assert streaming._interrupt_changes_model_setting([
        {"tool": "row_bot_update_setting", "args": {"setting": "voice", "value": "off"}},
        {"tool": "row_bot_update_setting", "args": {"setting": "model", "value": "anthropic:opus"}},
    ])
    assert not streaming._interrupt_changes_model_setting({
        "tool": "row_bot_update_setting",
        "args": {"setting": "voice", "value": "on"},
    })
    assert not streaming._interrupt_changes_model_setting({
        "tool": "other_tool",
        "args": {"setting": "model", "value": "openai:gpt-5"},
    })


def test_tool_result_changes_model_setting_detects_auto_success(tmp_path, monkeypatch):
    _agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)

    assert streaming._tool_result_changes_model_setting(
        "row_bot_update_setting",
        "Active model changed to: model:codex:gpt-5.4",
    )
    assert streaming._tool_result_changes_model_setting(
        "row_bot_update_setting",
        "Thread model override changed to: model:anthropic:claude-fable-5",
    )
    assert streaming._tool_result_changes_model_setting(
        "row_bot_update_setting",
        "Thread model override cleared; using global default: model:codex:gpt-5.5",
    )
    assert streaming._tool_result_changes_model_setting(
        "row_bot_update_setting",
        "Global default model changed to: model:codex:gpt-5.5",
    )
    assert not streaming._tool_result_changes_model_setting(
        "row_bot_update_setting",
        "Model change cancelled.",
    )
    assert not streaming._tool_result_changes_model_setting(
        "row_bot_update_setting",
        "Failed to change model: boom",
    )
    assert not streaming._tool_result_changes_model_setting(
        "row_bot_status",
        "Active model changed to: model:codex:gpt-5.4",
    )


def test_new_agent_request_is_not_child_steering(tmp_path, monkeypatch):
    _agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)

    assert streaming._looks_like_new_agent_request("also use another agent to write the PDF")
    assert not streaming._looks_like_new_agent_request("also make the essay more technical")


def test_single_steering_target_is_conservative(tmp_path, monkeypatch):
    agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)

    assert streaming._single_steering_target("parent") is None

    first = agent_runs.create_agent_run(
        run_id="target-1",
        kind="subagent",
        status="queued",
        parent_thread_id="parent",
        display_name="Queued Writer",
    )
    assert streaming._single_steering_target("parent")["id"] == first["id"]

    agent_runs.create_agent_run(
        run_id="target-2",
        kind="subagent",
        status="queued",
        parent_thread_id="parent",
        display_name="Second Writer",
    )
    assert streaming._single_steering_target("parent") is None


def test_send_message_does_not_shadow_queue_module(tmp_path, monkeypatch):
    _agent_runs, streaming, _transcript = _fresh_modules(tmp_path, monkeypatch)

    tree = ast.parse(inspect.getsource(streaming.send_message))
    assigned_names: set[str] = set()

    class _AssignedNameVisitor(ast.NodeVisitor):
        def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
            if isinstance(node.ctx, ast.Store):
                assigned_names.add(node.id)

    _AssignedNameVisitor().visit(tree)

    assert "queue" not in assigned_names
