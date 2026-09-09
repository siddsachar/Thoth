"""Resumed workflow cancellation uses the same actual process producer owner."""
from __future__ import annotations

import threading

from tests.fixtures.tasks import fresh_tasks_module


def test_f_r02_resumed_workflow_stop_reaches_scope_and_retains_owner(tmp_path, monkeypatch):
    tasks = fresh_tasks_module(tmp_path, monkeypatch)
    from row_bot import agent, threads
    from row_bot.cancellation import current_cancellation_scope
    from row_bot.runtime import executions
    registry = executions.GenerationRuntimeRegistry()
    monkeypatch.setattr(executions, "generation_registry", registry)
    monkeypatch.setattr(threads, "DB_PATH", str(tmp_path / "threads.db"))
    threads._ensure_thread_db()
    threads._save_thread_meta("workflow-thread", "Synthetic workflow")
    task_id = tasks.create_task("Synthetic approval workflow", prompts=["Synthetic input"],
                                channels=[], apply_default_skills=False)
    run_id = tasks._record_run_start(task_id, "workflow-thread", 1)
    tasks._save_pipeline_state(run_id=run_id, task_id=task_id, thread_id="workflow-thread",
        current_step_index=0, step_outputs={}, config={"configurable": {"thread_id": "workflow-thread"}},
        resume_token="workflow-resume", status="paused", graph_interrupted=True)
    entered, cancel_seen, release, cleaned = (threading.Event() for _ in range(4))
    observed = []
    def blocked_resume(_tools, _config, approved, *, stop_event):
        scope = current_cancellation_scope()
        assert scope is not None and stop_event is scope.stop_event
        scope.register(cancel_seen.set)
        observed.append(stop_event)
        entered.set()
        try:
            assert release.wait(10)
            assert stop_event.is_set()
            raise agent.TaskStoppedError("Synthetic cancelled provider")
        finally:
            cleaned.set()
    monkeypatch.setattr(agent, "resume_invoke_agent", blocked_resume)
    tasks._resume_pipeline("workflow-resume", approved=True)
    assert entered.wait(10)
    handle = registry.active("workflow-thread")[0]
    try:
        assert tasks.stop_task("workflow-thread")
        assert cancel_seen.wait(10)
        assert observed == [handle.cancel_scope.stop_event]
        assert handle.status == "stopping"
        assert not handle.producer_done.is_set()
        assert not cleaned.is_set()
    finally:
        release.set()
        assert handle.producer_done.wait(10)
    assert handle.status == "stopped"
    assert cleaned.is_set()
    assert tasks._load_pipeline_state("workflow-resume") is None
    assert tasks.get_run_history(task_id)[0]["status"] == "stopped"
