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


def _orchestration(orchestrator, *, generation="generation-1", version=1):
    return orchestrator.create_or_get_orchestration(
        parent_thread_id="parent-thread",
        parent_generation_id=generation,
        root_objective="Compare the implementations and report one recommendation.",
        model_ref="provider:model",
        approval_mode="block",
        runtime_surface="normal_chat",
        orchestration_version=version,
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
        "orchestration_version",
        "parent_state",
        "wake_requested_at",
        "lease_owner",
        "lease_expires_at",
        "parent_attempt",
    } <= columns
    assert {
        "agent_orchestration_members",
        "agent_orchestration_messages",
    } <= tables
    with sqlite3.connect(db_path) as conn:
        message_columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(agent_orchestration_messages)"
            ).fetchall()
        }
    assert {
        "payload_json",
        "source_event_id",
        "consumed_at",
        "attempt_count",
        "last_error",
    } <= message_columns


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


def test_orchestration_display_counts_separate_approval_and_active_work(
    tmp_path,
    monkeypatch,
):
    _tasks, agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = _orchestration(orchestrator)
    statuses = {
        "queued-child": "queued",
        "running-child": "running",
        "approval-child": "waiting_approval",
        "complete-child": "completed",
        "failed-child": "failed",
    }
    for run_id, status in statuses.items():
        agent_runs.create_agent_run(
            run_id=run_id,
            status=status,
            parent_thread_id="parent",
        )
        orchestrator.register_member(
            orchestration["id"],
            run_id,
            required=True,
        )
        orchestrator.handle_run_status(run_id, status)

    overview = orchestrator.orchestration_overview(orchestration["id"])

    assert overview["counts"] == {
        "running": 2,
        "needs_approval": 1,
        "completed": 1,
        "failed": 1,
        "active": 3,
        "required": 5,
        "total_attempts": 5,
    }
    assert orchestrator.orchestration_status_label("waiting_children") == (
        "Waiting for Agents"
    )
    assert orchestrator.orchestration_status_label("synthesizing") == (
        "Preparing final answer"
    )
    assert orchestrator.orchestration_status_label("completed_partial") == (
        "Completed with issues"
    )


def test_orchestration_counts_reconcile_terminal_run_before_member_callback(
    tmp_path,
    monkeypatch,
):
    _tasks, agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = _orchestration(orchestrator)
    agent_runs.create_agent_run(
        run_id="stopped-before-member-callback",
        status="running",
        parent_thread_id="parent",
    )
    orchestrator.register_member(
        orchestration["id"],
        "stopped-before-member-callback",
        required=True,
    )
    orchestrator.handle_run_status("stopped-before-member-callback", "running")

    agent_runs.finish_agent_run(
        "stopped-before-member-callback",
        "stopped",
        error="Stop requested",
    )
    overview = orchestrator.orchestration_overview(orchestration["id"])

    assert overview["counts"]["running"] == 0
    assert overview["counts"]["active"] == 0
    assert overview["counts"]["failed"] == 1


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


def test_v2_child_completion_wakes_original_parent_without_synthesis(
    tmp_path,
    monkeypatch,
):
    _tasks, agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = _orchestration(orchestrator, version=2)
    child = _run(agent_runs, "joined-child")
    orchestrator.register_member(orchestration["id"], child["id"], required=True)
    parent_calls: list[tuple[str, str]] = []
    deliveries: list[tuple[str, str, str]] = []

    def parent_executor(row, event_context, enabled_tools, config):
        parent_calls.append(
            (
                str((config.get("configurable") or {}).get("thread_id") or ""),
                event_context,
            )
        )
        assert enabled_tools == ["agents", "shell"]
        return "The child result supports the final recommendation."

    orchestrator.set_test_executors(
        parent=parent_executor,
        synthesis=lambda *_args: pytest.fail("v2 must not use synthesis"),
        delivery=lambda _row, kind, text, key: (
            deliveries.append((kind, text, key)) or True
        ),
    )
    result = orchestrator.complete_parent_pass(
        orchestration["id"],
        "I delegated the comparison and am checking the local implementation.",
        continuation_state={
            "config": {"configurable": {"thread_id": "parent-thread"}},
            "enabled_tool_names": ["agents", "shell"],
        },
        foreground=True,
    )

    assert result.waiting is True
    assert result.output_kind == "progress"
    assert orchestrator.get_orchestration(orchestration["id"])["parent_state"] == "waiting"
    assert [
        row["content"]
        for row in orchestrator.list_messages(
            orchestration["id"], kinds=["parent_progress"]
        )
    ] == ["I delegated the comparison and am checking the local implementation."]

    agent_runs.finish_agent_run(
        child["id"],
        "completed",
        summary="The implementations differ in one material way.",
    )
    final = orchestrator.wait_for_parent(orchestration["id"], timeout=2)

    assert final["status"] == "completed"
    assert parent_calls[0][0] == "parent-thread"
    assert "joined-child" in parent_calls[0][1]
    assert "The implementations differ in one material way." in parent_calls[0][1]
    assert deliveries == [
        (
            "final",
            "The child result supports the final recommendation.",
            f"orchestration:{orchestration['id']}:parent_final:1",
        )
    ]
    assert not [
        message
        for message in orchestrator.list_messages(orchestration["id"])
        if message["kind"] == "acknowledgement"
    ]


