"""A rejected worker entry finalizes its domain before releasing ownership."""
from __future__ import annotations

import sqlite3
import threading

import pytest

from tests.test_agent_runner import _fresh_agent_runner_modules, _workspace


def _defer_worker_starts(monkeypatch):
    original = threading.Thread.start
    workers = []
    def start(worker):
        if worker.name.startswith(("agent-run-", "agent-resume-", "task-", "graph-resume-")):
            workers.append(worker)
        else:
            original(worker)
    monkeypatch.setattr(threading.Thread, "start", start)
    return workers, original


def _reject_entry(reason, registry, threads, conversation_id):
    if reason == "stop":
        assert registry.stop(conversation_id)
    else:
        # The durable resource reader itself rejects an unavailable snapshot.
        with sqlite3.connect(threads.DB_PATH) as connection:
            connection.execute("UPDATE thread_meta SET resource_bindings_json='{' WHERE thread_id=?", (conversation_id,))


def test_entry_failure_keeps_registry_owned_until_domain_cleanup_returns():
    from row_bot.runtime.executions import GenerationRuntimeRegistry
    registry = GenerationRuntimeRegistry()
    stop, entered, release = (threading.Event() for _ in range(3))
    stop.set()
    def cleanup(_exc):
        entered.set()
        assert release.wait(5)
    worker = registry.thread(target=lambda: pytest.fail("Cancelled dispatch"), conversation_id="synthetic",
                             stop_event=stop, domain="agent", on_entry_failure=cleanup)
    handle = registry.active("synthetic")[0]
    worker.start()
    try:
        assert entered.wait(5)
        assert not handle.producer_done.is_set()
        assert registry.active("synthetic") == (handle,)
    finally:
        release.set()
        worker.join(5)
    assert handle.producer_done.is_set() and handle.cleanup_complete
    assert handle.status == "stopped"


def test_duplicate_registration_does_not_finalize_existing_domain_owner():
    from row_bot.runtime.executions import GenerationRuntimeRegistry
    registry = GenerationRuntimeRegistry()
    existing = registry.register("synthetic", domain="agent", domain_id="existing-run")
    callbacks = []
    try:
        with pytest.raises(ValueError, match="execution_already_active"):
            registry.thread(target=lambda: None, conversation_id="synthetic", domain="agent",
                            domain_id="existing-run", stop_event=threading.Event(), on_entry_failure=callbacks.append)
        assert callbacks == []
        assert registry.active("synthetic") == (existing,)
        assert not existing.producer_done.is_set()
    finally:
        registry.finish(existing)


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("reason", ["stop", "resource"])
def test_child_entry_rejection_cleans_actual_domain_without_dispatch(tmp_path, monkeypatch, resume, reason):
    runner, runs, _, _, threads = _fresh_agent_runner_modules(tmp_path, monkeypatch)
    from row_bot.runtime import executions
    registry = executions.GenerationRuntimeRegistry()
    monkeypatch.setattr(executions, "generation_registry", registry)
    monkeypatch.setattr(runner, "generation_registry", registry)
    parent = threads.create_thread("Synthetic parent")
    workers, start = _defer_worker_starts(monkeypatch)
    monkeypatch.setattr(runner, "_invoke_agent", lambda *a, **k: pytest.fail("Rejected child dispatched"))
    monkeypatch.setattr(runner, "_resume_invoke_agent", lambda *a, **k: pytest.fail("Rejected child resumed"))
    if resume:
        child = threads.create_thread("Synthetic paused child", thread_type="agent_child")
        run = runs.create_agent_run(kind="subagent", status="waiting_approval", parent_thread_id=parent, thread_id=child)
        runs.save_agent_resume_state(run["id"], {"config": {"configurable": {"thread_id": child}},
            "enabled_tool_names": [], "interrupts": [{"id": "synthetic-interrupt"}]}, status="waiting_approval")
        runner.resume_agent_run(run["id"], approved=True)
    else:
        run = runner.spawn_agent_run("Synthetic objective", parent_thread_id=parent, profile="review")
        child = run["thread_id"]
    handle = registry.active(child)[0]
    _reject_entry(reason, registry, threads, child)
    assert not handle.producer_done.is_set()
    assert run["id"] in runner.list_active_agent_run_ids()
    start(workers[0])
    workers[0].join(5)
    assert not workers[0].is_alive()
    assert handle.producer_done.is_set() and handle.cleanup_complete
    assert runs.get_agent_run(run["id"])["status"] == ("stopped" if reason == "stop" else "failed")
    assert runner.list_active_agent_run_ids() == []
    assert runner.child_dispatch_state()["active"] == 0
    assert runs.list_agent_write_locks() == []


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("reason", ["stop", "resource"])
def test_workflow_entry_rejection_records_actual_run_outcome(tmp_path, monkeypatch, resume, reason):
    _, _, _, _, threads = _fresh_agent_runner_modules(tmp_path, monkeypatch)
    from row_bot import agent, tasks
    from row_bot.runtime import executions
    registry = executions.GenerationRuntimeRegistry()
    monkeypatch.setattr(executions, "generation_registry", registry)
    conversation_id = threads.create_thread("Synthetic workflow")
    task_id = tasks.create_task("Synthetic task", prompts=["Synthetic input"], channels=[], apply_default_skills=False)
    monkeypatch.setattr(agent, "invoke_agent", lambda *a, **k: pytest.fail("Rejected workflow dispatched"))
    monkeypatch.setattr(agent, "resume_invoke_agent", lambda *a, **k: pytest.fail("Rejected workflow resumed"))
    workers, start = _defer_worker_starts(monkeypatch)
    if resume:
        run_id = tasks._record_run_start(task_id, conversation_id, 1)
        tasks._save_pipeline_state(run_id=run_id, task_id=task_id, thread_id=conversation_id,
            current_step_index=0, step_outputs={}, config={"configurable": {"thread_id": conversation_id}},
            resume_token="synthetic-resume", status="paused", graph_interrupted=True)
        tasks._resume_pipeline("synthetic-resume", approved=True)
    else:
        tasks.run_task_background(task_id, conversation_id, [], notification=False)
    handle = registry.active(conversation_id)[0]
    _reject_entry(reason, registry, threads, conversation_id)
    assert not handle.producer_done.is_set()
    start(workers[0])
    workers[0].join(5)
    assert not workers[0].is_alive()
    assert handle.producer_done.is_set() and handle.cleanup_complete
    assert tasks.get_run_history(task_id)[0]["status"] == ("stopped" if reason == "stop" else "failed")
    assert not tasks.get_running_tasks()


