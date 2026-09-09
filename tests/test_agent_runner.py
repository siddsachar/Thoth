from __future__ import annotations

import importlib
import contextvars
from functools import wraps
import json
import sys
import threading
import time
from pathlib import Path

import pytest


def _fresh_agent_runner_modules(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(data_dir))
    # Keep package and dotted imports on one storage owner; isolate its paths
    # without leaving a stale package attribute after sys.modules eviction.
    workspace_storage = importlib.import_module("row_bot.developer.storage")
    monkeypatch.setattr(workspace_storage, "DATA_DIR", data_dir)
    monkeypatch.setattr(workspace_storage, "DEVELOPER_DIR", data_dir / "developer")
    monkeypatch.setattr(workspace_storage, "WORKSPACES_PATH", data_dir / "developer" / "workspaces.json")
    for name in (
        "row_bot.tasks",
        "row_bot.threads",
        "row_bot.agent_profiles",
        "row_bot.agent_settings",
        "row_bot.agent_runs",
        "row_bot.agent_context",
        "row_bot.agent_runner",
    ):
        sys.modules.pop(name, None)

    import row_bot.tasks as tasks
    import row_bot.threads as threads
    import row_bot.agent_profiles as agent_profiles
    import row_bot.agent_runs as agent_runs
    import row_bot.agent_context as agent_context
    import row_bot.agent_runner as agent_runner

    tasks = importlib.reload(tasks)
    threads = importlib.reload(threads)
    agent_profiles = importlib.reload(agent_profiles)
    agent_runs = importlib.reload(agent_runs)
    agent_context = importlib.reload(agent_context)
    agent_runner = importlib.reload(agent_runner)
    return agent_runner, agent_runs, agent_profiles, agent_context, threads


def _workspace(tmp_path, workspace_id, folder):
    from row_bot.developer.state import DeveloperWorkspace
    from row_bot.developer.storage import save_workspace
    path = tmp_path / folder
    path.mkdir(exist_ok=True)
    save_workspace(DeveloperWorkspace(id=workspace_id, name=workspace_id, path=str(path)))
    return path


def _isolated_runtime_context(function):
    @wraps(function)
    def isolated(*args, **kwargs):
        return contextvars.Context().run(function, *args, **kwargs)
    return isolated


def test_spawn_agent_run_creates_child_thread_and_completes(tmp_path, monkeypatch):
    agent_runner, agent_runs, _profiles, _context, threads = _fresh_agent_runner_modules(
        tmp_path,
        monkeypatch,
    )
    parent_thread_id = threads.create_thread(
        "Parent",
        approval_mode="block",
        model_override="model:test",
    )
    captured = {}

    def fake_invoke(prompt, enabled_tool_names, config, *, stop_event):
        captured["prompt"] = prompt
        captured["tools"] = enabled_tool_names
        captured["config"] = config
        captured["stop_event"] = stop_event
        return "review complete"

    monkeypatch.setattr(agent_runner, "_invoke_agent", fake_invoke)

    run = agent_runner.spawn_agent_run(
        "Review the auth change.",
        parent_thread_id=parent_thread_id,
        profile="quality_reviewer",
        context="Changed files: auth.py",
        enabled_tool_names=["shell", "agents", "filesystem"],
        wait=True,
    )

    assert run["status"] == "completed"
    assert run["summary"] == "review complete"
    assert run["profile_id"] == "builtin:review"
    assert run["profile_snapshot_json"]["slug"] == "review"
    assert run["parent_thread_id"] == parent_thread_id
    assert run["context_mode"] == "focused"
    assert "Review the auth change." in captured["prompt"]
    assert "PROFILE INSTRUCTIONS" in captured["prompt"]
    assert "Runtime model override: model:test" in captured["prompt"]
    assert "Do not call row_bot_update_setting(setting='model')" in captured["prompt"]
    assert "agents" not in captured["tools"]
    assert captured["config"]["configurable"]["runtime_surface"] == "agent_child"
    assert captured["config"]["configurable"]["approval_mode"] == "block"
    assert captured["config"]["configurable"]["model_override"] == "model:test"
    assert captured["config"]["configurable"]["agent_run_id"] == run["id"]
    assert "recursion_limit" not in captured["config"]
    assert run["root_run_id"] == run["id"]
    assert run["depth"] == 1

    child_profile = threads._get_thread_agent_profile(run["thread_id"])
    assert child_profile == {"id": "builtin:review", "slug": "review"}
    assert threads._get_thread_type(run["thread_id"]) == "agent_child"
    event_types = {event["type"] for event in agent_runs.get_agent_events(run["id"])}
    assert {"run.created", "run.started", "turn.started", "turn.completed", "run.completed"} <= event_types


def test_child_creation_rolls_back_when_parent_deletion_wins_the_race(
    tmp_path,
    monkeypatch,
) -> None:
    agent_runner, agent_runs, _profiles, _context, threads = _fresh_agent_runner_modules(
        tmp_path,
        monkeypatch,
    )
    cleanup = importlib.reload(importlib.import_module("row_bot.thread_cleanup"))
    parent_thread_id = threads.create_thread("Parent being deleted")
    deletion_token = ""
    original_create_agent_run = agent_runs.create_agent_run

    def _create_then_start_parent_delete(**kwargs):
        nonlocal deletion_token
        run = original_create_agent_run(**kwargs)
        deletion_token = cleanup._mark_thread_deleting(parent_thread_id)
        return run

    monkeypatch.setattr(
        agent_runs,
        "create_agent_run",
        _create_then_start_parent_delete,
    )
    monkeypatch.setattr(
        agent_runner,
        "_invoke_agent",
        lambda *_args, **_kwargs: pytest.fail("The child executor must not start"),
    )

    with pytest.raises(agent_runner.AgentRunnerError, match="parent conversation was deleted"):
        agent_runner.spawn_agent_run(
            "Do not start after parent deletion.",
            parent_thread_id=parent_thread_id,
            profile="quality_reviewer",
            enabled_tool_names=["filesystem"],
            wait=True,
        )

    child_rows = [
        row
        for row in threads._list_threads(include_details=True)
        if len(row) > 6 and row[6] == "agent_child"
    ]
    assert child_rows == []
    assert agent_runs.list_agent_runs(parent_thread_id=parent_thread_id) == []
    cleanup.finish_thread_deletion(parent_thread_id, deletion_token)
    assert threads._thread_exists(parent_thread_id) is False