def test_v2_later_delegation_wave_keeps_same_parent_turn(
    tmp_path,
    monkeypatch,
):
    _tasks, agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = _orchestration(orchestrator, version=2)
    first = _run(agent_runs, "wave-one")
    orchestrator.register_member(orchestration["id"], first["id"], required=True)
    second_run_id = "wave-two"
    calls: list[str] = []

    def parent_executor(_row, event_context, _tools, _config):
        calls.append(event_context)
        if len(calls) == 1:
            second = _run(agent_runs, second_run_id)
            orchestrator.register_member(
                orchestration["id"],
                second["id"],
                required=True,
            )
            return "The first result exposed a second check, which I delegated."
        return "Both delegated checks are now reconciled."

    orchestrator.set_test_executors(
        parent=parent_executor,
        synthesis=lambda *_args: pytest.fail("v2 must not use synthesis"),
        delivery=lambda *_args: True,
    )
    orchestrator.complete_parent_pass(
        orchestration["id"],
        "I started the first check.",
        continuation_state={
            "config": {"configurable": {"thread_id": "parent-thread"}},
            "enabled_tool_names": ["agents"],
        },
        foreground=True,
    )
    agent_runs.finish_agent_run(first["id"], "completed", summary="First finding")
    waiting = orchestrator.wait_for_parent(
        orchestration["id"],
        timeout=2,
        terminal_only=False,
        minimum_attempts=1,
    )
    assert waiting["parent_state"] == "waiting"
    assert len(calls) == 1

    agent_runs.finish_agent_run(second_run_id, "completed", summary="Second finding")
    final = orchestrator.wait_for_parent(orchestration["id"], timeout=2)

    assert final["status"] == "completed"
    assert len(calls) == 2
    assert "First finding" in calls[0]
    assert "Second finding" in calls[1]


def test_v2_multiple_children_complete_out_of_order_before_one_final(
    tmp_path,
    monkeypatch,
):
    _tasks, agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = _orchestration(orchestrator, version=2)
    first = _run(agent_runs, "ordered-first")
    second = _run(agent_runs, "ordered-second")
    orchestrator.register_member(orchestration["id"], first["id"], required=True)
    orchestrator.register_member(orchestration["id"], second["id"], required=True)
    calls: list[str] = []

    def parent_executor(_row, context, _tools, _config):
        calls.append(context)
        if len(calls) == 1:
            return "The second check is back; I am still waiting for the first."
        return "Both checks are now reconciled in one final answer."

    orchestrator.set_test_executors(
        parent=parent_executor,
        delivery=lambda *_args: True,
    )
    orchestrator.complete_parent_pass(
        orchestration["id"],
        "I delegated both checks.",
        continuation_state={
            "config": {"configurable": {"thread_id": "parent-thread"}},
            "enabled_tool_names": ["agents"],
        },
        foreground=True,
    )

    agent_runs.finish_agent_run(
        second["id"],
        "completed",
        summary="Second completed first",
    )
    waiting = orchestrator.wait_for_parent(
        orchestration["id"],
        terminal_only=False,
        minimum_attempts=1,
    )
    assert waiting["parent_state"] == "waiting"
    agent_runs.finish_agent_run(
        first["id"],
        "completed",
        summary="First completed second",
    )
    final = orchestrator.wait_for_parent(orchestration["id"], minimum_attempts=2)

    assert final["status"] == "completed"
    assert "Second completed first" in calls[0]
    assert "First completed second" in calls[1]
    assert len(
        orchestrator.list_messages(orchestration["id"], kinds=["parent_final"])
    ) == 1


