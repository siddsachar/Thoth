from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from pathlib import Path


def _fresh_agent_approval_modules(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(data_dir))
    for name in (
        "row_bot.tasks",
        "row_bot.threads",
        "row_bot.agent_profiles",
        "row_bot.agent_runs",
        "row_bot.agent_context",
        "row_bot.agent_runner",
    ):
        sys.modules.pop(name, None)

    import row_bot.tasks as tasks
    import row_bot.threads as threads
    import row_bot.agent_runs as agent_runs
    import row_bot.agent_runner as agent_runner

    tasks = importlib.reload(tasks)
    threads = importlib.reload(threads)
    agent_runs = importlib.reload(agent_runs)
    agent_runner = importlib.reload(agent_runner)
    return tasks, threads, agent_runs, agent_runner


def _approval_columns(db_path: str) -> set[str]:
    with sqlite3.connect(Path(db_path)) as conn:
        return {row[1] for row in conn.execute("PRAGMA table_info(approval_requests)")}


def test_approval_schema_has_agent_routing_columns(tmp_path, monkeypatch):
    tasks, _threads, _agent_runs, _agent_runner = _fresh_agent_approval_modules(
        tmp_path,
        monkeypatch,
    )

    tasks.ensure_task_schema(force=True)

    assert {
        "agent_run_id",
        "resume_kind",
        "source_label",
        "source_thread_id",
        "parent_thread_id",
        "approval_payload_json",
    } <= _approval_columns(tasks._DB_PATH)


def test_agent_interrupt_creates_approval_and_resume_routes_to_agent_runner(
    tmp_path,
    monkeypatch,
):
    tasks, threads, agent_runs, agent_runner = _fresh_agent_approval_modules(
        tmp_path,
        monkeypatch,
    )
    parent_thread_id = threads.create_thread("Parent", approval_mode="approve")

    def fake_interrupt(prompt, enabled_tool_names, config, *, stop_event):
        return {
            "type": "interrupt",
            "interrupts": [
                {
                    "id": "interrupt-1",
                    "tool": "run_command",
                    "label": "Run shell command",
                    "approval_reason": "Check the project state.",
                    "args": {"command": "git status"},
                }
            ],
        }

    def fake_resume(enabled_tool_names, config, approved, *, interrupt_ids=None, stop_event):
        assert approved is True
        assert interrupt_ids == ["interrupt-1"]
        return "approved child result"

    monkeypatch.setattr(agent_runner, "_invoke_agent", fake_interrupt)
    monkeypatch.setattr(agent_runner, "_resume_invoke_agent", fake_resume)

    run = agent_runner.spawn_agent_run(
        "Run a gated action.",
        parent_thread_id=parent_thread_id,
        profile="worker",
        enabled_tool_names=["shell"],
        wait=True,
    )

    assert run["status"] == "waiting_approval"
    pending = tasks.get_pending_approvals()
    approval = next(row for row in pending if row["agent_run_id"] == run["id"])
    assert approval["resume_kind"] == "agent_run"
    assert approval["task_id"] == ""
    assert approval["step_id"] == "agent_interrupt"
    assert approval["source_thread_id"] == run["thread_id"]
    assert approval["parent_thread_id"] == parent_thread_id
    payload = json.loads(approval["approval_payload_json"])
    assert payload["reason"] == "Check the project state."
    assert payload["raw_action"] == "git status"
    assert "needs approval" in approval["message"]
    assert "Check the project state." in approval["message"]

    parent_messages = threads.get_latest_checkpoint_messages(parent_thread_id)
    approval_messages = [
        message
        for message in parent_messages
        if (
            getattr(message, "additional_kwargs", {})
            .get("row_bot_ui", {})
            .get("approval_request_id")
            == approval["id"]
        )
    ]
    assert len(approval_messages) == 1
    metadata = approval_messages[0].additional_kwargs["row_bot_ui"]
    assert metadata["agent_approval_for"] == run["id"]
    assert metadata["agent_run_ids"] == [run["id"]]
    assert metadata["approval_resume_token"] == approval["resume_token"]

    from row_bot.channels.thread_notifications import notify_agent_run_approval

    assert notify_agent_run_approval(approval["id"]) is True
    assert len(
        [
            message
            for message in threads.get_latest_checkpoint_messages(parent_thread_id)
            if (
                getattr(message, "additional_kwargs", {})
                .get("row_bot_ui", {})
                .get("approval_request_id")
                == approval["id"]
            )
        ]
    ) == 1

    assert tasks.respond_to_approval(approval["resume_token"], True) is True
    final = agent_runner.wait_for_agent_run(run["id"], timeout=2.0)

    assert final["status"] == "completed"
    assert final["summary"] == "approved child result"
    stored = agent_runs.get_agent_run(run["id"])
    assert stored["resume_state_json"]["approval_id"] == approval["id"]
    events = {event["type"] for event in agent_runs.get_agent_events(run["id"])}
    assert {"approval.requested", "approval.resolved", "run.completed"} <= events