def test_child_dispatcher_queues_fifo_at_global_and_parent_capacity(tmp_path, monkeypatch):
    agent_runner, agent_runs, _profiles, _context, threads = _fresh_agent_runner_modules(
        tmp_path, monkeypatch
    )
    from row_bot.agent_settings import AgentRuntimeSettings, save_agent_runtime_settings

    save_agent_runtime_settings(
        AgentRuntimeSettings(max_concurrent_children=1, max_active_children_global=1)
    )
    parent_thread_id = threads.create_thread("Capacity parent")
    first_started = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()
    order: list[str] = []

    def fake_invoke(prompt, enabled_tool_names, config, *, stop_event):
        del prompt, enabled_tool_names, stop_event
        run_id = config["configurable"]["agent_run_id"]
        order.append(run_id)
        if len(order) == 1:
            first_started.set()
            assert release_first.wait(2)
        else:
            second_started.set()
        return "done"

    monkeypatch.setattr(agent_runner, "_invoke_agent", fake_invoke)
    first = agent_runner.spawn_agent_run("First", parent_thread_id=parent_thread_id)
    assert first_started.wait(2)
    first_live = agent_runs.get_agent_run(first["id"])
    assert first_live["status"] == "running"
    assert first_live["status_message"] == ""
    second = agent_runner.spawn_agent_run("Second", parent_thread_id=parent_thread_id)

    assert not second_started.wait(0.15)
    second_queued = agent_runs.get_agent_run(second["id"])
    assert second_queued["status"] == "queued"
    assert second_queued["status_message"] == "Queued for Agent capacity"
    assert agent_runner.child_dispatch_state() == {
        "queued": 1,
        "active": 1,
        "max_active": 1,
        "max_per_parent": 1,
    }
    release_first.set()
    assert agent_runner.wait_for_agent_run(first["id"], timeout=2)["status"] == "completed"
    assert agent_runner.wait_for_agent_run(second["id"], timeout=2)["status"] == "completed"
    assert order == [first["id"], second["id"]]


def test_default_dispatcher_runs_cumulative_children_in_bounded_waves(
    tmp_path,
    monkeypatch,
):
    agent_runner, _agent_runs, _profiles, _context, threads = _fresh_agent_runner_modules(
        tmp_path,
        monkeypatch,
    )
    parents = [
        threads.create_thread(f"Wave parent {index}")
        for index in range(3)
    ]
    release = threading.Event()
    started: list[tuple[str, str]] = []
    started_lock = threading.Lock()

    def fake_invoke(prompt, enabled_tool_names, config, *, stop_event):
        del prompt, enabled_tool_names, stop_event
        configurable = config["configurable"]
        with started_lock:
            started.append(
                (
                    str(configurable["agent_run_id"]),
                    str(configurable["parent_thread_id"]),
                )
            )
        assert release.wait(10)
        return "done"

    monkeypatch.setattr(agent_runner, "_invoke_agent", fake_invoke)
    runs = []
    for parent_index, count in enumerate((4, 3, 3)):
        for child_index in range(count):
            runs.append(
                agent_runner.spawn_agent_run(
                    f"Wave {parent_index}-{child_index}",
                    parent_thread_id=parents[parent_index],
                    profile="review",
                )
            )

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = agent_runner.child_dispatch_state()
        if current["active"] == 8 and current["active"] + current["queued"] == 10:
            break
        time.sleep(0.01)
    state = agent_runner.child_dispatch_state()
    parent_counts = {
        parent: sum(
            active_parent == parent
            for active_parent in agent_runner._DISPATCH_ACTIVE.values()
        )
        for parent in parents
    }
    assert state == {
        "queued": 2,
        "active": 8,
        "max_active": 8,
        "max_per_parent": 3,
    }, (started, agent_runner._DISPATCH_QUEUE, agent_runner._DISPATCH_ACTIVE)
    assert parent_counts == {
        parents[0]: 3,
        parents[1]: 3,
        parents[2]: 2,
    }

    release.set()
    finals = [
        agent_runner.wait_for_agent_run(run["id"], timeout=3)
        for run in runs
    ]
    assert all(run["status"] == "completed" for run in finals)
    assert len(started) == 10


def test_nested_depth_is_trusted_and_configurable_without_run_override(tmp_path, monkeypatch):
    agent_runner, agent_runs, _profiles, _context, threads = _fresh_agent_runner_modules(
        tmp_path, monkeypatch
    )
    from row_bot.agent_settings import AgentRuntimeSettings, save_agent_runtime_settings

    parent_thread_id = threads.create_thread("Nested parent")
    parent = agent_runs.create_agent_run(
        run_id="trusted-parent",
        status="running",
        thread_id=parent_thread_id,
        parent_thread_id=parent_thread_id,
        depth=1,
        root_run_id="trusted-parent",
    )
    monkeypatch.setattr(agent_runner, "_invoke_agent", lambda *args, **kwargs: "done")

    with pytest.raises(agent_runner.AgentRunnerError, match="configured maximum"):
        agent_runner.spawn_agent_run(
            "Nested child",
            parent_thread_id=parent_thread_id,
            parent_run_id=parent["id"],
        )

    save_agent_runtime_settings(AgentRuntimeSettings(max_spawn_depth=2))
    child = agent_runner.spawn_agent_run(
        "Nested child",
        parent_thread_id=parent_thread_id,
        parent_run_id=parent["id"],
        wait=True,
    )
    assert child["depth"] == 2
    assert child["root_run_id"] == parent["id"]
    assert child["settings_snapshot_json"]["max_spawn_depth"] == 2