@pytest.mark.parametrize("failure", ["unavailable", "changed"])
def test_child_resource_inheritance_failure_rolls_back_only_new_child(tmp_path, monkeypatch, failure):
    runner, runs, _, _, threads = _fresh_agent_runner_modules(tmp_path, monkeypatch)
    from row_bot import conversation_resources as resources
    parent = threads.create_thread("Synthetic parent", developer_workspace_id="missing" if failure == "unavailable" else "")
    if failure == "changed":
        _workspace(tmp_path, "new-resource", "new-resource")
        original = resources.inherit_bindings
        def racing_inherit(parent_id, child_id, **kwargs):
            resources.bind(parent_id, "workspace", "new-resource", role="primary", expected_revision=0)
            return original(parent_id, child_id, **kwargs)
        monkeypatch.setattr(resources, "inherit_bindings", racing_inherit)
    monkeypatch.setattr(runner, "_invoke_agent", lambda *a, **k: pytest.fail("Rejected resources dispatched"))
    with pytest.raises(runner.AgentRunnerError, match="resources could not be inherited safely"):
        runner.spawn_agent_run("Synthetic child", parent_thread_id=parent, profile="review")
    assert threads._thread_exists(parent)
    assert [row for row in threads._list_threads(include_details=True) if row[6] == "agent_child"] == []
    assert runs.list_agent_runs(parent_thread_id=parent) == []
    assert runner.list_active_agent_run_ids() == []
    if failure == "changed":
        assert resources.list_bindings(parent).bindings[0].resource_id == "new-resource"


