from __future__ import annotations

import importlib
import sys
import threading
import time


def _fresh_lock_modules(tmp_path, monkeypatch):
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
        "row_bot.developer.storage",
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
    return threads, agent_runs, agent_runner


def test_agent_write_lock_helpers_are_single_owner(tmp_path, monkeypatch):
    _threads, agent_runs, _agent_runner = _fresh_lock_modules(tmp_path, monkeypatch)

    assert agent_runs.acquire_agent_write_lock("thread:parent", "run-1") is True
    assert agent_runs.acquire_agent_write_lock("thread:parent", "run-2") is False
    lock = agent_runs.get_agent_write_lock("thread:parent")
    assert lock["run_id"] == "run-1"
    assert len(agent_runs.list_agent_write_locks()) == 1

    assert agent_runs.release_agent_write_lock("thread:parent") is True
    assert agent_runs.get_agent_write_lock("thread:parent") is None
    assert agent_runs.acquire_agent_write_lock("thread:parent", "run-2") is True
    assert agent_runs.release_agent_write_lock(run_id="run-2") is True


def test_write_capable_agents_queue_until_lock_released(tmp_path, monkeypatch):
    threads, agent_runs, agent_runner = _fresh_lock_modules(tmp_path, monkeypatch)
    parent_thread_id = threads.create_thread("Parent")
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()

    def fake_invoke(prompt, enabled_tool_names, config, *, stop_event):
        if "First writer" in prompt:
            first_started.set()
            assert release_first.wait(timeout=2.0)
            return "first done"
        second_started.set()
        return "second done"

    monkeypatch.setattr(agent_runner, "_invoke_agent", fake_invoke)

    first = agent_runner.spawn_agent_run(
        "First writer",
        parent_thread_id=parent_thread_id,
        profile="worker",
        enabled_tool_names=[],
        wait=False,
    )
    assert first_started.wait(timeout=1.0)

    second = agent_runner.spawn_agent_run(
        "Second writer",
        parent_thread_id=parent_thread_id,
        profile="worker",
        enabled_tool_names=[],
        wait=False,
    )
    time.sleep(0.15)

    queued = agent_runs.get_agent_run(second["id"])
    assert queued["status"] == "queued"
    assert queued["status_message"] == "Queued for writer lock"
    assert second_started.is_set() is False

    release_first.set()
    first_final = agent_runner.wait_for_agent_run(first["id"], timeout=2.0)
    second_final = agent_runner.wait_for_agent_run(second["id"], timeout=2.0)

    assert first_final["status"] == "completed"
    assert second_final["status"] == "completed"
    assert second_started.is_set() is True
    assert agent_runs.list_agent_write_locks() == []
    assert first_final["write_lock_key"] == f"thread:{parent_thread_id}"
    assert second_final["write_lock_key"] == f"thread:{parent_thread_id}"


def test_distinct_developer_workspaces_allow_concurrent_writers(tmp_path, monkeypatch):
    threads, agent_runs, agent_runner = _fresh_lock_modules(tmp_path, monkeypatch)
    parent_thread_id = threads.create_thread("Parent")
    first_started = threading.Event()
    second_started = threading.Event()
    both_started = threading.Event()
    release_children = threading.Event()

    def fake_invoke(prompt, enabled_tool_names, config, *, stop_event):
        started = first_started if "First workspace writer" in prompt else second_started
        started.set()
        if first_started.is_set() and second_started.is_set():
            both_started.set()
        assert both_started.wait(timeout=2.0)
        assert release_children.wait(timeout=2.0)
        return "done"

    monkeypatch.setattr(agent_runner, "_invoke_agent", fake_invoke)

    first = agent_runner.spawn_agent_run(
        "First workspace writer",
        parent_thread_id=parent_thread_id,
        profile="worker",
        developer_workspace_id="dev-first-folder",
        enabled_tool_names=[],
        wait=False,
    )
    second = agent_runner.spawn_agent_run(
        "Second workspace writer",
        parent_thread_id=parent_thread_id,
        profile="worker",
        developer_workspace_id="dev-second-folder",
        enabled_tool_names=[],
        wait=False,
    )

    assert both_started.wait(timeout=1.0)
    first_running = agent_runs.get_agent_run(first["id"])
    second_running = agent_runs.get_agent_run(second["id"])
    assert first_running["status"] == "running"
    assert second_running["status"] == "running"
    assert first_running["write_lock_key"] == "developer:dev-first-folder"
    assert second_running["write_lock_key"] == "developer:dev-second-folder"
    assert first_running["write_lock_key"] != second_running["write_lock_key"]
    assert {lock["lock_key"] for lock in agent_runs.list_agent_write_locks()} == {
        "developer:dev-first-folder",
        "developer:dev-second-folder",
    }

    release_children.set()
    first_final = agent_runner.wait_for_agent_run(first["id"], timeout=2.0)
    second_final = agent_runner.wait_for_agent_run(second["id"], timeout=2.0)

    assert first_final["status"] == "completed"
    assert second_final["status"] == "completed"
    assert agent_runs.list_agent_write_locks() == []


def test_queued_parent_message_is_applied_before_agent_starts(tmp_path, monkeypatch):
    threads, agent_runs, agent_runner = _fresh_lock_modules(tmp_path, monkeypatch)
    parent_thread_id = threads.create_thread("Parent")
    release_first = threading.Event()
    captured_prompts: list[str] = []

    def fake_invoke(prompt, enabled_tool_names, config, *, stop_event):
        captured_prompts.append(prompt)
        if "First writer" in prompt:
            assert release_first.wait(timeout=2.0)
            return "first done"
        return "second done"

    monkeypatch.setattr(agent_runner, "_invoke_agent", fake_invoke)

    first = agent_runner.spawn_agent_run(
        "First writer",
        parent_thread_id=parent_thread_id,
        profile="worker",
        enabled_tool_names=[],
        wait=False,
    )
    time.sleep(0.1)
    second = agent_runner.spawn_agent_run(
        "Second writer",
        parent_thread_id=parent_thread_id,
        profile="worker",
        enabled_tool_names=[],
        wait=False,
    )
    time.sleep(0.15)
    assert agent_runs.get_agent_run(second["id"])["status"] == "queued"

    agent_runs.append_agent_parent_message(second["id"], "Use the narrow patch.")
    release_first.set()

    assert agent_runner.wait_for_agent_run(first["id"], timeout=2.0)["status"] == "completed"
    assert agent_runner.wait_for_agent_run(second["id"], timeout=2.0)["status"] == "completed"
    second_prompt = next(prompt for prompt in captured_prompts if "Second writer" in prompt)
    assert "[Parent follow-up before start]" in second_prompt
    assert "Use the narrow patch." in second_prompt


def test_read_only_agents_do_not_take_writer_lock(tmp_path, monkeypatch):
    threads, agent_runs, agent_runner = _fresh_lock_modules(tmp_path, monkeypatch)
    parent_thread_id = threads.create_thread("Parent")

    monkeypatch.setattr(
        agent_runner,
        "_invoke_agent",
        lambda prompt, enabled_tool_names, config, *, stop_event: "read-only done",
    )

    run = agent_runner.spawn_agent_run(
        "Read only review",
        parent_thread_id=parent_thread_id,
        profile="review",
        enabled_tool_names=[],
        wait=True,
    )

    assert run["status"] == "completed"
    assert run["write_lock_key"] == ""
    assert agent_runs.list_agent_write_locks() == []