def test_v2_child_approval_does_not_freeze_original_parent(
    tmp_path,
    monkeypatch,
):
    _tasks, agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = _orchestration(orchestrator, version=2)
    child = _run(agent_runs, "approval-child")
    orchestrator.register_member(orchestration["id"], child["id"], required=True)
    calls: list[str] = []
    orchestrator.set_test_executors(
        parent=lambda _row, context, _tools, _config: (
            calls.append(context)
            or "The child needs approval; I can keep the parent thread responsive."
        ),
        delivery=lambda *_args: True,
    )
    orchestrator.complete_parent_pass(
        orchestration["id"],
        "I delegated the gated step.",
        continuation_state={
            "config": {"configurable": {"thread_id": "parent-thread"}},
            "enabled_tool_names": ["agents"],
        },
        foreground=True,
    )

    agent_runs.save_agent_resume_state(
        child["id"],
        {"config": {"configurable": {"thread_id": child["thread_id"]}}},
        status_message="Needs approval",
    )
    waiting = orchestrator.wait_for_parent(
        orchestration["id"],
        terminal_only=False,
        minimum_attempts=1,
    )

    assert waiting["status"] == "waiting_children"
    assert waiting["parent_state"] == "waiting"
    assert "CHILD_APPROVAL_REQUESTED" in calls[0]
    assert agent_runs.get_agent_run(child["id"])["status"] == "waiting_approval"


def test_v2_transient_retry_returns_replacement_to_same_parent(
    tmp_path,
    monkeypatch,
):
    _tasks, agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = _orchestration(orchestrator, version=2)
    original = _run(agent_runs, "retry-original")
    orchestrator.register_member(orchestration["id"], original["id"], required=True)
    replacement_ids: list[str] = []
    calls: list[str] = []

    def retry_executor(row, member, _explicit_resume):
        replacement = _run(agent_runs, "retry-replacement", status="queued")
        orchestrator.register_member(
            row["id"],
            replacement["id"],
            required=member["required"],
            attempt=2,
            retry_of_run_id=member["run_id"],
        )
        replacement_ids.append(replacement["id"])
        return replacement

    def parent_executor(_row, context, _tools, _config):
        calls.append(context)
        if "CHILD_RETRY_SCHEDULED" in context:
            return "The transient failure is retrying in the same delegated slot."
        return "The replacement result completed the original delegated task."

    orchestrator.set_test_executors(
        retry=retry_executor,
        parent=parent_executor,
        delivery=lambda *_args: True,
    )
    orchestrator.complete_parent_pass(
        orchestration["id"],
        "I delegated the provider check.",
        continuation_state={
            "config": {"configurable": {"thread_id": "parent-thread"}},
            "enabled_tool_names": ["agents"],
        },
        foreground=True,
    )

    agent_runs.finish_agent_run(
        original["id"],
        "failed",
        error="Provider temporarily unavailable; try again",
    )
    waiting = orchestrator.wait_for_parent(
        orchestration["id"],
        terminal_only=False,
        minimum_attempts=1,
    )
    assert waiting["parent_state"] == "waiting"
    assert replacement_ids == ["retry-replacement"]
    agent_runs.finish_agent_run(
        replacement_ids[0],
        "completed",
        summary="Replacement evidence",
    )
    final = orchestrator.wait_for_parent(orchestration["id"], minimum_attempts=2)

    assert final["status"] == "completed"
    current_required = [
        member
        for member in orchestrator.list_members(
            orchestration["id"],
            include_runs=False,
        )
        if member["required"]
    ]
    assert [member["run_id"] for member in current_required] == replacement_ids
    assert current_required[0]["attempt"] == 2
    assert "Replacement evidence" in calls[-1]


def test_v2_detached_child_does_not_block_parent_final(tmp_path, monkeypatch):
    _tasks, agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = _orchestration(orchestrator, version=2)
    detached = _run(agent_runs, "detached")
    orchestrator.register_member(
        orchestration["id"],
        detached["id"],
        required=False,
    )
    deliveries: list[str] = []
    orchestrator.set_test_executors(
        synthesis=lambda *_args: pytest.fail("v2 must not use synthesis"),
        delivery=lambda _row, kind, _text, _key: deliveries.append(kind) or True,
    )

    result = orchestrator.complete_parent_pass(
        orchestration["id"],
        "The requested answer is complete; optional monitoring continues.",
        continuation_state={
            "config": {"configurable": {"thread_id": "parent-thread"}},
            "enabled_tool_names": ["agents"],
        },
        foreground=True,
    )

    assert result.waiting is False
    assert result.output_kind == "final"
    assert orchestrator.get_orchestration(orchestration["id"])["status"] == "completed"
    assert deliveries == []


