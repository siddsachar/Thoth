from __future__ import annotations

import importlib
import sys

import pytest


pytestmark = pytest.mark.subsystem


def _fresh_modules(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(data_dir))
    for name in (
        "row_bot.tasks",
        "row_bot.agent_profiles",
        "row_bot.agent_settings",
        "row_bot.agent_runs",
        "row_bot.agent_orchestrator",
    ):
        sys.modules.pop(name, None)
    import row_bot.tasks as tasks
    import row_bot.agent_runs as agent_runs
    import row_bot.agent_orchestrator as orchestrator

    importlib.reload(tasks)
    agent_runs = importlib.reload(agent_runs)
    orchestrator = importlib.reload(orchestrator)
    return agent_runs, orchestrator


def _setup(agent_runs, orchestrator):
    orchestration = orchestrator.create_or_get_orchestration(
        parent_thread_id="parent",
        parent_generation_id="generation",
        root_objective="Finish after restart",
        model_ref="provider:model",
        approval_mode="block",
        runtime_surface="normal_chat",
    )
    completed = agent_runs.create_agent_run(
        run_id="completed",
        status="completed",
        parent_thread_id="parent",
        prompt="Already done",
        summary="Retained result",
        model_override="provider:model",
    )
    running = agent_runs.create_agent_run(
        run_id="running",
        status="running",
        parent_thread_id="parent",
        prompt="Still running",
        model_override="provider:model",
    )
    orchestrator.register_member(orchestration["id"], completed["id"], required=True)
    orchestrator.register_member(orchestration["id"], running["id"], required=True)
    orchestrator.finalize_parent_generation(
        orchestration["id"],
        continuation_state={"config": {"configurable": {}}, "enabled_tool_names": []},
    )
    return orchestration, completed, running