def test_budget_terminal_child_is_blocked_not_completed(tmp_path, monkeypatch):
    agent_runner, _agent_runs, _profiles, _context, threads = _fresh_agent_runner_modules(
        tmp_path, monkeypatch
    )
    parent_thread_id = threads.create_thread("Budget parent")
    monkeypatch.setattr(
        agent_runner,
        "_invoke_agent",
        lambda *args, **kwargs: {
            "type": "terminal",
            "terminal_reason": "budget_exhausted",
            "message": "Partial work; continue later.",
        },
    )

    run = agent_runner.spawn_agent_run(
        "Long child task",
        parent_thread_id=parent_thread_id,
        wait=True,
    )
    assert run["status"] == "blocked"
    assert run["terminal_reason"] == "budget_exhausted"
    assert run["result_json"]["complete"] is False


def test_child_active_time_timeout_is_opt_in_and_terminal(tmp_path, monkeypatch):
    agent_runner, _agent_runs, _profiles, _context, threads = _fresh_agent_runner_modules(
        tmp_path, monkeypatch
    )
    from row_bot.agent_settings import AgentRuntimeSettings, save_agent_runtime_settings

    save_agent_runtime_settings(AgentRuntimeSettings(child_timeout_seconds=1))
    parent_thread_id = threads.create_thread("Timeout parent")

    def wait_until_stopped(prompt, enabled_tool_names, config, *, stop_event):
        del prompt, enabled_tool_names, config
        assert stop_event.wait(2)
        return "late result"

    monkeypatch.setattr(agent_runner, "_invoke_agent", wait_until_stopped)
    run = agent_runner.spawn_agent_run(
        "Wait for timeout",
        parent_thread_id=parent_thread_id,
        wait=True,
        timeout=3,
    )
    assert run["status"] == "timed_out"
    assert run["terminal_reason"] == "timeout"
    assert run["active_seconds"] >= 0.9


def test_default_child_has_no_timeout_even_after_large_fake_clock_jump(
    tmp_path,
    monkeypatch,
):
    agent_runner, _agent_runs, _profiles, _context, threads = _fresh_agent_runner_modules(
        tmp_path,
        monkeypatch,
    )
    parent_thread_id = threads.create_thread("No-timeout parent")
    timer_calls: list[float] = []

    class UnexpectedTimer:
        def __init__(self, seconds, callback):
            del callback
            timer_calls.append(seconds)

    monkeypatch.setattr(agent_runner.threading, "Timer", UnexpectedTimer)
    monkeypatch.setattr(agent_runner.time, "monotonic", lambda: 60 * 60 * 6)
    monkeypatch.setattr(agent_runner, "_invoke_agent", lambda *args, **kwargs: "done")

    run = agent_runner.spawn_agent_run(
        "Remain valid across a six-hour sleep/wake jump.",
        parent_thread_id=parent_thread_id,
        wait=True,
    )

    assert run["status"] == "completed"
    assert run["settings_snapshot_json"]["child_timeout_seconds"] == 0
    assert timer_calls == []


def test_spawn_agent_run_marks_provider_error_text_failed(tmp_path, monkeypatch):
    agent_runner, agent_runs, _profiles, _context, threads = _fresh_agent_runner_modules(
        tmp_path,
        monkeypatch,
    )
    parent_thread_id = threads.create_thread("Parent")

    def fake_invoke(prompt, enabled_tool_names, config, *, stop_event):
        return "!!! API error: provider rejected the request"

    monkeypatch.setattr(agent_runner, "_invoke_agent", fake_invoke)

    run = agent_runner.spawn_agent_run(
        "Use an unavailable model.",
        parent_thread_id=parent_thread_id,
        profile="worker",
        wait=True,
    )

    assert run["status"] == "failed"
    assert run["error"] == "!!! API error: provider rejected the request"
    assert run["summary"] == "!!! API error: provider rejected the request"
    event_types = {event["type"] for event in agent_runs.get_agent_events(run["id"])}
    assert "run.failed" in event_types
    assert "run.completed" not in event_types
    assert "turn.completed" not in event_types


def test_builtin_profile_skills_flow_to_child_agent(tmp_path, monkeypatch):
    agent_runner, agent_runs, _profiles, _context, threads = _fresh_agent_runner_modules(
        tmp_path,
        monkeypatch,
    )
    parent_thread_id = threads.create_thread("Parent")
    captured = {}

    def fake_invoke(prompt, enabled_tool_names, config, *, stop_event):
        captured["tools"] = enabled_tool_names
        captured["config"] = config
        return "research complete"

    monkeypatch.setattr(agent_runner, "_invoke_agent", fake_invoke)

    run = agent_runner.spawn_agent_run(
        "Research profile skills.",
        parent_thread_id=parent_thread_id,
        profile="research",
        enabled_tool_names=[
            "agents",
            "memory",
            "row_bot_status",
            "conversation_search",
            "duckduckgo",
            "web_search",
            "url_reader",
            "filesystem",
            "shell",
            "arxiv",
            "browser",
            "documents",
            "wiki",
            "wikipedia",
            "youtube",
        ],
        wait=True,
    )

    assert run["status"] == "completed"
    assert run["profile_snapshot_json"]["slug"] == "research"
    assert run["skills_override"] == ["deep_research", "web_navigator"]
    assert threads.get_thread_skills_override(run["thread_id"]) == [
        "deep_research",
        "web_navigator",
    ]
    assert "agents" not in captured["tools"]
    assert "shell" in captured["tools"]
    assert "browser" in captured["tools"]
    assert captured["config"]["configurable"]["tool_allowlist"] == run["tools_override"]
    event_types = {event["type"] for event in agent_runs.get_agent_events(run["id"])}
    assert {"run.created", "run.started", "turn.completed", "run.completed"} <= event_types