@pytest.mark.parametrize("approved", [True, False])
def test_v2_background_parent_approval_resumes_same_checkpoint(
    tmp_path,
    monkeypatch,
    approved,
):
    tasks, agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = _orchestration(orchestrator, version=2)
    child = _run(agent_runs, "approval-source")
    orchestrator.register_member(orchestration["id"], child["id"], required=True)
    calls: list[str] = []
    deliveries: list[str] = []

    def parent_executor(_row, event_context, _tools, _config):
        calls.append(event_context)
        if len(calls) == 1:
            return {
                "type": "interrupt",
                "interrupts": [
                    {
                        "tool": "shell",
                        "description": "Write the authorized disposable smoke file.",
                        "__interrupt_id": "interrupt-1",
                    }
                ],
            }
        return (
            "I completed the approved action."
            if approved
            else "I respected the denial and completed without that action."
        )

    orchestrator.set_test_executors(
        parent=parent_executor,
        synthesis=lambda *_args: pytest.fail("v2 must not use synthesis"),
        delivery=lambda _row, kind, text, _key: deliveries.append(
            f"{kind}:{text}"
        )
        or True,
    )
    orchestrator.complete_parent_pass(
        orchestration["id"],
        "I am waiting for the delegated evidence.",
        continuation_state={
            "config": {"configurable": {"thread_id": "parent-thread"}},
            "enabled_tool_names": ["agents", "shell"],
        },
        foreground=True,
    )
    agent_runs.finish_agent_run(
        child["id"],
        "completed",
        summary="The child result requires one gated parent action.",
    )
    waiting = orchestrator.wait_for_parent(
        orchestration["id"],
        timeout=2,
        terminal_only=False,
        minimum_attempts=1,
    )

    assert waiting["parent_state"] == "waiting_approval"
    approvals = tasks.get_pending_approvals(parent_thread_id="parent-thread")
    assert len(approvals) == 1
    assert approvals[0]["resume_kind"] == "parent_orchestration"
    assert tasks.respond_to_approval(
        approvals[0]["resume_token"],
        approved,
        source="test",
    )

    final = orchestrator.get_orchestration(orchestration["id"])
    assert final["status"] == "completed"
    assert final["parent_attempt"] == 2
    assert f"Approved: {str(approved).lower()}" in calls[-1]
    assert deliveries[-1].startswith("final:I ")


def test_v2_parent_approval_resume_applies_decision_to_regenerated_interrupt_group(
    tmp_path,
    monkeypatch,
):
    tasks, agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = _orchestration(orchestrator, version=2)
    child = _run(agent_runs, "approval-source")
    orchestrator.register_member(orchestration["id"], child["id"], required=True)
    agent_runs.finish_agent_run(
        child["id"],
        "completed",
        summary="The evidence is ready.",
    )
    pending_events = orchestrator.pending_thread_events(orchestration["id"])
    result = orchestrator.complete_parent_pass(
        orchestration["id"],
        {
            "type": "interrupt",
            "interrupts": [
                {
                    "__interrupt_id": f"transient-parent-interrupt-{index}",
                    "tool": "run_command",
                    "args": {"command": f"command-{index}"},
                }
                for index in range(4)
            ],
        },
        continuation_state={
            "config": {"configurable": {"thread_id": "parent-thread"}},
            "enabled_tool_names": ["shell"],
        },
        foreground=False,
        consumed_event_ids=[event.id for event in pending_events],
    )
    assert result.output_kind == "approval"
    waiting = orchestrator.get_orchestration(orchestration["id"])
    approval = orchestrator._persist_parent_approval(waiting, {
        "type": "interrupt",
        "interrupts": [
            {
                "__interrupt_id": f"transient-parent-interrupt-{index}",
                "tool": "run_command",
                "args": {"command": f"command-{index}"},
            }
            for index in range(4)
        ],
    })
    regenerated = [
        {
            "__interrupt_id": f"regenerated-parent-interrupt-{index}",
            "tool": "run_command",
            "args": {"command": f"command-{index}"},
        }
        for index in range(4)
    ]
    captured: dict[str, object] = {}

    def fake_resume(
        enabled_tools,
        config,
        approved,
        *,
        interrupt_ids=None,
    ):
        captured.update(
            enabled_tools=enabled_tools,
            config=config,
            approved=approved,
            interrupt_ids=interrupt_ids,
        )
        return "The approved parent action completed."

    monkeypatch.setattr("row_bot.agent.resume_invoke_agent", fake_resume)
    monkeypatch.setattr(
        "row_bot.agent.get_invoke_agent_interrupts",
        lambda _enabled_tools, _config: regenerated,
    )

    final = orchestrator.resume_parent_orchestration(
        orchestration["id"],
        resume_token=approval["resume_token"],
        approved=True,
    )

    assert final["status"] == "completed"
    assert captured["approved"] is True
    assert captured["interrupt_ids"] == [
        f"regenerated-parent-interrupt-{index}" for index in range(4)
    ]
    assert tasks.get_pending_approvals(parent_thread_id="parent-thread")


