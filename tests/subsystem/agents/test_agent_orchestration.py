from __future__ import annotations

import importlib
import sqlite3
import sys
import threading
import time

import pytest


pytestmark = pytest.mark.subsystem


def _fresh_modules(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
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

    tasks = importlib.reload(tasks)
    agent_runs = importlib.reload(agent_runs)
    orchestrator = importlib.reload(orchestrator)
    return tasks, agent_runs, orchestrator


def _orchestration(orchestrator, *, generation="generation-1"):
    return orchestrator.create_or_get_orchestration(
        parent_thread_id="parent-thread",
        parent_generation_id=generation,
        root_objective="Compare the implementations and report one recommendation.",
        model_ref="provider:model",
        approval_mode="block",
        runtime_surface="normal_chat",
    )


def _run(agent_runs, run_id: str, *, status: str = "running"):
    return agent_runs.create_agent_run(
        run_id=run_id,
        status=status,
        parent_thread_id="parent-thread",
        thread_id=f"thread-{run_id}",
        prompt=f"Objective {run_id}",
        display_name=run_id,
        model_override="provider:model",
    )


def test_schema_repairs_partial_orchestration_tables(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(data_dir))
    db_path = data_dir / "tasks.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE agent_orchestrations (id TEXT PRIMARY KEY)")
        conn.execute(
            "INSERT INTO agent_orchestrations (id) VALUES ('partial-orchestration')"
        )
        conn.commit()

    _tasks, agent_runs, _orchestrator = _fresh_modules(tmp_path, monkeypatch)
    agent_runs.ensure_agent_run_schema(force=True)

    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(agent_orchestrations)"
            ).fetchall()
        }
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {
        "parent_thread_id",
        "parent_generation_id",
        "continuation_state_json",
        "delivery_context_json",
        "settings_snapshot_json",
    } <= columns
    assert {
        "agent_orchestration_members",
        "agent_orchestration_messages",
    } <= tables