@_isolated_runtime_context
def test_child_skill_snapshot_starts_profile_skills_and_loads_others_task_locally(
    tmp_path,
    monkeypatch,
):
    _runner, _runs, _profiles, _context, _threads = _fresh_agent_runner_modules(
        tmp_path,
        monkeypatch,
    )
    import row_bot.agent as agent
    import row_bot.skill_discovery as discovery
    import row_bot.skills_activation as activation

    records = (
        discovery.SkillRecord(
            canonical_id="selected_manual",
            alias=None,
            display_name="Selected Manual",
            icon="*",
            description="Selected",
            tags=(),
            activation={},
            instructions="selected instructions",
            source="manual",
            root=Path(tmp_path),
        ),
        discovery.SkillRecord(
            canonical_id="plugin:demo:deferred",
            alias="deferred",
            display_name="Deferred Plugin",
            icon="*",
            description="Deferred",
            tags=("review",),
            activation={},
            instructions="deferred instructions",
            source="plugin:demo",
            root=Path(tmp_path),
            plugin_name="Demo",
        ),
    )
    monkeypatch.setattr(discovery, "collect_enabled_skill_records", lambda: list(records))
    profile = {
        "id": "profile:child",
        "enabled": True,
        "skill_policy_json": {"skills_override": ["selected_manual"]},
    }
    activation.load_auto_skill(
        "parent-thread",
        "plugin:demo:deferred",
        available_ids={record.canonical_id for record in records},
    )
    agent._set_active_runtime_context(
        thread_id="child-thread",
        runtime_surface="agent_child",
        agent_profile_snapshot=profile,
    )

    authorized, active_ids, discoverable, _fingerprint = agent._build_runtime_skill_snapshot()
    search, load = discovery.build_skill_discovery_tools(
        authorized,
        thread_id="child-thread",
        context_tokens=32_768,
        active_skill_ids=active_ids,
        child_boundaries=True,
    )

    assert [record.canonical_id for record in authorized] == [
        "selected_manual",
        "plugin:demo:deferred",
    ]
    assert active_ids == ("selected_manual",)
    assert discoverable is True
    search_payload = json.loads(search.invoke({"query": "deferred"}))
    assert search_payload["results"][0]["skill_id"] == "plugin:demo:deferred"
    assert "cannot grant tools" in search.description
    load_payload = json.loads(load.invoke({"name": "deferred"}))
    assert load_payload["newly_active"] is True
    assert activation.get_auto_loaded_skill_ids("child-thread") == ["plugin:demo:deferred"]
    assert activation.get_auto_loaded_skill_ids("sibling-thread") == []
    assert activation.get_auto_loaded_skill_ids("parent-thread") == ["plugin:demo:deferred"]


def test_concurrent_child_contexts_keep_profile_provider_and_cache_metadata_isolated():
    import row_bot.agent as agent

    first_key = frozenset({"child:first"})
    second_key = frozenset({"child:second"})
    agent.clear_agent_cache()
    agent._agent_cache_metadata.update({
        first_key: {
            "tool_parents": ("filesystem",),
            "bound_schema_tokens": 111,
            "skill_records": ("first-skill",),
        },
        second_key: {
            "tool_parents": ("mcp",),
            "bound_schema_tokens": 222,
            "skill_records": ("second-skill",),
        },
    })
    barrier = threading.Barrier(2)
    results: dict[str, dict] = {}
    errors: list[BaseException] = []

    def worker(label: str, cache_key: frozenset[str]) -> None:
        try:
            agent._set_active_runtime_context(
                thread_id=f"thread-{label}",
                runtime_surface="agent_child",
                model_override=f"model:{label}",
                agent_profile_id=f"profile:{label}",
                agent_profile_snapshot={"id": f"profile:{label}"},
            )
            agent._activate_agent_cache_metadata(cache_key)
            barrier.wait(timeout=5)
            results[label] = {
                "runtime": agent.get_active_runtime_context(),
                "parents": agent._current_effective_tool_parent_names_var.get(),
                "schema_tokens": agent._current_bound_tool_schema_tokens_var.get(),
                "skills": agent._current_authorized_skill_records_var.get(),
            }
        except BaseException as exc:  # surface thread failures in the main assertion
            errors.append(exc)

    first = threading.Thread(target=worker, args=("first", first_key))
    second = threading.Thread(target=worker, args=("second", second_key))
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not errors
    assert not first.is_alive() and not second.is_alive()
    assert results["first"]["runtime"]["agent_profile_id"] == "profile:first"
    assert results["first"]["runtime"]["model_override"] == "model:first"
    assert results["first"]["parents"] == ("filesystem",)
    assert results["first"]["schema_tokens"] == 111
    assert results["first"]["skills"] == ("first-skill",)
    assert results["second"]["runtime"]["agent_profile_id"] == "profile:second"
    assert results["second"]["runtime"]["model_override"] == "model:second"
    assert results["second"]["parents"] == ("mcp",)
    assert results["second"]["schema_tokens"] == 222
    assert results["second"]["skills"] == ("second-skill",)
    agent.clear_agent_cache()


