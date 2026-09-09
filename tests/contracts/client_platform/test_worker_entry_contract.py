"""Independent producer ownership checks around rejected and entered workers."""
from __future__ import annotations

from contextlib import contextmanager
import threading

import pytest

from row_bot.conversation_resources import ResourceError
from row_bot.runtime.executions import GenerationRuntimeRegistry, current_execution


def test_rejected_entry_keeps_registered_owner_until_callback_returns():
    registry = GenerationRuntimeRegistry()
    stop = threading.Event()
    stop.set()
    entered = threading.Event()
    release = threading.Event()
    targets = []
    callbacks = []

    def reject(exc):
        callbacks.append(exc)
        assert current_execution() is handle
        entered.set()
        assert release.wait(5), "The independent cleanup barrier was not released"

    worker = registry.thread(target=lambda: targets.append("entered"),
                             conversation_id="synthetic-rejected-entry", stop_event=stop,
                             domain="contract", on_entry_failure=reject)
    handle = registry.active("synthetic-rejected-entry")[0]
    worker.start()
    try:
        assert entered.wait(5), "Rejected entry did not call its cleanup callback"
        assert registry.active("synthetic-rejected-entry") == (handle,)
        assert not handle.producer_done.is_set()
        assert not handle.cleanup_complete
        assert targets == []
        assert len(callbacks) == 1 and isinstance(callbacks[0], InterruptedError)
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert handle.producer_done.is_set() and handle.cleanup_complete
    assert handle.status == "stopped"
    assert registry.active("synthetic-rejected-entry") == ()
    assert len(callbacks) == 1 and targets == []


def test_entered_target_failure_unwinds_before_finish_without_entry_callback(monkeypatch):
    from row_bot import conversation_resources

    registry = GenerationRuntimeRegistry()
    exiting = threading.Event()
    release = threading.Event()
    callbacks = []
    errors = []
    events = []

    @contextmanager
    def resources(conversation_id):
        assert conversation_id == "synthetic-entered-target"
        events.append("resource_entered")
        try:
            yield object()
        finally:
            assert current_execution() is handle
            events.append("resource_exit_started")
            exiting.set()
            assert release.wait(5), "The independent resource unwind was not released"
            events.append("resource_exit_finished")

    def target():
        assert current_execution() is handle
        events.append("target_entered")
        raise ResourceError("resource_unavailable")

    monkeypatch.setattr(conversation_resources, "execution_context", resources)
    monkeypatch.setattr(threading, "excepthook", lambda args: errors.append(args.exc_value))
    worker = registry.thread(target=target, conversation_id="synthetic-entered-target",
                             stop_event=threading.Event(), domain="contract", resource_context=True,
                             on_entry_failure=lambda exc: callbacks.append(exc))
    handle = registry.active("synthetic-entered-target")[0]
    worker.start()
    try:
        assert exiting.wait(5), "The entered target did not begin resource cleanup"
        assert registry.active("synthetic-entered-target") == (handle,)
        assert not handle.producer_done.is_set()
        assert not handle.cleanup_complete
        assert callbacks == []
        assert events == ["resource_entered", "target_entered", "resource_exit_started"]
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert handle.producer_done.is_set() and handle.cleanup_complete
    assert handle.status == "interrupted"
    assert registry.active("synthetic-entered-target") == ()
    assert events[-1] == "resource_exit_finished"
    assert callbacks == []
    assert len(errors) == 1 and isinstance(errors[0], ResourceError)