def test_deferred_repair_marks_active_work_interrupted_without_executor_calls(
    tmp_path,
    monkeypatch,
):
    agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration, completed, running = _setup(agent_runs, orchestrator)
    superseded = agent_runs.create_agent_run(
        run_id="superseded",
        status="failed",
        parent_thread_id="parent",
        prompt="Failed earlier attempt",
        model_override="provider:model",
    )
    orchestrator.register_member(
        orchestration["id"],
        superseded["id"],
        required=False,
    )
    conn = agent_runs._get_conn()
    try:
        conn.execute(
            "UPDATE agent_orchestration_members SET status = 'retried' "
            "WHERE orchestration_id = ? AND run_id = ?",
            (orchestration["id"], superseded["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    calls: list[str] = []
    orchestrator.set_test_executors(
        synthesis=lambda *_args: calls.append("synthesis") or "unexpected",
        retry=lambda *_args: calls.append("retry") or {},
        delivery=lambda *_args: True,
    )

    startup_result = agent_runs.recover_stale_agent_runs()

    assert startup_result["orchestrations_interrupted"] == 0
    assert orchestrator.get_orchestration(orchestration["id"])["status"] == "waiting_children"
    assert agent_runs.get_agent_run(running["id"])["status"] == "running"

    result = orchestrator.repair_interrupted_orchestrations_batch(limit=10)

    assert result["processed"] == 1
    assert orchestrator.get_orchestration(orchestration["id"])["status"] == "interrupted"
    assert agent_runs.get_agent_run(completed["id"])["status"] == "completed"
    assert agent_runs.get_agent_run(running["id"])["status"] == "interrupted"
    assert orchestrator.get_member_for_run(superseded["id"])["status"] == "retried"
    assert agent_runs.get_agent_run(superseded["id"])["status"] == "failed"
    assert calls == []


def test_deferred_repair_restores_recorded_terminal_event_before_interrupting(
    tmp_path,
    monkeypatch,
):
    agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration, _completed, running = _setup(agent_runs, orchestrator)
    agent_runs.append_agent_event(
        running["id"],
        "run.stopped",
        {"reason": "Already stopped before the restart repair"},
    )
    conn = agent_runs._get_conn()
    try:
        conn.execute(
            "UPDATE agent_runs SET status = 'interrupted', stop_requested = 1 WHERE id = ?",
            (running["id"],),
        )
        conn.execute(
            "UPDATE agent_orchestration_members SET status = 'interrupted' "
            "WHERE orchestration_id = ? AND run_id = ?",
            (orchestration["id"], running["id"]),
        )
        conn.execute(
            "UPDATE agent_orchestrations SET status = 'interrupted', "
            "parent_state = 'interrupted', lease_owner = 'dead-owner', "
            "wake_requested_at = 'stale-wake' WHERE id = ?",
            (orchestration["id"],),
        )
        conn.commit()
    finally:
        conn.close()
    calls: list[str] = []
    orchestrator.set_test_executors(
        parent=lambda *_args: calls.append("parent") or "unexpected",
        retry=lambda *_args: calls.append("retry") or {},
        delivery=lambda *_args: calls.append("delivery") or True,
    )

    result = orchestrator.repair_interrupted_orchestrations_batch(limit=10)

    assert result["processed"] == 1
    assert agent_runs.get_agent_run(running["id"])["status"] == "stopped"
    assert orchestrator.get_member_for_run(running["id"])["status"] == "stopped"
    repaired = orchestrator.get_orchestration(orchestration["id"])
    assert repaired["status"] == "interrupted"
    assert repaired["lease_owner"] == ""
    assert repaired["wake_requested_at"] == ""
    assert calls == []


def test_explicit_resume_requeues_only_interrupted_required_work(
    tmp_path,
    monkeypatch,
):
    agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration, completed, running = _setup(agent_runs, orchestrator)
    orchestrator.recover_interrupted_orchestrations()
    replacements: list[str] = []

    monkeypatch.setattr("row_bot.tools.registry.is_enabled", lambda name: name == "agents")
    monkeypatch.setattr(
        "row_bot.providers.readiness.ensure_agent_ready",
        lambda _model: object(),
    )

    def resume_executor(row, member, explicit_resume):
        assert explicit_resume is True
        replacement = agent_runs.create_agent_run(
            run_id="resumed",
            status="queued",
            parent_thread_id="parent",
            prompt="Still running",
            model_override=row["model_ref"],
        )
        orchestrator.register_member(
            row["id"],
            replacement["id"],
            required=True,
            attempt=member["attempt"],
            retry_of_run_id=member["run_id"],
        )
        replacements.append(replacement["id"])
        return replacement

    orchestrator.set_test_executors(
        retry=resume_executor,
        synthesis=lambda *_args: "Final after resume",
        delivery=lambda *_args: True,
    )
    resumed = orchestrator.resume_orchestration(orchestration["id"])

    assert resumed["status"] == "waiting_children"
    assert replacements == ["resumed"]
    assert agent_runs.get_agent_run(completed["id"])["status"] == "completed"
    agent_runs.finish_agent_run("resumed", "completed", summary="Finished after restart")
    assert orchestrator.wait_for_synthesis(orchestration["id"])["status"] == "completed"


def test_resume_revalidates_agents_model_and_workspace_without_calling_executor(
    tmp_path,
    monkeypatch,
):
    agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration, _completed, running = _setup(agent_runs, orchestrator)
    orchestrator.recover_interrupted_orchestrations()
    calls: list[str] = []
    orchestrator.set_test_executors(
        retry=lambda *_args: calls.append("retry") or {},
        delivery=lambda *_args: True,
    )

    monkeypatch.setattr("row_bot.tools.registry.is_enabled", lambda _name: False)
    with pytest.raises(orchestrator.OrchestrationError, match="Agents are disabled"):
        orchestrator.resume_orchestration(orchestration["id"])
    assert calls == []

    monkeypatch.setattr("row_bot.tools.registry.is_enabled", lambda _name: True)
    monkeypatch.setattr(
        "row_bot.providers.readiness.ensure_agent_ready",
        lambda _model: (_ for _ in ()).throw(RuntimeError("Configured model unavailable")),
    )
    with pytest.raises(orchestrator.OrchestrationError, match="model unavailable"):
        orchestrator.resume_orchestration(orchestration["id"])
    assert calls == []

    monkeypatch.setattr(
        "row_bot.providers.readiness.ensure_agent_ready",
        lambda _model: object(),
    )
    missing = tmp_path / "removed-worktree"
    conn = agent_runs._get_conn()
    try:
        conn.execute(
            "UPDATE agent_runs SET workspace_path = ? WHERE id = ?",
            (str(missing), running["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(orchestrator.OrchestrationError, match="no longer exists"):
        orchestrator.resume_orchestration(orchestration["id"])
    assert calls == []


def test_v2_recovery_keeps_inbox_and_resumes_original_parent_only_on_request(
    tmp_path,
    monkeypatch,
):
    agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    schedule_parent = orchestrator._schedule_parent_runner
    monkeypatch.setattr(orchestrator, "_schedule_parent_runner", lambda _id: None)
    orchestration = orchestrator.create_or_get_orchestration(
        parent_thread_id="parent",
        parent_generation_id="v2-generation",
        root_objective="Finish from the retained child event.",
        model_ref="provider:model",
        approval_mode="block",
        runtime_surface="normal_chat",
        orchestration_version=2,
    )
    child = agent_runs.create_agent_run(
        run_id="already-completed",
        status="completed",
        parent_thread_id="parent",
        prompt="Complete before restart",
        summary="Retained v2 result",
        model_override="provider:model",
    )
    orchestrator.register_member(
        orchestration["id"],
        child["id"],
        required=True,
    )
    orchestrator.arm_parent_wait(
        orchestration["id"],
        continuation_state={
            "config": {"configurable": {"thread_id": "parent"}},
            "enabled_tool_names": ["agents"],
        },
    )
    orchestrator.record_thread_event(
        orchestration["id"],
        kind="child_terminal",
        content="Retained v2 result",
        run_id=child["id"],
        source_event_id="run:already-completed:terminal:completed",
        payload={"status": "completed", "summary": "Retained v2 result"},
        request_wake=False,
    )
    calls: list[str] = []
    orchestrator.set_test_executors(
        parent=lambda *_args: calls.append("parent") or "Final after explicit resume",
        synthesis=lambda *_args: calls.append("synthesis") or "unexpected",
        delivery=lambda *_args: True,
    )

    recovered = agent_runs.recover_stale_agent_runs()
    repaired = orchestrator.repair_interrupted_orchestrations_batch(limit=10)

    assert recovered["orchestrations_interrupted"] == 0
    assert repaired["processed"] == 1
    assert calls == []
    interrupted = orchestrator.get_orchestration(orchestration["id"])
    assert interrupted["parent_state"] == "interrupted"
    assert len(orchestrator.pending_thread_events(orchestration["id"])) == 1

    monkeypatch.setattr("row_bot.tools.registry.is_enabled", lambda name: name == "agents")
    monkeypatch.setattr(
        "row_bot.providers.readiness.ensure_agent_ready",
        lambda _model: object(),
    )
    monkeypatch.setattr(orchestrator, "_schedule_parent_runner", schedule_parent)
    orchestrator.resume_orchestration(orchestration["id"])
    final = orchestrator.wait_for_parent(orchestration["id"], timeout=2)

    assert final["status"] == "completed"
    assert calls == ["parent"]
    assert orchestrator.pending_thread_events(orchestration["id"]) == []