def test_profile_tool_and_skill_policy_filters_child_context(tmp_path, monkeypatch):
    agent_runner, _agent_runs, profiles, _context, threads = _fresh_agent_runner_modules(
        tmp_path,
        monkeypatch,
    )
    parent_thread_id = threads.create_thread("Parent")
    threads.set_thread_skills_override(parent_thread_id, ["release_notes"])
    custom = profiles.save_agent_profile(
        slug="api_doc_worker",
        display_name="API Doc Worker",
        instructions="Write API docs.",
        tool_policy_json={
            "capability": "write_capable",
            "allow_tools": ["filesystem", "row_bot_status"],
        },
        skill_policy_json={
            "skills_override": ["release_notes", "openapi"],
        },
        context_policy_json={"default_context_mode": "focused"},
        workspace_policy_json={"workspace_mode_default": "single_writer"},
    )
    captured = {}

    def fake_invoke(prompt, enabled_tool_names, config, *, stop_event):
        captured["tools"] = enabled_tool_names
        captured["config"] = config
        return "done"

    monkeypatch.setattr(agent_runner, "_invoke_agent", fake_invoke)

    run = agent_runner.spawn_agent_run(
        "Write docs.",
        parent_thread_id=parent_thread_id,
        profile=custom["id"],
        enabled_tool_names=["shell", "filesystem", "row_bot_status", "gmail", "agents"],
        wait=True,
    )

    assert captured["tools"] == ["filesystem", "row_bot_status"]
    assert captured["config"]["configurable"]["tool_allowlist"] == ["filesystem", "row_bot_status"]
    assert run["tools_override"] == ["filesystem", "row_bot_status"]
    assert run["skills_override"] == ["release_notes", "openapi"]
    assert threads.get_thread_skills_override(run["thread_id"]) == ["release_notes", "openapi"]


def test_profile_external_tool_allowlist_passes_through_child_config(tmp_path, monkeypatch):
    agent_runner, _agent_runs, profiles, _context, threads = _fresh_agent_runner_modules(
        tmp_path,
        monkeypatch,
    )
    parent_thread_id = threads.create_thread("Parent")
    custom = profiles.save_agent_profile(
        slug="external_worker",
        display_name="External Worker",
        instructions="Use selected external tools.",
        tool_policy_json={
            "capability": "read_only",
            "allow_tools": ["filesystem", "plugin_lookup", "mcp_local_echo"],
        },
        context_policy_json={"default_context_mode": "focused"},
    )
    captured = {}

    def fake_invoke(prompt, enabled_tool_names, config, *, stop_event):
        captured["tools"] = enabled_tool_names
        captured["config"] = config
        return "done"

    monkeypatch.setattr(agent_runner, "_invoke_agent", fake_invoke)

    run = agent_runner.spawn_agent_run(
        "Use external tools.",
        parent_thread_id=parent_thread_id,
        profile=custom["id"],
        enabled_tool_names=["filesystem", "mcp", "row_bot_status", "agents"],
        wait=True,
    )

    assert captured["tools"] == ["filesystem", "mcp"]
    assert captured["config"]["configurable"]["tool_allowlist"] == [
        "filesystem",
        "plugin_lookup",
        "mcp_local_echo",
    ]
    assert run["tools_override"] == ["filesystem", "plugin_lookup", "mcp_local_echo"]


def test_profile_without_allowlist_preserves_default_child_tools(tmp_path, monkeypatch):
    agent_runner, _agent_runs, _profiles, _context, threads = _fresh_agent_runner_modules(
        tmp_path,
        monkeypatch,
    )
    parent_thread_id = threads.create_thread("Parent")
    captured = {}

    def fake_invoke(prompt, enabled_tool_names, config, *, stop_event):
        captured["tools"] = enabled_tool_names
        captured["config"] = config
        return "done"

    monkeypatch.setattr(agent_runner, "_invoke_agent", fake_invoke)

    run = agent_runner.spawn_agent_run(
        "Implement this.",
        parent_thread_id=parent_thread_id,
        profile="worker",
        enabled_tool_names=["filesystem", "mcp", "agents"],
        wait=True,
    )

    assert captured["tools"] == ["filesystem", "mcp"]
    assert "tool_allowlist" not in captured["config"]["configurable"]
    assert run["tools_override"] == ["filesystem", "mcp"]


def test_builtin_read_only_profile_does_not_inherit_write_heavy_tools(tmp_path, monkeypatch):
    agent_runner, _agent_runs, _profiles, _context, threads = _fresh_agent_runner_modules(
        tmp_path,
        monkeypatch,
    )
    parent_thread_id = threads.create_thread("Parent")
    captured = {}

    def fake_invoke(prompt, enabled_tool_names, config, *, stop_event):
        captured["tools"] = enabled_tool_names
        return "done"

    monkeypatch.setattr(agent_runner, "_invoke_agent", fake_invoke)

    run = agent_runner.spawn_agent_run(
        "Review this.",
        parent_thread_id=parent_thread_id,
        profile="review",
        enabled_tool_names=["filesystem", "row_bot_status", "gmail", "image_gen", "agents"],
        wait=True,
    )

    assert run["skills_override"] == []
    assert captured["tools"] == ["filesystem", "row_bot_status"]


def test_spawn_agent_run_records_interrupt_resume_state(tmp_path, monkeypatch):
    agent_runner, agent_runs, _profiles, _context, threads = _fresh_agent_runner_modules(
        tmp_path,
        monkeypatch,
    )
    parent_thread_id = threads.create_thread("Parent", approval_mode="approve")

    def fake_interrupt(prompt, enabled_tool_names, config, *, stop_event):
        return {
            "type": "interrupt",
            "interrupts": [{"tool": "shell", "description": "Needs approval"}],
        }

    monkeypatch.setattr(agent_runner, "_invoke_agent", fake_interrupt)

    run = agent_runner.spawn_agent_run(
        "Inspect the release.",
        parent_thread_id=parent_thread_id,
        profile="worker",
        enabled_tool_names=["shell"],
        wait=True,
    )

    assert run["status"] == "waiting_approval"
    assert run["status_message"] == "Waiting for approval"
    assert run["resume_state_json"]["enabled_tool_names"] == ["shell"]
    assert run["resume_state_json"]["interrupts"][0]["tool"] == "shell"
    event_types = {event["type"] for event in agent_runs.get_agent_events(run["id"])}
    assert "approval.requested" in event_types