def test_agent_resume_uses_langgraph_interrupt_ids_for_multi_interrupts(
    tmp_path,
    monkeypatch,
):
    tasks, threads, _agent_runs, agent_runner = _fresh_agent_approval_modules(
        tmp_path,
        monkeypatch,
    )
    parent_thread_id = threads.create_thread("Parent", approval_mode="approve")

    def fake_interrupt(prompt, enabled_tool_names, config, *, stop_event):
        return {
            "type": "interrupt",
            "interrupts": [
                {"__interrupt_id": "write-a", "id": "legacy-a", "tool": "filesystem"},
                {"__interrupt_id": "write-b", "tool": "filesystem"},
            ],
        }

    def fake_resume(enabled_tool_names, config, approved, *, interrupt_ids=None, stop_event):
        assert approved is True
        assert interrupt_ids == ["write-a", "write-b"]
        return "approved child result"

    monkeypatch.setattr(agent_runner, "_invoke_agent", fake_interrupt)
    monkeypatch.setattr(agent_runner, "_resume_invoke_agent", fake_resume)

    run = agent_runner.spawn_agent_run(
        "Edit two files.",
        parent_thread_id=parent_thread_id,
        profile="worker",
        enabled_tool_names=["filesystem"],
        wait=True,
    )
    approval = next(row for row in tasks.get_pending_approvals() if row["agent_run_id"] == run["id"])

    assert tasks.respond_to_approval(approval["resume_token"], True) is True
    final = agent_runner.wait_for_agent_run(run["id"], timeout=2.0)

    assert final["status"] == "completed"
    assert final["summary"] == "approved child result"


def test_agent_resume_error_marks_run_failed(tmp_path, monkeypatch):
    tasks, threads, _agent_runs, agent_runner = _fresh_agent_approval_modules(
        tmp_path,
        monkeypatch,
    )
    parent_thread_id = threads.create_thread("Parent", approval_mode="approve")

    monkeypatch.setattr(
        agent_runner,
        "_invoke_agent",
        lambda prompt, enabled_tool_names, config, *, stop_event: {
            "type": "interrupt",
            "interrupts": [{"__interrupt_id": "write-a", "tool": "filesystem"}],
        },
    )

    def fail_resume(*_args, **_kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(agent_runner, "_resume_invoke_agent", fail_resume)

    run = agent_runner.spawn_agent_run(
        "Resume should fail.",
        parent_thread_id=parent_thread_id,
        profile="worker",
        enabled_tool_names=["filesystem"],
        wait=True,
    )
    approval = next(row for row in tasks.get_pending_approvals() if row["agent_run_id"] == run["id"])

    assert tasks.respond_to_approval(approval["resume_token"], True) is True
    final = agent_runner.wait_for_agent_run(run["id"], timeout=2.0)

    assert final["status"] == "failed"
    assert "provider exploded" in final["error"]


def test_agent_approval_denial_stops_child_run(tmp_path, monkeypatch):
    tasks, threads, _agent_runs, agent_runner = _fresh_agent_approval_modules(
        tmp_path,
        monkeypatch,
    )
    parent_thread_id = threads.create_thread("Parent", approval_mode="approve")

    monkeypatch.setattr(
        agent_runner,
        "_invoke_agent",
        lambda prompt, enabled_tool_names, config, *, stop_event: {
            "type": "interrupt",
            "interrupts": [{"tool": "shell"}],
        },
    )

    run = agent_runner.spawn_agent_run(
        "Try a gated action.",
        parent_thread_id=parent_thread_id,
        profile="worker",
        enabled_tool_names=["shell"],
        wait=True,
    )
    approval = next(row for row in tasks.get_pending_approvals() if row["agent_run_id"] == run["id"])

    assert tasks.respond_to_approval(approval["resume_token"], False) is True
    final = agent_runner.wait_for_agent_run(run["id"], timeout=0.5)

    assert final["status"] == "stopped"
    assert final["status_message"] == "Approval denied by user"


def test_legacy_workflow_approval_still_routes_to_pipeline(tmp_path, monkeypatch):
    tasks, _threads, _agent_runs, _agent_runner = _fresh_agent_approval_modules(
        tmp_path,
        monkeypatch,
    )
    calls = []
    monkeypatch.setattr(
        tasks,
        "_resume_pipeline",
        lambda resume_token, approved=True: calls.append((resume_token, approved)),
    )

    resume_token, _approval_id = tasks.create_approval_request(
        run_id="workflow-run",
        task_id="workflow-task",
        step_id="step-1",
        message="Workflow approval",
    )

    assert tasks.respond_to_approval(resume_token, True) is True
    assert calls == [(resume_token, True)]
