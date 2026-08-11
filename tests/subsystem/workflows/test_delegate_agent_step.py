from __future__ import annotations

import contextvars
import importlib
import sys
import types
from pathlib import Path

import pytest


pytestmark = pytest.mark.subsystem


def _fresh_modules(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    for name in (
        "row_bot.tasks",
        "row_bot.threads",
        "row_bot.agent_profiles",
        "row_bot.agent_runs",
        "row_bot.agent_runner",
        "row_bot.agent_orchestrator",
        "row_bot.developer.worktrees",
        "row_bot.agent",
    ):
        sys.modules.pop(name, None)

    import row_bot.tasks as tasks
    import row_bot.threads as threads
    import row_bot.agent_profiles as agent_profiles
    import row_bot.agent_runs as agent_runs
    import row_bot.agent_runner as agent_runner
    import row_bot.developer.worktrees as worktrees

    tasks = importlib.reload(tasks)
    threads = importlib.reload(threads)
    agent_profiles = importlib.reload(agent_profiles)
    agent_runs = importlib.reload(agent_runs)
    agent_runner = importlib.reload(agent_runner)
    worktrees = importlib.reload(worktrees)
    return tasks, threads, agent_profiles, agent_runs, agent_runner, worktrees


def _install_fake_agent(monkeypatch, calls: list[dict]) -> None:
    fake_agent = types.ModuleType("row_bot.agent")

    class TaskStoppedError(Exception):
        pass

    def invoke_agent(prompt, enabled_tool_names, config, stop_event=None):
        calls.append(
            {
                "prompt": prompt,
                "tools": list(enabled_tool_names),
                "config": config,
                "stop_event": stop_event,
            }
        )
        return "child result"

    fake_agent.TaskStoppedError = TaskStoppedError
    fake_agent.RECURSION_LIMIT_TASK = 12
    fake_agent.invoke_agent = invoke_agent
    fake_agent.resume_invoke_agent = lambda *args, **kwargs: "resumed"
    fake_agent.repair_orphaned_tool_calls = lambda *args, **kwargs: None
    fake_agent._approval_mode_var = contextvars.ContextVar("approval_mode", default="approve")
    fake_agent._background_workflow_var = contextvars.ContextVar(
        "background_workflow",
        default=False,
    )
    fake_agent._persistent_thread_var = contextvars.ContextVar(
        "persistent_thread",
        default=False,
    )
    monkeypatch.setitem(sys.modules, "row_bot.agent", fake_agent)


def _run_workflow_synchronously(monkeypatch, tasks) -> None:
    class ImmediateThread:
        def __init__(self, target, *args, **kwargs):
            self._target = target
            self._args = tuple(kwargs.get("args") or ())
            self._kwargs = dict(kwargs.get("kwargs") or {})
            self._alive = False

        def start(self):
            self._alive = True
            try:
                self._target(*self._args, **self._kwargs)
            finally:
                self._alive = False

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return self._alive

    monkeypatch.setattr(tasks.threading, "Thread", ImmediateThread)


def test_delegate_agent_step_waits_and_records_child_run(tmp_path, monkeypatch) -> None:
    tasks, threads, _profiles, agent_runs, _agent_runner, _worktrees = _fresh_modules(
        tmp_path,
        monkeypatch,
    )
    calls: list[dict] = []
    _install_fake_agent(monkeypatch, calls)
    _run_workflow_synchronously(monkeypatch, tasks)
    task_id = tasks.create_task(
        "Delegate smoke",
        steps=[
            {
                "type": "delegate_agent",
                "objective": "Summarize {{prev_output}}",
                "context": "Workflow id {{task_id}}",
                "profile": "worker",
                "wait": True,
            },
        ],
        channels=[],
        apply_default_skills=False,
    )
    thread_id = threads.create_thread("Workflow run", thread_id="workflow-delegate-run")

    tasks.run_task_background(task_id, thread_id, ["row_bot_status"], notification=False)

    child_runs = agent_runs.list_agent_runs(
        parent_thread_id=thread_id,
        kind="subagent",
        limit=10,
    )
    assert len(child_runs) == 1
    child = child_runs[0]
    assert child["status"] == "completed"
    assert child["summary"] == "child result"
    assert child["parent_thread_id"] == thread_id
    assert calls[0]["config"]["configurable"]["runtime_surface"] == "agent_child"
    assert calls[-1]["config"]["configurable"]["runtime_surface"] == "workflow"
    assert calls[-1]["config"]["configurable"]["thread_event_context"] is True


def test_delegate_agent_wait_pauses_and_resumes_after_child_approval(
    tmp_path,
    monkeypatch,
) -> None:
    tasks, threads, _profiles, agent_runs, _agent_runner, _worktrees = _fresh_modules(
        tmp_path,
        monkeypatch,
    )
    _run_workflow_synchronously(monkeypatch, tasks)
    fake_agent = types.ModuleType("row_bot.agent")

    class TaskStoppedError(Exception):
        pass

    fake_agent.TaskStoppedError = TaskStoppedError
    fake_agent.RECURSION_LIMIT_TASK = 12
    def invoke_agent(_prompt, _tools, config, **_kwargs):
        if config["configurable"]["runtime_surface"] == "agent_child":
            return {
                "type": "interrupt",
                "interrupts": [{"tool": "shell", "description": "Needs approval"}],
            }
        return "Parent finalized the approved child result."

    fake_agent.invoke_agent = invoke_agent
    fake_agent.resume_invoke_agent = lambda *args, **kwargs: "resumed"
    fake_agent.repair_orphaned_tool_calls = lambda *args, **kwargs: None
    fake_agent._approval_mode_var = contextvars.ContextVar("approval_mode", default="approve")
    fake_agent._background_workflow_var = contextvars.ContextVar("background_workflow", default=False)
    fake_agent._persistent_thread_var = contextvars.ContextVar("persistent_thread", default=False)
    monkeypatch.setitem(sys.modules, "row_bot.agent", fake_agent)

    task_id = tasks.create_task(
        "Delegate approval wait",
        steps=[
            {
                "type": "delegate_agent",
                "objective": "Run a gated command",
                "profile": "worker",
                "wait": True,
                "timeout_seconds": 0.01,
            },
        ],
        channels=[],
        apply_default_skills=False,
    )
    thread_id = threads.create_thread("Workflow run", thread_id="workflow-approval-run")

    tasks.run_task_background(task_id, thread_id, ["shell"], notification=False)

    child_runs = agent_runs.list_agent_runs(
        parent_thread_id=thread_id,
        kind="subagent",
        limit=10,
    )
    assert len(child_runs) == 1
    child_run_id = child_runs[0]["id"]
    assert child_runs[0]["status"] == "waiting_approval"
    history = tasks.get_run_history(task_id, limit=1)
    assert history[0]["status"] == "paused"
    assert "waiting for approval" in history[0]["status_message"]

    approvals = tasks.get_pending_approvals()
    assert len(approvals) == 1
    assert tasks.respond_to_approval(approvals[0]["resume_token"], True, source="test")

    child = agent_runs.get_agent_run(child_run_id)
    assert child["status"] == "completed"
    assert child["summary"] == "resumed"
    history = tasks.get_run_history(task_id, limit=1)
    assert history[0]["status"] == "completed"


def test_delegate_agent_wait_denial_stops_parent_workflow(tmp_path, monkeypatch) -> None:
    tasks, threads, _profiles, agent_runs, _agent_runner, _worktrees = _fresh_modules(
        tmp_path,
        monkeypatch,
    )
    _run_workflow_synchronously(monkeypatch, tasks)
    fake_agent = types.ModuleType("row_bot.agent")

    class TaskStoppedError(Exception):
        pass

    fake_agent.TaskStoppedError = TaskStoppedError
    fake_agent.RECURSION_LIMIT_TASK = 12
    def invoke_agent(_prompt, _tools, config, **_kwargs):
        if config["configurable"]["runtime_surface"] == "agent_child":
            return {
                "type": "interrupt",
                "interrupts": [{"tool": "shell", "description": "Needs approval"}],
            }
        return "Parent finalized after the child was stopped."

    fake_agent.invoke_agent = invoke_agent
    fake_agent.resume_invoke_agent = lambda *args, **kwargs: "should not run"
    fake_agent.repair_orphaned_tool_calls = lambda *args, **kwargs: None
    fake_agent._approval_mode_var = contextvars.ContextVar("approval_mode", default="approve")
    fake_agent._background_workflow_var = contextvars.ContextVar("background_workflow", default=False)
    fake_agent._persistent_thread_var = contextvars.ContextVar("persistent_thread", default=False)
    monkeypatch.setitem(sys.modules, "row_bot.agent", fake_agent)

    task_id = tasks.create_task(
        "Delegate approval denied",
        steps=[
            {
                "type": "delegate_agent",
                "objective": "Run a gated command",
                "profile": "worker",
                "wait": True,
            },
        ],
        channels=[],
        apply_default_skills=False,
    )
    thread_id = threads.create_thread("Workflow run", thread_id="workflow-denial-run")

    tasks.run_task_background(task_id, thread_id, ["shell"], notification=False)

    approvals = tasks.get_pending_approvals()
    assert len(approvals) == 1
    assert tasks.respond_to_approval(approvals[0]["resume_token"], False, source="test")

    child_runs = agent_runs.list_agent_runs(
        parent_thread_id=thread_id,
        kind="subagent",
        limit=10,
    )
    assert len(child_runs) == 1
    assert child_runs[0]["status"] == "stopped"
    history = tasks.get_run_history(task_id, limit=1)
    assert history[0]["status"] == "stopped"
    assert "Child Agent" in history[0]["status_message"]


def test_delegate_agent_wait_child_error_fails_parent_workflow(tmp_path, monkeypatch) -> None:
    tasks, threads, _profiles, agent_runs, _agent_runner, _worktrees = _fresh_modules(
        tmp_path,
        monkeypatch,
    )
    _run_workflow_synchronously(monkeypatch, tasks)
    fake_agent = types.ModuleType("row_bot.agent")

    class TaskStoppedError(Exception):
        pass

    fake_agent.TaskStoppedError = TaskStoppedError
    fake_agent.RECURSION_LIMIT_TASK = 12
    fake_agent.invoke_agent = lambda *args, **kwargs: {
        "type": "error",
        "error": "boom",
    }
    fake_agent.resume_invoke_agent = lambda *args, **kwargs: "resumed"
    fake_agent.repair_orphaned_tool_calls = lambda *args, **kwargs: None
    fake_agent._approval_mode_var = contextvars.ContextVar("approval_mode", default="approve")
    fake_agent._background_workflow_var = contextvars.ContextVar("background_workflow", default=False)
    fake_agent._persistent_thread_var = contextvars.ContextVar("persistent_thread", default=False)
    monkeypatch.setitem(sys.modules, "row_bot.agent", fake_agent)

    task_id = tasks.create_task(
        "Delegate child error",
        steps=[
            {
                "type": "delegate_agent",
                "objective": "Fail child",
                "profile": "worker",
                "wait": True,
            },
        ],
        channels=[],
        apply_default_skills=False,
    )
    thread_id = threads.create_thread("Workflow run", thread_id="workflow-child-error-run")

    tasks.run_task_background(task_id, thread_id, ["shell"], notification=False)

    child_runs = agent_runs.list_agent_runs(
        parent_thread_id=thread_id,
        kind="subagent",
        limit=10,
    )
    assert len(child_runs) == 1
    assert child_runs[0]["status"] == "failed"
    history = tasks.get_run_history(task_id, limit=1)
    assert history[0]["status"] == "failed"
    assert "finished with status: failed" in history[0]["status_message"]


def test_delegate_agent_step_can_start_background_and_wait_for_children(tmp_path, monkeypatch) -> None:
    tasks, threads, _profiles, agent_runs, _agent_runner, _worktrees = _fresh_modules(
        tmp_path,
        monkeypatch,
    )
    calls: list[dict] = []
    _install_fake_agent(monkeypatch, calls)
    _run_workflow_synchronously(monkeypatch, tasks)
    task_id = tasks.create_task(
        "Parallel delegate smoke",
        steps=[
            {
                "type": "delegate_agent",
                "objective": "Inspect docs",
                "profile": "worker",
                "return_mode": "background",
                "wait": False,
            },
            {"type": "wait_for_agents", "timeout_seconds": 30},
        ],
        channels=[],
        apply_default_skills=False,
    )
    thread_id = threads.create_thread("Workflow run", thread_id="workflow-bg-run")

    tasks.run_task_background(task_id, thread_id, ["row_bot_status"], notification=False)

    child_runs = agent_runs.list_agent_runs(
        parent_thread_id=thread_id,
        kind="subagent",
        limit=10,
    )
    assert len(child_runs) == 1
    assert child_runs[0]["status"] == "completed"


def test_delegate_agent_step_uses_worktree_workspace(tmp_path, monkeypatch) -> None:
    tasks, threads, _profiles, agent_runs, _agent_runner, worktrees = _fresh_modules(
        tmp_path,
        monkeypatch,
    )
    calls: list[dict] = []
    _install_fake_agent(monkeypatch, calls)
    _run_workflow_synchronously(monkeypatch, tasks)

    def fake_allocate(
        run_id,
        parent_workspace_id,
        *,
        objective="",
        branch_slug="",
        seed_mode="current_changes",
        parent_thread_id="",
    ):
        return {
            "status": "active",
            "owner_kind": "agent_run",
            "owner_id": run_id,
            "project_workspace_id": parent_workspace_id,
            "worktree_workspace_id": f"dev_worktree_{run_id}",
            "worktree_path": str(tmp_path / f"repo-wt-{run_id}"),
            "branch_name": f"row-bot/{run_id}-delegate",
            "metadata_json": {"seeded_from_current_changes": False},
        }

    monkeypatch.setattr(worktrees, "allocate_agent_worktree", fake_allocate)
    task_id = tasks.create_task(
        "Delegate Worktree",
        steps=[
            {
                "type": "delegate_agent",
                "objective": "Edit safely",
                "profile": "worker",
                "editing_safety": "worktree",
                "use_worktree": True,
                "wait": True,
            },
        ],
        channels=[],
        apply_default_skills=False,
    )
    thread_id = threads.create_thread(
        "Workflow run",
        thread_id="workflow-worktree-run",
        developer_workspace_id="dev_parent",
    )

    tasks.run_task_background(task_id, thread_id, ["row_bot_status"], notification=False)

    child_runs = agent_runs.list_agent_runs(
        parent_thread_id=thread_id,
        kind="subagent",
        limit=10,
    )
    assert len(child_runs) == 1
    child = child_runs[0]
    assert child["workspace_mode"] == "worktree"
    assert child["workspace_id"].startswith("dev_worktree_")
    assert threads._get_thread_developer_workspace(child["thread_id"]) == child["workspace_id"]


@pytest.mark.parametrize("on_error", ["stop", "skip"])
def test_delegate_agent_worktree_requires_developer_workspace_even_when_skippable(
    tmp_path,
    monkeypatch,
    on_error,
) -> None:
    tasks, threads, _profiles, agent_runs, _agent_runner, _worktrees = _fresh_modules(
        tmp_path,
        monkeypatch,
    )
    calls: list[dict] = []
    _install_fake_agent(monkeypatch, calls)
    _run_workflow_synchronously(monkeypatch, tasks)
    task_id = tasks.create_task(
        f"Delegate Worktree Missing Workspace {on_error}",
        steps=[
            {
                "type": "delegate_agent",
                "objective": "Edit safely",
                "profile": "worker",
                "editing_safety": "worktree",
                "use_worktree": True,
                "wait": True,
                "on_error": on_error,
            },
        ],
        channels=[],
        apply_default_skills=False,
    )
    thread_id = threads.create_thread(
        "Workflow run",
        thread_id=f"workflow-worktree-missing-{on_error}",
    )

    tasks.run_task_background(task_id, thread_id, ["row_bot_status"], notification=False)

    child_runs = agent_runs.list_agent_runs(
        parent_thread_id=thread_id,
        kind="subagent",
        limit=10,
    )
    history = tasks.get_run_history(task_id, limit=1)
    assert child_runs == []
    assert calls == []
    assert history[0]["status"] == "failed"
    assert "Worktree requires a git-backed Developer workspace" in history[0]["status_message"]