def test_approval_resume_state_preserves_profile_tool_allowlist(tmp_path, monkeypatch):
    agent_runner, _agent_runs, profiles, _context, threads = _fresh_agent_runner_modules(
        tmp_path,
        monkeypatch,
    )
    parent_thread_id = threads.create_thread("Parent", approval_mode="approve")
    custom = profiles.save_agent_profile(
        slug="approval_worker",
        display_name="Approval Worker",
        instructions="Request approval.",
        tool_policy_json={
            "capability": "write_capable",
            "allow_tools": ["shell", "plugin_lookup"],
        },
    )

    def fake_interrupt(prompt, enabled_tool_names, config, *, stop_event):
        return {
            "type": "interrupt",
            "interrupts": [{"tool": "shell", "description": "Needs approval"}],
        }

    monkeypatch.setattr(agent_runner, "_invoke_agent", fake_interrupt)

    run = agent_runner.spawn_agent_run(
        "Run a command.",
        parent_thread_id=parent_thread_id,
        profile=custom["id"],
        enabled_tool_names=["shell", "filesystem"],
        wait=True,
    )

    assert run["status"] == "waiting_approval"
    assert run["resume_state_json"]["enabled_tool_names"] == ["shell"]
    assert run["resume_state_json"]["tool_allowlist"] == ["shell", "plugin_lookup"]
    assert run["resume_state_json"]["config"]["configurable"]["tool_allowlist"] == [
        "shell",
        "plugin_lookup",
    ]


def test_stop_agent_run_sets_live_stop_event_and_durable_status(tmp_path, monkeypatch):
    agent_runner, _agent_runs, _profiles, _context, threads = _fresh_agent_runner_modules(
        tmp_path,
        monkeypatch,
    )
    parent_thread_id = threads.create_thread("Parent")
    started = threading.Event()
    release = threading.Event()

    class TaskStoppedError(Exception):
        pass

    def fake_slow(prompt, enabled_tool_names, config, *, stop_event):
        started.set()
        assert stop_event.wait(3)
        assert release.wait(3)
        raise TaskStoppedError("stopped")

    monkeypatch.setattr(agent_runner, "_invoke_agent", fake_slow)

    run = agent_runner.spawn_agent_run(
        "Wait until stopped.",
        parent_thread_id=parent_thread_id,
        profile="worker",
        enabled_tool_names=[],
        wait=False,
    )

    assert started.wait(timeout=1.0)
    stopped = agent_runner.stop_agent_run(run["id"])
    try:
        assert stopped["status"] == "stopping"
        assert agent_runner.list_active_agent_run_ids() == [run["id"]]
    finally:
        release.set()
    final = agent_runner.wait_for_agent_run(run["id"], timeout=2.0)

    assert stopped["stop_requested"] is True
    assert final["status"] == "stopped"
    assert agent_runner.list_active_agent_run_ids() == []


def test_stop_agent_run_preserves_stop_when_invocation_returns_late_success(tmp_path, monkeypatch):
    agent_runner, _agent_runs, _profiles, _context, threads = _fresh_agent_runner_modules(
        tmp_path,
        monkeypatch,
    )
    parent_thread_id = threads.create_thread("Parent")
    started = threading.Event()
    release = threading.Event()

    def fake_slow_success(prompt, enabled_tool_names, config, *, stop_event):
        started.set()
        assert stop_event.wait(3)
        assert release.wait(3)
        return "late success"

    monkeypatch.setattr(agent_runner, "_invoke_agent", fake_slow_success)

    run = agent_runner.spawn_agent_run(
        "Wait until stopped.",
        parent_thread_id=parent_thread_id,
        profile="worker",
        enabled_tool_names=[],
        wait=False,
    )

    assert started.wait(timeout=1.0)
    stopped = agent_runner.stop_agent_run(run["id"])
    try:
        assert stopped["status"] == "stopping"
        assert agent_runner.list_active_agent_run_ids() == [run["id"]]
    finally:
        release.set()
    final = agent_runner.wait_for_agent_run(run["id"], timeout=2.0)

    assert final["status"] == "stopped"
    assert final["summary"] == ""
    event_types = [event["type"] for event in _agent_runs.get_agent_events(run["id"])]
    assert "run.completed" not in event_types
    assert agent_runner.list_active_agent_run_ids() == []