def test_corrupt_orchestration_json_repairs_to_safe_empty_values(
    tmp_path,
    monkeypatch,
):
    _tasks, _agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = _orchestration(orchestrator)
    conn = orchestrator._conn()
    try:
        conn.execute(
            "UPDATE agent_orchestrations SET continuation_state_json = ?, "
            "delivery_context_json = ?, settings_snapshot_json = ? WHERE id = ?",
            ("{broken", "null", "[]", orchestration["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    repaired = orchestrator.get_orchestration(orchestration["id"])
    assert repaired["continuation_state_json"] == {}
    assert repaired["delivery_context_json"] == {}
    assert repaired["settings_snapshot_json"] == {}


def test_generation_is_single_and_members_are_required_optional_and_waved(
    tmp_path,
    monkeypatch,
):
    _tasks, agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = _orchestration(orchestrator)
    same = _orchestration(orchestrator)
    assert same["id"] == orchestration["id"]

    for index in range(5):
        run = _run(agent_runs, f"run-{index}", status="queued")
        orchestrator.register_member(
            orchestration["id"],
            run["id"],
            required=index != 4,
        )

    overview = orchestrator.orchestration_overview(orchestration["id"])
    assert overview["required_total"] == 4
    assert overview["optional_total"] == 1
    assert [member["wave"] for member in overview["members"]] == [0, 0, 0, 1, 1]
    assert [member["required"] for member in overview["members"]] == [
        True,
        True,
        True,
        True,
        False,
    ]


def test_concurrent_generation_creation_returns_one_orchestration(
    tmp_path,
    monkeypatch,
):
    _tasks, _agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    barrier = threading.Barrier(5)
    identifiers: list[str] = []

    def create():
        barrier.wait()
        identifiers.append(str(_orchestration(orchestrator)["id"]))

    workers = [threading.Thread(target=create) for _ in range(4)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join()

    assert len(identifiers) == 4
    assert len(set(identifiers)) == 1


def test_terminal_events_claim_one_synthesis_after_required_barrier(
    tmp_path,
    monkeypatch,
):
    _tasks, agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = _orchestration(orchestrator)
    first = _run(agent_runs, "first")
    second = _run(agent_runs, "second")
    optional = _run(agent_runs, "optional")
    orchestrator.register_member(orchestration["id"], first["id"], required=True)
    orchestrator.register_member(orchestration["id"], second["id"], required=True)
    orchestrator.register_member(orchestration["id"], optional["id"], required=False)

    calls: list[str] = []
    deliveries: list[tuple[str, str]] = []
    orchestrator.set_test_executors(
        synthesis=lambda _row, prompt: calls.append(prompt) or "Consolidated final",
        delivery=lambda _row, kind, text, _key: deliveries.append((kind, text)) or True,
    )
    assert orchestrator.finalize_parent_generation(
        orchestration["id"],
        continuation_state={
            "config": {"configurable": {"thread_id": "parent-thread"}},
            "enabled_tool_names": ["agents"],
        },
    )
    agent_runs.finish_agent_run(first["id"], "completed", summary="First result")
    assert orchestrator.get_orchestration(orchestration["id"])["status"] == "waiting_children"

    barrier = threading.Barrier(3)

    def finish_second():
        barrier.wait()
        agent_runs.finish_agent_run(second["id"], "completed", summary="Second result")

    def duplicate_terminal_event():
        barrier.wait()
        orchestrator.handle_run_terminal(second["id"])

    threads = [
        threading.Thread(target=finish_second),
        threading.Thread(target=duplicate_terminal_event),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    final = orchestrator.wait_for_synthesis(orchestration["id"])

    assert final["status"] == "completed"
    assert len(calls) == 1
    assert "First result" in calls[0]
    assert "Second result" in calls[0]
    assert deliveries == [
        ("acknowledgement", "I'm working on this with 2 agents."),
        ("final", "Consolidated final"),
    ]
    assert orchestrator.get_member_for_run(optional["id"])["status"] == "running"


def test_transient_failure_retries_once_and_preserves_logical_barrier(
    tmp_path,
    monkeypatch,
):
    _tasks, agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = _orchestration(orchestrator)
    original = _run(agent_runs, "original")
    orchestrator.register_member(orchestration["id"], original["id"], required=True)
    replacements: list[str] = []

    def retry_executor(row, member, _explicit_resume):
        replacement = _run(agent_runs, "replacement", status="queued")
        orchestrator.register_member(
            row["id"],
            replacement["id"],
            required=member["required"],
            attempt=2,
            retry_of_run_id=member["run_id"],
        )
        replacements.append(replacement["id"])
        return replacement

    orchestrator.set_test_executors(
        retry=retry_executor,
        synthesis=lambda _row, _prompt: "Recovered final",
        delivery=lambda *_args: True,
    )
    orchestrator.finalize_parent_generation(
        orchestration["id"],
        continuation_state={"config": {"configurable": {}}, "enabled_tool_names": []},
    )
    agent_runs.finish_agent_run(
        original["id"],
        "failed",
        error="Provider temporarily unavailable; try again",
    )
    assert replacements == ["replacement"]
    assert orchestrator.get_orchestration(orchestration["id"])["status"] == "waiting_children"

    agent_runs.finish_agent_run(
        replacements[0],
        "failed",
        error="Provider temporarily unavailable; try again",
    )
    final = orchestrator.wait_for_synthesis(orchestration["id"])
    assert final["status"] == "completed_partial"
    assert replacements == ["replacement"]
    current_required = [
        member
        for member in orchestrator.list_members(orchestration["id"], include_runs=False)
        if member["required"]
    ]
    assert len(current_required) == 1
    assert current_required[0]["attempt"] == 2


def test_lifecycle_snapshots_are_fixed_and_illegal_transitions_fail(
    tmp_path,
    monkeypatch,
):
    _tasks, _agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = orchestrator.create_or_get_orchestration(
        parent_thread_id="snapshot-parent",
        parent_generation_id="snapshot-generation",
        root_objective="Preserve the runtime contract.",
        model_ref="provider:parent-model",
        approval_mode="allow_all",
        runtime_surface="workflow",
        settings_snapshot={
            "max_iterations": 90,
            "max_spawn_depth": 1,
            "max_concurrent_children": 3,
            "max_active_children_global": 8,
            "child_timeout_seconds": 0,
        },
    )

    assert orchestration["model_ref"] == "provider:parent-model"
    assert orchestration["approval_mode"] == "allow_all"
    assert {
        key: orchestration["settings_snapshot_json"][key]
        for key in (
            "max_iterations",
            "max_spawn_depth",
            "max_concurrent_children",
            "max_active_children_global",
            "child_timeout_seconds",
        )
    } == {
        "max_iterations": 90,
        "max_spawn_depth": 1,
        "max_concurrent_children": 3,
        "max_active_children_global": 8,
        "child_timeout_seconds": 0,
    }
    running = orchestrator.transition_orchestration(orchestration["id"], "running")
    assert running["status"] == "running"
    with pytest.raises(orchestrator.OrchestrationError, match="Illegal"):
        orchestrator.transition_orchestration(orchestration["id"], "completed")


def test_dependencies_are_same_group_and_wake_from_terminal_event(
    tmp_path,
    monkeypatch,
):
    _tasks, agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = _orchestration(orchestrator)
    first = _run(agent_runs, "dependency", status="running")
    second = _run(agent_runs, "dependent", status="queued")
    foreign = _orchestration(orchestrator, generation="foreign")
    foreign_run = _run(agent_runs, "foreign-run", status="queued")
    orchestrator.register_member(foreign["id"], foreign_run["id"])
    orchestrator.register_member(orchestration["id"], first["id"])
    orchestrator.register_member(
        orchestration["id"],
        second["id"],
        dependency_run_ids=[first["id"]],
    )
    with pytest.raises(orchestrator.OrchestrationError, match="same orchestration"):
        extra = _run(agent_runs, "invalid-dependent", status="queued")
        orchestrator.register_member(
            orchestration["id"],
            extra["id"],
            dependency_run_ids=[foreign_run["id"]],
        )

    stopped = threading.Event()
    ready: list[bool] = []
    waiter = threading.Thread(
        target=lambda: ready.append(
            orchestrator.wait_for_dependencies(second["id"], stopped)
        )
    )
    waiter.start()
    time.sleep(0.02)
    assert ready == []
    agent_runs.finish_agent_run(first["id"], "completed", summary="Dependency done")
    waiter.join(1)
    assert ready == [True]


def test_optional_members_are_durable_nonblocking_and_keep_legacy_delivery(
    tmp_path,
    monkeypatch,
):
    _tasks, agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = _orchestration(orchestrator)
    required = _run(agent_runs, "required")
    optional = _run(agent_runs, "optional")
    orchestrator.register_member(orchestration["id"], required["id"], required=True)
    orchestrator.register_member(orchestration["id"], optional["id"], required=False)
    prompts: list[str] = []
    orchestrator.set_test_executors(
        synthesis=lambda _row, prompt: prompts.append(prompt) or "Final",
        delivery=lambda *_args: True,
    )
    orchestrator.finalize_parent_generation(
        orchestration["id"],
        continuation_state={"config": {"configurable": {}}, "enabled_tool_names": []},
    )
    agent_runs.finish_agent_run(
        optional["id"],
        "failed",
        error="Invalid argument: optional evidence unavailable",
    )
    # Optional completion is not owned by consolidated required delivery.
    assert orchestrator.handle_run_terminal(optional["id"]) is False
    agent_runs.finish_agent_run(required["id"], "completed", summary="Required evidence")
    final = orchestrator.wait_for_synthesis(orchestration["id"])

    assert final["status"] == "completed_partial"
    assert len(prompts) == 1
    assert "Optional background work" in prompts[0]
    assert "Invalid argument" in prompts[0]


@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        ("timed_out", "", True),
        ("failed", "Provider temporarily unavailable", True),
        ("failed", "Retryable network transport failure", True),
        ("blocked", "Temporary workspace lock contention", True),
        ("blocked", "Approval denied by user", False),
        ("failed", "Invalid tool argument", False),
        ("failed", "Missing API key credential", False),
        ("stopped", "Explicit stop", False),
    ],
)
def test_transient_failure_classification(status, error, expected):
    from row_bot.agent_orchestrator import is_transient_failure

    assert is_transient_failure({"status": status, "error": error}) is expected


def test_result_packet_is_ordered_bounded_and_reports_worktrees(
    tmp_path,
    monkeypatch,
):
    _tasks, agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = _orchestration(orchestrator)
    for index, summary in enumerate(("first-" + "a" * 200, "second-" + "b" * 200)):
        run = agent_runs.create_agent_run(
            run_id=f"ordered-{index}",
            status="completed",
            parent_thread_id="parent-thread",
            prompt=f"Objective {index}",
            display_name=f"Child {index}",
            summary=summary,
            workspace_mode="worktree",
            workspace_path=f"D:/disposable/worktree-{index}",
            model_override="provider:model",
        )
        orchestrator.register_member(orchestration["id"], run["id"], required=True)
        orchestrator.handle_run_terminal(run)

    packet = orchestrator._ordered_result_packet(orchestration)
    assert packet.index("Child 0") < packet.index("Child 1")
    assert "D:/disposable/worktree-0" in packet
    capped = orchestrator._ordered_result_packet(orchestration, limit=360)
    assert len(capped) <= 360
    assert capped.endswith("[Result packet truncated to context budget.]")