def test_handled_parent_approval_is_not_reused_for_a_new_interrupt(
    tmp_path,
    monkeypatch,
):
    tasks, _agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = _orchestration(orchestrator, version=2)
    first = {
        "type": "interrupt",
        "interrupts": [
            {
                "__interrupt_id": "first-id",
                "tool": "run_command",
                "args": {"command": "first command"},
            }
        ],
    }
    orchestrator.complete_parent_pass(
        orchestration["id"],
        first,
        continuation_state={
            "config": {"configurable": {"thread_id": "parent-thread"}},
            "enabled_tool_names": ["shell"],
        },
        foreground=False,
    )
    [old_approval] = tasks.get_pending_approvals(parent_thread_id="parent-thread")
    conn = orchestrator._conn()
    try:
        conn.execute(
            "UPDATE approval_requests SET status = 'approved', responded_at = ? WHERE id = ?",
            (orchestrator._now(), old_approval["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    second = {
        "type": "interrupt",
        "interrupts": [
            {
                "__interrupt_id": "second-id",
                "tool": "run_command",
                "args": {"command": "second command"},
            }
        ],
    }
    approval = orchestrator._persist_parent_approval(
        orchestrator.get_orchestration(orchestration["id"]),
        second,
    )

    assert approval["approval_id"] != old_approval["id"]
    [pending] = tasks.get_pending_approvals(parent_thread_id="parent-thread")
    assert pending["id"] == approval["approval_id"]


def test_parent_resume_failure_becomes_resume_required_not_phantom_approval(
    tmp_path,
    monkeypatch,
):
    tasks, _agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = _orchestration(orchestrator, version=2)
    interrupt = {
        "type": "interrupt",
        "interrupts": [
            {
                "__interrupt_id": "original-id",
                "tool": "run_command",
                "args": {"command": "protected command"},
            }
        ],
    }
    orchestrator.complete_parent_pass(
        orchestration["id"],
        interrupt,
        continuation_state={
            "config": {"configurable": {"thread_id": "parent-thread"}},
            "enabled_tool_names": ["shell"],
        },
        foreground=False,
    )
    [approval] = tasks.get_pending_approvals(parent_thread_id="parent-thread")
    monkeypatch.setattr(
        "row_bot.agent.get_invoke_agent_interrupts",
        lambda _enabled_tools, _config: interrupt["interrupts"],
    )
    monkeypatch.setattr(
        "row_bot.agent.resume_invoke_agent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("resume failed")),
    )

    with pytest.raises(RuntimeError, match="resume failed"):
        tasks.respond_to_approval(approval["resume_token"], True, source="test")

    failed = orchestrator.get_orchestration(orchestration["id"])
    assert failed["status"] == "interrupted"
    assert failed["parent_state"] == "interrupted"
    assert "Resume is required" in failed["error_message"]
    assert tasks.get_pending_approvals(parent_thread_id="parent-thread") == []
    assert tasks.respond_to_approval(approval["resume_token"], True, source="test") is False
    assert "parent_approval" not in failed["continuation_state_json"]
    assert "parent_interrupt" not in failed["continuation_state_json"]


def test_parent_pass_records_authoritative_checkpoint_identity(
    tmp_path,
    monkeypatch,
):
    _tasks, _agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = _orchestration(orchestrator, version=2)
    monkeypatch.setattr(
        orchestrator,
        "_checkpoint_output_metadata",
        lambda thread_id, text: {
            "checkpoint_message_id": "checkpoint-message-7",
            "checkpoint_revision": "checkpoint-revision-9",
        },
    )

    result = orchestrator.complete_parent_pass(
        orchestration["id"],
        "Checkpoint-authored final",
        foreground=True,
    )

    assert result.output_kind == "final"
    [message] = orchestrator.list_messages(
        orchestration["id"], kinds=["parent_final"]
    )
    assert message["payload_json"]["checkpoint_message_id"] == (
        "checkpoint-message-7"
    )
    assert message["payload_json"]["checkpoint_revision"] == (
        "checkpoint-revision-9"
    )


def test_v2_user_steering_and_child_completion_share_ordered_parent_inbox(
    tmp_path,
    monkeypatch,
):
    _tasks, agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = _orchestration(orchestrator, version=2)
    child = _run(agent_runs, "steered-child")
    orchestrator.register_member(orchestration["id"], child["id"], required=True)
    calls: list[tuple[str, list[dict]]] = []

    def parent_executor(_row, event_context, _tools, config):
        calls.append(
            (
                event_context,
                list(config["configurable"].get("thread_event_messages") or []),
            )
        )
        if len(calls) == 1:
            return "I applied your new direction while the child keeps running."
        return "The child result and your direction are now reconciled."

    orchestrator.set_test_executors(
        parent=parent_executor,
        synthesis=lambda *_args: pytest.fail("v2 must not use synthesis"),
        delivery=lambda *_args: True,
    )
    orchestrator.complete_parent_pass(
        orchestration["id"],
        "I delegated the initial check.",
        continuation_state={
            "config": {"configurable": {"thread_id": "parent-thread"}},
            "enabled_tool_names": ["agents"],
        },
        foreground=True,
    )

    routed = orchestrator.route_parent_steering(
        parent_thread_id="parent-thread",
        incoming_generation_id="later-user-message",
        content="Prioritize the Windows behavior in the final answer.",
    )
    waiting = orchestrator.wait_for_parent(
        orchestration["id"],
        timeout=2,
        terminal_only=False,
        minimum_attempts=1,
    )
    assert routed["id"] == orchestration["id"]
    assert waiting["parent_state"] == "waiting"
    assert calls[0][1] == [
        {
            "role": "human",
            "content": "Prioritize the Windows behavior in the final answer.",
            "source_event_id": "steering:later-user-message",
        }
    ]

    agent_runs.finish_agent_run(
        child["id"],
        "completed",
        summary="Windows result",
    )
    final = orchestrator.wait_for_parent(orchestration["id"], timeout=2)
    assert final["status"] == "completed"
    assert len(calls) == 2
    assert "Windows result" in calls[-1][0]


def test_v2_stop_all_wakes_parent_for_natural_final(tmp_path, monkeypatch):
    _tasks, agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = _orchestration(orchestrator, version=2)
    child = _run(agent_runs, "stop-v2")
    orchestrator.register_member(orchestration["id"], child["id"], required=True)
    calls: list[str] = []

    def parent_executor(_row, context, _tools, _config):
        calls.append(context)
        if "CHILD_TERMINAL" in context:
            return "I stopped the delegated work and left the parent thread in a safe state."
        return "I am stopping the delegated work now."

    orchestrator.set_test_executors(
        parent=parent_executor,
        synthesis=lambda *_args: pytest.fail("v2 must not use synthesis"),
        delivery=lambda *_args: True,
    )
    orchestrator.complete_parent_pass(
        orchestration["id"],
        "The delegated operation is running.",
        continuation_state={
            "config": {"configurable": {"thread_id": "parent-thread"}},
            "enabled_tool_names": ["agents"],
        },
        foreground=True,
    )
    monkeypatch.setattr(
        "row_bot.agent_runner.stop_agent_run",
        lambda run_id: agent_runs.finish_agent_run(
            run_id,
            "stopped",
            status_message="Stop requested",
        ),
    )

    orchestrator.stop_orchestration(orchestration["id"])
    final = orchestrator.wait_for_parent(orchestration["id"], timeout=2)

    assert final["status"] == "completed_partial"
    assert final["parent_state"] == "completed"
    assert any("STOP_REQUESTED" in context for context in calls)
    assert any("CHILD_TERMINAL" in context for context in calls)


def test_v2_individual_stop_then_stop_all_wakes_parent_for_natural_final(
    tmp_path,
    monkeypatch,
):
    _tasks, agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = _orchestration(orchestrator, version=2)
    first = _run(agent_runs, "stop-first-v2")
    second = _run(agent_runs, "stop-second-v2")
    orchestrator.register_member(orchestration["id"], first["id"], required=True)
    orchestrator.register_member(orchestration["id"], second["id"], required=True)
    calls: list[str] = []

    def parent_executor(_row, context, _tools, _config):
        calls.append(context)
        if "CHILD_TERMINAL" in context and second["id"] in context:
            return "I stopped both delegated checks and completed the parent response."
        return "I am still waiting for the remaining delegated check."

    orchestrator.set_test_executors(
        parent=parent_executor,
        synthesis=lambda *_args: pytest.fail("v2 must not use synthesis"),
        delivery=lambda *_args: True,
    )
    orchestrator.complete_parent_pass(
        orchestration["id"],
        "The delegated operations are running.",
        continuation_state={
            "config": {"configurable": {"thread_id": "parent-thread"}},
            "enabled_tool_names": ["agents"],
        },
        foreground=True,
    )
    monkeypatch.setattr(
        "row_bot.agent_runner.stop_agent_run",
        agent_runs.stop_agent_run,
    )

    agent_runs.stop_agent_run(first["id"])
    orchestrator.stop_orchestration(orchestration["id"])
    final = orchestrator.wait_for_parent(orchestration["id"], timeout=2)

    assert final["status"] == "completed_partial"
    assert final["parent_state"] == "completed"
    assert any("STOP_REQUESTED" in context for context in calls)
    assert any(
        "CHILD_TERMINAL" in context and second["id"] in context for context in calls
    )


def test_v2_parent_lease_serializes_one_thread_runner(tmp_path, monkeypatch):
    _tasks, _agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = _orchestration(orchestrator, version=2)
    orchestrator.arm_parent_wait(
        orchestration["id"],
        continuation_state={
            "config": {"configurable": {"thread_id": "parent-thread"}},
            "enabled_tool_names": ["agents"],
        },
    )
    orchestrator.record_thread_event(
        orchestration["id"],
        kind="parent_steering",
        content="Use the completed evidence.",
        source_event_id="steering:lease-test",
        request_wake=False,
    )

    first = orchestrator._claim_parent_lease(orchestration["id"])
    assert first is not None
    assert orchestrator._claim_parent_lease(orchestration["id"]) is None

    _claimed, lease_owner = first
    orchestrator._release_parent_lease(orchestration["id"], lease_owner)


def test_v2_provider_failure_keeps_event_for_bounded_retry(tmp_path, monkeypatch):
    _tasks, _agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = _orchestration(orchestrator, version=2)
    orchestrator.arm_parent_wait(
        orchestration["id"],
        continuation_state={
            "config": {"configurable": {"thread_id": "parent-thread"}},
            "enabled_tool_names": ["agents"],
        },
    )
    calls: list[str] = []

    def fail_once(_orchestration, context, _tools, _config):
        calls.append(context)
        raise RuntimeError("scripted provider outage")

    orchestrator.set_test_executors(parent=fail_once)
    orchestrator.record_thread_event(
        orchestration["id"],
        kind="parent_steering",
        content="Retry this same event.",
        source_event_id="steering:provider-retry",
    )
    failed = orchestrator.wait_for_parent(
        orchestration["id"],
        terminal_only=False,
        minimum_attempts=1,
    )
    assert failed["parent_state"] == "waiting"
    assert "provider outage" in failed["error_message"]
    assert len(orchestrator.pending_thread_events(orchestration["id"])) == 1

    orchestrator.set_test_executors(parent=lambda *_args: "Recovered parent final.")
    assert orchestrator.request_parent_wake(orchestration["id"])
    recovered = orchestrator.wait_for_parent(
        orchestration["id"],
        minimum_attempts=2,
    )

    assert recovered["status"] == "completed"
    assert len(calls) == 1
    assert orchestrator.pending_thread_events(orchestration["id"]) == []
    finals = orchestrator.list_messages(
        orchestration["id"],
        kinds=["parent_final"],
    )
    assert [row["content"] for row in finals] == ["Recovered parent final."]


def test_v2_parent_outbox_retry_reuses_key_and_records_attempts(
    tmp_path,
    monkeypatch,
):
    _tasks, _agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = _orchestration(orchestrator, version=2)
    calls: list[str] = []

    def delivery(_row, _kind, _text, key):
        calls.append(key)
        return len(calls) > 1

    orchestrator.set_test_executors(delivery=delivery)
    orchestrator.complete_parent_pass(
        orchestration["id"],
        "Durable final.",
        continuation_state={
            "config": {"configurable": {"thread_id": "parent-thread"}},
            "enabled_tool_names": [],
        },
        foreground=False,
    )
    first = orchestrator.list_messages(
        orchestration["id"],
        kinds=["parent_final"],
    )[0]
    assert first["delivery_status"] == "failed"
    assert first["attempt_count"] == 1
    assert first["last_error"] == "Delivery executor returned false."

    assert orchestrator.retry_pending_deliveries() == 1
    final = orchestrator.list_messages(
        orchestration["id"],
        kinds=["parent_final"],
    )[0]
    assert final["delivery_status"] == "delivered"
    assert final["attempt_count"] == 2
    assert calls == [first["id"], first["id"]]


def test_v2_duplicate_callbacks_enqueue_one_parent_event(tmp_path, monkeypatch):
    _tasks, agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = _orchestration(orchestrator, version=2)
    child = _run(agent_runs, "duplicate-child", status="running")
    orchestrator.register_member(orchestration["id"], child["id"], required=True)
    orchestrator.arm_parent_wait(
        orchestration["id"],
        continuation_state={
            "config": {"configurable": {"thread_id": "parent-thread"}},
            "enabled_tool_names": ["agents"],
        },
    )
    orchestrator.set_test_executors(parent=lambda *_args: "One final answer.")
    completed = agent_runs.finish_agent_run(
        child["id"],
        "completed",
        summary="Stable result",
    )

    assert orchestrator.handle_run_terminal(completed)
    assert orchestrator.handle_run_terminal(completed)
    final = orchestrator.wait_for_parent(orchestration["id"])

    assert final["status"] == "completed"
    terminal_events = [
        row
        for row in orchestrator.list_messages(orchestration["id"])
        if row["kind"] == "event.child_terminal"
    ]
    assert len(terminal_events) == 1
    assert len(
        orchestrator.list_messages(orchestration["id"], kinds=["parent_final"])
    ) == 1


def test_v2_joining_already_terminal_run_materializes_event(tmp_path, monkeypatch):
    _tasks, agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = _orchestration(orchestrator, version=2)
    child = _run(agent_runs, "already-done", status="completed")
    orchestrator.set_test_executors(parent=lambda *_args: "Used the earlier result.")

    orchestrator.register_member(orchestration["id"], child["id"], required=True)
    assert len(orchestrator.pending_thread_events(orchestration["id"])) == 1
    orchestrator.arm_parent_wait(
        orchestration["id"],
        continuation_state={
            "config": {"configurable": {"thread_id": "parent-thread"}},
            "enabled_tool_names": ["agents"],
        },
    )
    final = orchestrator.wait_for_parent(orchestration["id"])

    assert final["status"] == "completed"
    assert orchestrator.list_messages(
        orchestration["id"],
        kinds=["parent_final"],
    )[0]["content"] == "Used the earlier result."


def test_v2_parent_wake_rebinds_exact_designer_project_and_rejects_drift(
    tmp_path,
    monkeypatch,
):
    _tasks, _agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    import row_bot.designer.session as designer_session
    import row_bot.threads as threads

    monkeypatch.setattr(threads, "_get_thread_developer_workspace", lambda _tid: "")
    monkeypatch.setattr(threads, "_get_thread_project_workspace", lambda _tid: "")
    monkeypatch.setattr(
        threads,
        "_get_thread_project_id",
        lambda _tid: "recorded-project",
    )
    bound: list[tuple[str, str]] = []
    monkeypatch.setattr(
        designer_session,
        "bind_project_to_thread",
        lambda thread_id, project_id: bound.append((thread_id, project_id)),
    )
    orchestration = {
        "parent_thread_id": "parent-thread",
    }
    config = {
        "configurable": {
            "designer_project_id": "recorded-project",
            "designer_mode": "deck",
        }
    }

    orchestrator._bind_recorded_parent_resources(orchestration, config)
    assert bound == [("parent-thread", "recorded-project")]
    assert config["configurable"]["designer_mode"] == "deck"

    monkeypatch.setattr(
        threads,
        "_get_thread_project_id",
        lambda _tid: "new-visible-project",
    )
    with pytest.raises(orchestrator.OrchestrationError, match="binding changed"):
        orchestrator._bind_recorded_parent_resources(orchestration, config)