@pytest.mark.parametrize("operation", ["inheritance_rollback", "child_delete"])
def test_child_cleanup_preserves_parent_owned_worktree(tmp_path, monkeypatch, operation):
    from pathlib import Path
    import subprocess
    from types import SimpleNamespace
    from tests.test_agent_runner import _fresh_agent_runner_modules

    git_calls, unexpected_calls = [], []

    def fake_subprocess(command, **kwargs):
        # No executable is launched, including imports' optional CLI discovery.
        assert not kwargs.get("shell")
        assert isinstance(command, (list, tuple)) and command
        executable = Path(str(command[0])).name.lower()
        arguments = tuple(str(value) for value in command[1:])
        if executable in {"git", "git.exe"}:
            git_calls.append(arguments)
        elif not (executable in {"claude", "claude.exe"}
                  and arguments in {("--version",), ("auth", "status")}):
            unexpected_calls.append((executable, arguments))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_subprocess)
    runner, runs, _, _, threads = _fresh_agent_runner_modules(tmp_path, monkeypatch)
    from row_bot import conversation_resources as resources
    from row_bot.developer import storage, worktrees
    from row_bot.developer.state import DeveloperWorkspace

    project = tmp_path / "synthetic-project"
    project.mkdir()
    shared = tmp_path / ".row-bot-worktrees" / "synthetic-parent"
    shared.mkdir(parents=True)
    sentinel = shared / "retained.txt"
    sentinel.write_text("Synthetic parent-owned content", encoding="utf-8")
    storage.save_workspace(DeveloperWorkspace(id="parent-worktree", name="Parent resource", path=str(shared)))
    storage.save_workspace(DeveloperWorkspace(id="racing-resource", name="Concurrent resource", path=str(project)))
    parent = threads.create_thread("Synthetic parent", developer_workspace_id="parent-worktree")
    worktrees._insert_or_update(row_id="synthetic-parent-allocation", owner_kind="thread", owner_id=parent,
                               project_workspace_id="synthetic-project", project_path=str(project),
                               worktree_workspace_id="parent-worktree", worktree_path=str(shared),
                               status="active")
    # The stored allocation, workspace, thread, resource CAS and rollback
    # owners remain real; all subprocess boundaries stay intercepted.
    monkeypatch.setattr(worktrees, "worktree_diff_summary", lambda *args, **kwargs: {"ok": True, "dirty": False})
    original_inherit = resources.inherit_bindings
    races = []

    def racing_inherit(parent_id, child_id, **kwargs):
        assert parent_id == parent
        assert resources.list_bindings(child_id).bindings[0].resource_id == "parent-worktree"
        races.append((parent_id, child_id))
        resources.bind(parent_id, "workspace", "racing-resource", role="context", expected_revision=0)
        return original_inherit(parent_id, child_id, **kwargs)

    monkeypatch.setattr(runner, "_invoke_agent", lambda *args, **kwargs: pytest.fail("Rejected inheritance dispatched"))
    if operation == "inheritance_rollback":
        monkeypatch.setattr(resources, "inherit_bindings", racing_inherit)
        with pytest.raises(runner.AgentRunnerError, match="resources could not be inherited safely"):
            runner.spawn_agent_run("Synthetic child", parent_thread_id=parent, profile="review", workspace_mode="read_only")
        assert len(races) == 1
    else:
        from row_bot.thread_cleanup import delete_thread
        child = threads.create_thread("Synthetic completed child", thread_type="agent_child",
                                      developer_workspace_id="parent-worktree")
        completed_run = runs.create_agent_run(kind="subagent", status="completed", parent_thread_id=parent, thread_id=child)
        assert delete_thread(child).deleted

    assert threads._thread_exists(parent)
    assert storage.get_workspace("parent-worktree") is not None
    assert worktrees.get_worktree("thread", parent) is not None
    assert git_calls == [], "A rejected child must not ask Git to remove its parent's allocation"
    assert unexpected_calls == [], "Only explicitly faked read-only CLI discovery is expected"
    assert sentinel.read_text(encoding="utf-8") == "Synthetic parent-owned content"
    assert [row for row in threads._list_threads(include_details=True) if row[6] == "agent_child"] == []
    remaining_runs = runs.list_agent_runs(parent_thread_id=parent)
    if operation == "inheritance_rollback":
        assert remaining_runs == []
    else:
        # The completed run remains part of the surviving parent's audit.
        assert len(remaining_runs) == 1
        assert remaining_runs[0]["id"] == completed_run["id"]
        assert remaining_runs[0]["status"] == "completed"
    assert runner.list_active_agent_run_ids() == []