def test_orchestration_guidance_is_applied_at_next_safe_boundary(
    tmp_path,
    monkeypatch,
):
    agent_runner, _agent_runs, _profiles, _context, threads = _fresh_agent_runner_modules(
        tmp_path,
        monkeypatch,
    )
    from row_bot.agent_orchestrator import (
        create_or_get_orchestration,
        list_messages,
        message_orchestration,
    )

    parent_thread_id = threads.create_thread(
        "Guidance parent",
        model_override="provider:parent-model",
    )
    orchestration = create_or_get_orchestration(
        parent_thread_id=parent_thread_id,
        parent_generation_id="guidance-generation",
        root_objective="Review the implementation.",
        model_ref="provider:parent-model",
        approval_mode="block",
        runtime_surface="normal_chat",
    )
    started = threading.Event()
    release = threading.Event()
    prompts: list[str] = []

    def fake_invoke(prompt, enabled_tool_names, config, *, stop_event):
        del enabled_tool_names, config, stop_event
        prompts.append(prompt)
        if len(prompts) == 1:
            started.set()
            assert release.wait(2)
            return "Initial result"
        return "Updated result"

    monkeypatch.setattr(agent_runner, "_invoke_agent", fake_invoke)
    run = agent_runner.spawn_agent_run(
        "Review the implementation.",
        parent_thread_id=parent_thread_id,
        orchestration_id=orchestration["id"],
    )
    assert started.wait(1)
    assert message_orchestration(
        orchestration["id"],
        "Also verify the rollback path.",
        run_id=run["id"],
    ) == 1
    pending = list_messages(orchestration["id"], kinds=["steering"])
    assert pending[0]["delivery_status"] == "pending"
    release.set()
    final = agent_runner.wait_for_agent_run(run["id"], timeout=2)

    assert final["status"] == "completed"
    assert final["summary"] == "Updated result"
    assert "Also verify the rollback path." in prompts[1]
    delivered = list_messages(orchestration["id"], kinds=["steering"])
    assert delivered[0]["delivery_status"] == "delivered"


def test_worktree_workspace_mode_allocates_child_workspace(tmp_path, monkeypatch):
    agent_runner, agent_runs, profiles, _context, threads = _fresh_agent_runner_modules(
        tmp_path,
        monkeypatch,
    )
    _workspace(tmp_path, "dev_parent", "parent")
    parent_thread_id = threads.create_thread(
        "Parent",
        developer_workspace_id="dev_parent",
    )
    custom = profiles.save_agent_profile(
        slug="parallel_coder",
        display_name="Parallel Coder",
        instructions="Edit in isolation.",
        tool_policy_json={"capability": "write_capable"},
        workspace_policy_json={"workspace_mode_default": "worktree"},
    )
    calls = []

    def fake_allocate(
        run_id,
        parent_workspace_id,
        *,
        objective="",
        branch_slug="",
        seed_mode="current_changes",
        parent_thread_id="",
    ):
        calls.append((run_id, parent_workspace_id, objective, branch_slug, seed_mode, parent_thread_id))
        _workspace(tmp_path, "dev_worktree", "repo-wt")
        return {
            "status": "active",
            "owner_kind": "agent_run",
            "owner_id": run_id,
            "project_workspace_id": parent_workspace_id,
            "worktree_workspace_id": "dev_worktree",
            "worktree_path": str(tmp_path / "repo-wt"),
            "branch_name": f"row-bot/{run_id}-parallel-coder",
            "metadata_json": {"seeded_from_current_changes": True},
        }

    import row_bot.developer.worktrees as worktrees

    monkeypatch.setattr(worktrees, "allocate_agent_worktree", fake_allocate)
    captured = {}

    def fake_invoke(prompt, enabled_tool_names, config, *, stop_event):
        captured["config"] = config
        return "done"

    monkeypatch.setattr(agent_runner, "_invoke_agent", fake_invoke)

    run = agent_runner.spawn_agent_run(
        "Fix tests.",
        parent_thread_id=parent_thread_id,
        profile=custom["id"],
        enabled_tool_names=["filesystem"],
        wait=True,
    )

    assert calls == [(run["id"], "dev_parent", "Fix tests.", "", "current_changes", parent_thread_id)]
    assert run["status"] == "completed"
    assert run["workspace_id"] == "dev_worktree"
    assert run["workspace_path"] == str(tmp_path / "repo-wt")
    assert run["workspace_mode"] == "worktree"
    assert run["write_lock_key"] == "developer:dev_worktree"
    assert threads._get_thread_developer_workspace(run["thread_id"]) == "dev_worktree"
    assert captured["config"]["configurable"]["developer_workspace_id"] == "dev_worktree"
    events = agent_runs.get_agent_events(run["id"])
    worktree_event = next(event for event in events if event["type"] == "workspace.worktree_allocated")
    assert worktree_event["payload_json"]["branch_name"].endswith("parallel-coder")
    assert worktree_event["payload_json"]["seeded_from_current_changes"] is True


def test_two_worktree_child_agents_receive_distinct_workspaces(tmp_path, monkeypatch):
    agent_runner, _agent_runs, profiles, _context, threads = _fresh_agent_runner_modules(
        tmp_path,
        monkeypatch,
    )
    _workspace(tmp_path, "dev_parent", "parent")
    parent_thread_id = threads.create_thread(
        "Parent",
        developer_workspace_id="dev_parent",
    )
    custom = profiles.save_agent_profile(
        slug="parallel_coder",
        display_name="Parallel Coder",
        instructions="Edit in isolation.",
        workspace_policy_json={"workspace_mode_default": "worktree"},
    )

    def fake_allocate(
        run_id,
        parent_workspace_id,
        *,
        objective="",
        branch_slug="",
        seed_mode="current_changes",
        parent_thread_id="",
    ):
        _workspace(tmp_path, f"dev_worktree_{run_id}", f"repo-wt-{run_id}")
        return {
            "status": "active",
            "owner_kind": "agent_run",
            "owner_id": run_id,
            "project_workspace_id": parent_workspace_id,
            "worktree_workspace_id": f"dev_worktree_{run_id}",
            "worktree_path": str(tmp_path / f"repo-wt-{run_id}"),
            "branch_name": f"row-bot/{run_id}-parallel-coder",
            "metadata_json": {"seeded_from_current_changes": False},
        }

    import row_bot.developer.worktrees as worktrees

    monkeypatch.setattr(worktrees, "allocate_agent_worktree", fake_allocate)
    monkeypatch.setattr(agent_runner, "_invoke_agent", lambda *a, **k: "done")

    first = agent_runner.spawn_agent_run(
        "Fix tests.",
        parent_thread_id=parent_thread_id,
        profile=custom["id"],
        wait=True,
    )
    second = agent_runner.spawn_agent_run(
        "Update docs.",
        parent_thread_id=parent_thread_id,
        profile=custom["id"],
        wait=True,
    )

    assert first["workspace_mode"] == "worktree"
    assert second["workspace_mode"] == "worktree"
    assert first["workspace_id"] != second["workspace_id"]
    assert first["workspace_path"] != second["workspace_path"]


