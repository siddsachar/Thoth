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


def test_startup_marks_active_work_interrupted_without_executor_calls(
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

    result = agent_runs.recover_stale_agent_runs()

    assert result["orchestrations_interrupted"] == 1
    assert orchestrator.get_orchestration(orchestration["id"])["status"] == "interrupted"
    assert agent_runs.get_agent_run(completed["id"])["status"] == "completed"
    assert agent_runs.get_agent_run(running["id"])["status"] == "interrupted"
    assert orchestrator.get_member_for_run(superseded["id"])["status"] == "retried"
    assert agent_runs.get_agent_run(superseded["id"])["status"] == "failed"
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
