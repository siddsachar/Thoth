from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path


AGENT_RUN_TABLES = {
    "agent_profiles",
    "agent_runs",
    "agent_run_events",
    "agent_run_edges",
    "agent_write_locks",
    "thread_goals",
}


def _fresh_agent_run_modules(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(data_dir))
    for name in (
        "row_bot.tasks",
        "row_bot.agent_profiles",
        "row_bot.agent_settings",
        "row_bot.agent_runs",
    ):
        sys.modules.pop(name, None)

    import row_bot.tasks as tasks
    import row_bot.agent_profiles as agent_profiles
    import row_bot.agent_runs as agent_runs

    tasks = importlib.reload(tasks)
    agent_profiles = importlib.reload(agent_profiles)
    agent_runs = importlib.reload(agent_runs)
    return agent_runs, agent_profiles, tasks, data_dir


def _tables(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }


def _columns(db_path: Path, table: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_agent_run_schema_creation_and_migration(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "tasks.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE agent_runs ("
            "id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.commit()

    agent_runs, _profiles, _tasks, _data_dir = _fresh_agent_run_modules(
        tmp_path,
        monkeypatch,
    )
    agent_runs.ensure_agent_run_schema(force=True)

    assert AGENT_RUN_TABLES <= _tables(db_path)
    assert {
        "kind",
        "status",
        "profile_snapshot_json",
        "settings_snapshot_json",
        "resume_state_json",
        "root_run_id",
        "model_iterations_max",
        "model_iterations_used",
        "terminal_reason",
        "heartbeat_at",
        "active_seconds",
    } <= _columns(db_path, "agent_runs")
    assert {"active_run_id", "continuation_key"} <= _columns(db_path, "thread_goals")


def test_agent_run_event_edge_crud(tmp_path, monkeypatch):
    agent_runs, _profiles, _tasks, _data_dir = _fresh_agent_run_modules(
        tmp_path,
        monkeypatch,
    )

    parent = agent_runs.create_agent_run(
        run_id="parent-run",
        kind="subagent",
        status="running",
        parent_thread_id="parent-thread",
        thread_id="parent-thread",
        display_name="Parent",
        profile_id="plan",
    )
    child = agent_runs.create_agent_run(
        run_id="child-run",
        kind="subagent",
        status="queued",
        parent_run_id=parent["id"],
        parent_thread_id="parent-thread",
        thread_id="child-thread",
        display_name="Child",
        profile_id="quality_reviewer",
        tools_override=["shell"],
        skills_override=["tests"],
    )

    edge = agent_runs.create_agent_run_edge(parent["id"], child["id"], "delegated")
    started = agent_runs.start_agent_run(child["id"])
    event = agent_runs.append_agent_event(
        child["id"],
        "tool.completed",
        {"tool": "pytest", "exit_code": 0},
    )
    finished = agent_runs.finish_agent_run(
        child["id"],
        "completed",
        summary="Child done",
        result_json={"ok": True},
    )

    assert edge["relation"] == "delegated"
    assert event["payload_json"]["tool"] == "pytest"
    assert started["status"] == "running"
    assert finished["status"] == "completed"
    assert finished["profile_id"] == "builtin:review"
    assert finished["profile_snapshot_json"]["slug"] == "review"
    assert finished["tools_override"] == ["shell"]
    assert finished["result_json"] == {"ok": True}
    assert finished["root_run_id"] == "child-run"
    assert finished["model_iterations_max"] == 90

    children = agent_runs.list_child_runs(parent_run_id=parent["id"])
    assert [run["id"] for run in children] == ["child-run"]
    assert children[0]["edge_relation"] == "delegated"

    runs = agent_runs.list_agent_runs(parent_thread_id="parent-thread", kind="subagent")
    assert {run["id"] for run in runs} == {"parent-run", "child-run"}
    event_types = {item["type"] for item in agent_runs.get_agent_events(child["id"])}
    assert {"run.created", "run.started", "tool.completed", "run.completed"} <= event_types


def test_parent_messages_return_the_newest_limited_window(tmp_path, monkeypatch):
    agent_runs, _profiles, _tasks, _data_dir = _fresh_agent_run_modules(
        tmp_path,
        monkeypatch,
    )
    run = agent_runs.create_agent_run(run_id="guided-run", status="running")
    for index in range(5):
        agent_runs.append_agent_event(
            run["id"],
            "tool.completed",
            {"index": index},
        )
    for message in ("first guidance", "second guidance", "latest guidance"):
        agent_runs.append_agent_parent_message(run["id"], message)

    assert agent_runs.get_agent_parent_messages(run["id"], limit=2) == [
        "second guidance",
        "latest guidance",
    ]


def test_agent_run_budget_progress_and_terminal_reason_are_additive(tmp_path, monkeypatch):
    agent_runs, _profiles, _tasks, _data_dir = _fresh_agent_run_modules(tmp_path, monkeypatch)
    run = agent_runs.create_agent_run(run_id="budget-run", status="running")

    updated = agent_runs.update_agent_budget_progress(
        run["id"],
        used_iterations=7,
        max_iterations=90,
    )
    finished = agent_runs.finish_agent_run(
        run["id"],
        "blocked",
        summary="Incomplete",
        terminal_reason="budget_exhausted",
    )

    assert updated["model_iterations_used"] == 7
    assert updated["model_iterations_max"] == 90
    assert updated["heartbeat_at"]
    assert updated["active_seconds"] >= 0
    assert finished["status"] == "blocked"
    assert finished["terminal_reason"] == "budget_exhausted"


def test_stop_agent_run_marks_durable_stop_state(tmp_path, monkeypatch):
    agent_runs, _profiles, _tasks, _data_dir = _fresh_agent_run_modules(
        tmp_path,
        monkeypatch,
    )
    run = agent_runs.create_agent_run(
        run_id="stop-me",
        kind="subagent",
        status="running",
        display_name="Stop Me",
    )

    stopped = agent_runs.stop_agent_run(run["id"])

    assert stopped["status"] == "stopped"
    assert stopped["stop_requested"] is True
    assert stopped["finished_at"]
    assert agent_runs.get_agent_events(run["id"])[-1]["type"] == "run.stopped"


def test_finish_agent_run_does_not_overwrite_requested_stop(tmp_path, monkeypatch):
    agent_runs, _profiles, _tasks, _data_dir = _fresh_agent_run_modules(
        tmp_path,
        monkeypatch,
    )
    run = agent_runs.create_agent_run(
        run_id="late-success-after-stop",
        kind="subagent",
        status="running",
        display_name="Stop Me",
    )

    stopped = agent_runs.stop_agent_run(run["id"])
    finished = agent_runs.finish_agent_run(
        run["id"],
        "completed",
        summary="Late success should not win.",
        result_json={"response": "late success"},
    )

    assert stopped["status"] == "stopped"
    assert finished["status"] == "stopped"
    assert finished["stop_requested"] is True
    assert finished["summary"] == ""
    event_types = [event["type"] for event in agent_runs.get_agent_events(run["id"])]
    assert event_types.count("run.stopped") == 1
    assert "run.completed" not in event_types


def test_startup_recovery_stops_stale_runs_and_releases_locks(tmp_path, monkeypatch):
    agent_runs, _profiles, _tasks, _data_dir = _fresh_agent_run_modules(
        tmp_path,
        monkeypatch,
    )
    import row_bot.threads as threads

    threads = importlib.reload(threads)
    parent_thread_id = threads.create_thread("Recovery parent")
    queued_keep = agent_runs.create_agent_run(
        run_id="queued-keep",
        status="queued",
        parent_thread_id=parent_thread_id,
        display_name="Queued Keep",
    )
    queued_orphan = agent_runs.create_agent_run(
        run_id="queued-orphan",
        status="queued",
        parent_thread_id="missing-parent",
        display_name="Queued Orphan",
    )
    running = agent_runs.create_agent_run(
        run_id="running-stale",
        status="running",
        parent_thread_id=parent_thread_id,
        display_name="Running Stale",
    )
    approval = agent_runs.create_agent_run(
        run_id="approval-keep",
        status="waiting_approval",
        parent_thread_id=parent_thread_id,
        display_name="Approval Keep",
        resume_state_json={"resume_token": "token"},
    )
    approval_orphan = agent_runs.create_agent_run(
        run_id="approval-orphan",
        status="waiting_approval",
        parent_thread_id=parent_thread_id,
        display_name="Approval Orphan",
    )
    assert agent_runs.acquire_agent_write_lock("thread:recovery", running["id"])

    result = agent_runs.recover_stale_agent_runs()

    assert result["locks_released"] == 1
    assert agent_runs.list_agent_write_locks() == []
    assert agent_runs.get_agent_run(queued_keep["id"])["status"] == "stopped"
    assert "queued child capacity" in agent_runs.get_agent_run(queued_keep["id"])["status_message"]
    assert agent_runs.get_agent_run(queued_orphan["id"])["status"] == "stopped"
    assert agent_runs.get_agent_run(running["id"])["status"] == "stopped"
    assert agent_runs.get_agent_run(approval["id"])["status"] == "waiting_approval"
    assert agent_runs.get_agent_run(approval_orphan["id"])["status"] == "stopped"


def test_parent_thread_delete_cascades_chat_agent_state_but_keeps_workflows(tmp_path, monkeypatch):
    agent_runs, _profiles, tasks, _data_dir = _fresh_agent_run_modules(
        tmp_path,
        monkeypatch,
    )
    import row_bot.threads as threads

    threads = importlib.reload(threads)
    parent_thread_id = threads.create_thread("Cascade parent")
    child_thread_id = threads.create_thread("Cascade child", thread_type="agent_child")
    child = agent_runs.create_agent_run(
        run_id="child-run",
        kind="subagent",
        status="running",
        parent_thread_id=parent_thread_id,
        thread_id=child_thread_id,
        display_name="Child Run",
    )
    goal = agent_runs.create_agent_run(
        run_id="goal-run",
        kind="goal",
        status="running",
        thread_id=parent_thread_id,
        display_name="Goal Run",
    )
    workflow = agent_runs.create_agent_run(
        run_id="workflow-run",
        kind="workflow",
        status="completed",
        thread_id=parent_thread_id,
        task_id="task-1",
        display_name="Workflow Audit",
    )
    agent_runs.append_agent_event(child["id"], "summary.updated", {"summary": "done"})
    agent_runs.create_agent_run_edge("parent-run", child["id"], "delegated")
    assert agent_runs.acquire_agent_write_lock("thread:cascade", child["id"])
    _resume_token, approval_id = tasks.create_approval_request(
        run_id="legacy-run",
        task_id="",
        step_id="agent_interrupt",
        message="Approve child",
        agent_run_id=child["id"],
        resume_kind="agent_run",
        source_thread_id=child_thread_id,
    )

    threads._delete_thread(parent_thread_id)
    # A first Delete durably stops formerly running child records. An explicit
    # retry observes their terminal acknowledgment and completes the cascade.
    assert agent_runs.get_agent_run(child["id"])["status"] == "stopped"
    assert threads._thread_exists(parent_thread_id)
    threads._delete_thread(parent_thread_id)

    assert agent_runs.get_agent_run(child["id"]) is None
    assert agent_runs.get_agent_run(goal["id"]) is None
    assert agent_runs.get_agent_run(workflow["id"]) is not None
    assert not threads._thread_exists(child_thread_id)
    assert agent_runs.list_agent_write_locks() == []
    conn = tasks._get_conn()
    try:
        row = conn.execute(
            "SELECT status FROM approval_requests WHERE id = ?",
            (approval_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is None


def test_get_agent_run_for_child_thread(tmp_path, monkeypatch):
    agent_runs, _profiles, _tasks, _data_dir = _fresh_agent_run_modules(
        tmp_path,
        monkeypatch,
    )
    import row_bot.threads as threads

    threads = importlib.reload(threads)
    parent_thread_id = threads.create_thread("Lookup parent")
    child_thread_id = threads.create_thread("Lookup child", thread_type="agent_child")
    agent_runs.create_agent_run(
        run_id="lookup-child",
        kind="subagent",
        status="running",
        parent_thread_id=parent_thread_id,
        thread_id=child_thread_id,
        display_name="Lookup Child",
    )

    row = agent_runs.get_agent_run_for_thread(child_thread_id)
    assert row is not None
    assert row["id"] == "lookup-child"
    assert row["parent_thread_id"] == parent_thread_id


def test_workflow_run_is_mirrored_to_agent_runs(tmp_path, monkeypatch):
    agent_runs, _profiles, tasks, _data_dir = _fresh_agent_run_modules(
        tmp_path,
        monkeypatch,
    )

    task_id = tasks.create_task(
        "Profiled workflow",
        steps=[
            {
                "type": "prompt",
                "prompt": "Review this change.",
            }
        ],
        agent_profile_id="quality_reviewer",
    )
    task = tasks.get_task(task_id)
    run_id = tasks._record_run_start(
        task_id,
        "workflow-thread",
        1,
        task_name=task["name"],
        task_icon=task["icon"],
    )

    with sqlite3.connect(Path(tasks._DB_PATH)) as conn:
        legacy = conn.execute(
            "SELECT * FROM task_runs WHERE id = ?",
            (run_id,),
        ).fetchone()

    mirrored = agent_runs.get_agent_run(run_id)
    assert legacy is not None
    assert mirrored["kind"] == "workflow"
    assert mirrored["task_id"] == task_id
    assert mirrored["thread_id"] == "workflow-thread"
    assert mirrored["status"] == "running"
    assert mirrored["profile_id"] == "builtin:review"
    assert mirrored["profile_snapshot_json"]["slug"] == "review"
    assert mirrored["max_turns"] == 1

    tasks._update_run_progress(run_id, 1)
    progressed = agent_runs.get_agent_run(run_id)
    assert progressed["turns_used"] == 1

    tasks._finish_run(run_id, "completed", status_message="Delivered")
    finished = agent_runs.get_agent_run(run_id)
    assert finished["status"] == "completed"
    assert finished["status_message"] == "Delivered"
    assert finished["summary"] == "Delivered"
    event_types = {item["type"] for item in agent_runs.get_agent_events(run_id)}
    assert {"run.started", "turn.completed", "run.completed"} <= event_types