def test_orchestrated_write_children_are_forced_into_distinct_worktrees(
    tmp_path,
    monkeypatch,
):
    agent_runner, _agent_runs, profiles, _context, threads = _fresh_agent_runner_modules(
        tmp_path,
        monkeypatch,
    )
    from row_bot.agent_orchestrator import create_or_get_orchestration
    _workspace(tmp_path, "dev_parent", "parent")

    parent_thread_id = threads.create_thread(
        "Parent",
        developer_workspace_id="dev_parent",
        model_override="provider:parent-model",
    )
    writer = profiles.save_agent_profile(
        slug="automatic_writer",
        display_name="Automatic Writer",
        instructions="Edit the assigned files.",
        tool_policy_json={"capability": "write_capable"},
        workspace_policy_json={"workspace_mode_default": "auto"},
    )
    orchestration = create_or_get_orchestration(
        parent_thread_id=parent_thread_id,
        parent_generation_id="generation",
        root_objective="Implement two independent changes.",
        model_ref="provider:parent-model",
        approval_mode="block",
        runtime_surface="normal_chat",
    )

    def fake_allocate(run_id, parent_workspace_id, **_kwargs):
        _workspace(tmp_path, f"workspace-{run_id}", f"worktree-{run_id}")
        return {
            "status": "active",
            "project_workspace_id": parent_workspace_id,
            "worktree_workspace_id": f"workspace-{run_id}",
            "worktree_path": str(tmp_path / f"worktree-{run_id}"),
            "branch_name": f"row-bot/{run_id}",
            "metadata_json": {},
        }

    import row_bot.developer.worktrees as worktrees

    monkeypatch.setattr(worktrees, "allocate_agent_worktree", fake_allocate)
    monkeypatch.setattr(
        agent_runner,
        "_developer_workspace_supports_auto_worktree",
        lambda workspace_id: workspace_id == "dev_parent",
    )
    monkeypatch.setattr(agent_runner, "_invoke_agent", lambda *args, **kwargs: "done")

    first = agent_runner.spawn_agent_run(
        "Edit module A.",
        parent_thread_id=parent_thread_id,
        profile=writer["id"],
        orchestration_id=orchestration["id"],
        wait=True,
    )
    second = agent_runner.spawn_agent_run(
        "Edit module B.",
        parent_thread_id=parent_thread_id,
        profile=writer["id"],
        orchestration_id=orchestration["id"],
        wait=True,
    )

    assert first["model_override"] == "provider:parent-model"
    assert second["model_override"] == "provider:parent-model"
    assert first["workspace_mode"] == second["workspace_mode"] == "worktree"
    assert first["workspace_id"] != second["workspace_id"]
    assert first["write_lock_key"] != second["write_lock_key"]


def test_orchestrated_writer_keeps_single_writer_mode_for_non_git_workspace(
    tmp_path,
    monkeypatch,
):
    agent_runner, _agent_runs, profiles, _context, threads = _fresh_agent_runner_modules(
        tmp_path,
        monkeypatch,
    )
    from row_bot.agent_orchestrator import create_or_get_orchestration
    _workspace(tmp_path, "dev_folder", "folder")

    parent_thread_id = threads.create_thread(
        "Non-Git parent",
        developer_workspace_id="dev_folder",
        model_override="provider:parent-model",
    )
    writer = profiles.save_agent_profile(
        slug="folder_writer",
        display_name="Folder Writer",
        instructions="Edit the selected folder.",
        tool_policy_json={"capability": "write_capable"},
        workspace_policy_json={"workspace_mode_default": "single_writer"},
    )
    orchestration = create_or_get_orchestration(
        parent_thread_id=parent_thread_id,
        parent_generation_id="generation",
        root_objective="Implement a small local tool.",
        model_ref="provider:parent-model",
        approval_mode="block",
        runtime_surface="developer",
    )

    monkeypatch.setattr(
        agent_runner,
        "_developer_workspace_supports_auto_worktree",
        lambda _workspace_id: False,
    )
    monkeypatch.setattr(agent_runner, "_invoke_agent", lambda *args, **kwargs: "done")

    run = agent_runner.spawn_agent_run(
        "Inspect and edit the folder.",
        parent_thread_id=parent_thread_id,
        profile=writer["id"],
        orchestration_id=orchestration["id"],
        wait=True,
    )

    assert run["status"] == "completed"
    assert run["workspace_mode"] == "single_writer"
    assert run["workspace_id"] == "dev_folder"
    assert run["workspace_path"] == ""
    assert run["write_lock_key"] == "developer:dev_folder"
    assert threads._get_thread_developer_workspace(run["thread_id"]) == "dev_folder"


def test_worktree_requires_developer_workspace(tmp_path, monkeypatch):
    agent_runner, _agent_runs, _profiles, _context, threads = _fresh_agent_runner_modules(
        tmp_path,
        monkeypatch,
    )
    parent_thread_id = threads.create_thread("Parent")

    try:
        agent_runner.spawn_agent_run(
            "Fix tests.",
            parent_thread_id=parent_thread_id,
            profile="worker",
            use_worktree=True,
        )
    except agent_runner.AgentRunnerError as exc:
        assert "Choose a repo" in str(exc)
    else:
        raise AssertionError("Expected AgentRunnerError")