@pytest.mark.parametrize("domain", ["child", "workflow"])
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("failure", ["closed", "start"])
def test_rejected_start_finalizes_domain_admission_without_dispatch(tmp_path, monkeypatch, domain, resume, failure):
    runner, runs, _, _, threads = _fresh_agent_runner_modules(tmp_path, monkeypatch)
    from row_bot import agent, tasks
    from row_bot.runtime import executions
    registry = executions.GenerationRuntimeRegistry()
    monkeypatch.setattr(executions, "generation_registry", registry)
    monkeypatch.setattr(runner, "generation_registry", registry)
    conversation_id = threads.create_thread("Synthetic closed runtime")
    monkeypatch.setattr(runner, "_invoke_agent", lambda *a, **k: pytest.fail("Closed child dispatched"))
    monkeypatch.setattr(runner, "_resume_invoke_agent", lambda *a, **k: pytest.fail("Closed child resumed"))
    monkeypatch.setattr(agent, "invoke_agent", lambda *a, **k: pytest.fail("Closed workflow dispatched"))
    monkeypatch.setattr(agent, "resume_invoke_agent", lambda *a, **k: pytest.fail("Closed workflow resumed"))
    if failure == "closed":
        registry.shutdown()
    else:
        def failed_start(_worker):
            raise RuntimeError("cannot start new thread")
        monkeypatch.setattr(threading.Thread, "start", failed_start)
    error = "runtime_closed" if failure == "closed" else "cannot start new thread"
    if domain == "child":
        if resume:
            child = threads.create_thread("Synthetic paused child", thread_type="agent_child")
            run = runs.create_agent_run(kind="subagent", status="waiting_approval", parent_thread_id=conversation_id, thread_id=child)
            runs.save_agent_resume_state(run["id"], {"config": {"configurable": {"thread_id": child}},
                "enabled_tool_names": [], "interrupts": []}, status="waiting_approval")
        with pytest.raises((ValueError, RuntimeError), match=error):
            if resume:
                runner.resume_agent_run(run["id"], approved=True)
            else:
                runner.spawn_agent_run("Synthetic objective", parent_thread_id=conversation_id, profile="review")
        records = runs.list_agent_runs(parent_thread_id=conversation_id)
        assert len(records) == 1 and records[0]["status"] == "failed"
        assert not runner.list_active_agent_run_ids()
    else:
        task_id = tasks.create_task("Synthetic task", prompts=["Synthetic input"], channels=[], apply_default_skills=False)
        if resume:
            run_id = tasks._record_run_start(task_id, conversation_id, 1)
            tasks._save_pipeline_state(run_id=run_id, task_id=task_id, thread_id=conversation_id,
                current_step_index=0, step_outputs={}, config={"configurable": {"thread_id": conversation_id}},
                resume_token="synthetic-closed-resume", status="paused", graph_interrupted=True)
        with pytest.raises((ValueError, RuntimeError), match=error):
            if resume:
                tasks._resume_pipeline("synthetic-closed-resume", approved=True)
            else:
                tasks.run_task_background(task_id, conversation_id, [], notification=False)
        assert tasks.get_run_history(task_id)[0]["status"] == "failed"
        assert not tasks.get_running_tasks()
    assert registry.active() == ()


def test_failed_start_dispatch_fence_prevents_any_late_target(monkeypatch):
    from row_bot.runtime.executions import GenerationRuntimeRegistry
    registry = GenerationRuntimeRegistry()
    native_start = threading.Thread.start
    def failed_start(_worker):
        raise RuntimeError("cannot start new thread")
    monkeypatch.setattr(threading.Thread, "start", failed_start)
    targets, cleanups = [], []
    worker = registry.thread(target=lambda: targets.append("dispatched"), conversation_id="synthetic",
        stop_event=threading.Event(), domain="agent", on_entry_failure=cleanups.append)
    handle = registry.active("synthetic")[0]
    with pytest.raises(RuntimeError, match="cannot start new thread"):
        worker.start()
    assert handle.producer_done.is_set() and len(cleanups) == 1
    with pytest.raises(RuntimeError, match="execution_start_failed"):
        worker.start()
    # Even a delayed native entry outside the failed start wrapper is fenced.
    native_start(worker)
    worker.join(5)
    assert targets == [] and len(cleanups) == 1


def test_error_after_native_start_retains_live_owner(monkeypatch):
    from row_bot.runtime.executions import GenerationRuntimeRegistry
    registry = GenerationRuntimeRegistry()
    native_start = threading.Thread.start
    entered, release = threading.Event(), threading.Event()
    callbacks = []
    def post_start_error(worker):
        native_start(worker)
        raise RuntimeError("synthetic caller error after start")
    monkeypatch.setattr(threading.Thread, "start", post_start_error)
    def target():
        entered.set()
        assert release.wait(5)
    worker = registry.thread(target=target, conversation_id="synthetic", stop_event=threading.Event(),
                             domain="agent", on_entry_failure=callbacks.append)
    handle = registry.active("synthetic")[0]
    try:
        with pytest.raises(RuntimeError, match="caller error after start"):
            worker.start()
        assert entered.wait(5)
        assert not handle.producer_done.is_set()
        assert registry.active("synthetic") == (handle,) and callbacks == []
    finally:
        release.set()
        worker.join(5)
    assert handle.producer_done.is_set() and callbacks == []
