from __future__ import annotations

import importlib
import json
import sys

import pytest


def _fresh_agent_tool_modules(tmp_path, monkeypatch):
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
        "row_bot.tools.agent_tool",
    ):
        sys.modules.pop(name, None)

    import row_bot.tasks as tasks
    import row_bot.agent_runs as agent_runs
    import row_bot.tools.agent_tool as agent_tool

    tasks = importlib.reload(tasks)
    agent_runs = importlib.reload(agent_runs)
    agent_tool = importlib.reload(agent_tool)
    return agent_tool, agent_runs


def _isolated_model_choices(tmp_path, monkeypatch):
    import row_bot.providers.config as provider_config
    import row_bot.providers.selection as selection

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(selection, "load_provider_config", provider_config.load_provider_config)
    monkeypatch.setattr(selection, "save_provider_config", provider_config.save_provider_config)
    provider_config.save_provider_config({})
    selection._provider_status_picker_cache.clear()
    return selection


def _chat_snapshot() -> dict:
    return {
        "tasks": ["chat"],
        "input_modalities": ["text"],
        "output_modalities": ["text"],
    }


def test_agents_tool_registers_expected_subtools(tmp_path, monkeypatch):
    agent_tool, _agent_runs = _fresh_agent_tool_modules(tmp_path, monkeypatch)
    from row_bot.tools import registry

    registered = registry.get_tool("agents")
    assert registered is not None
    names = {tool.name for tool in registered.as_langchain_tools()}

    assert {
        "delegate_work",
        "agent_status",
        "agent_wait",
        "agent_stop",
        "agent_profiles",
        "agent_profile_save",
        "agent_message",
        "agent_promote",
    } <= names
    assert registered.destructive_tool_names == {"agent_profile_save", "agent_promote"}
    assert registered.enabled_by_default is True


def test_delegate_work_uses_runner_and_returns_public_run(tmp_path, monkeypatch):
    agent_tool, _agent_runs = _fresh_agent_tool_modules(tmp_path, monkeypatch)
    calls = {}

    def fake_spawn(objective, **kwargs):
        calls["objective"] = objective
        calls["kwargs"] = kwargs
        return {
            "id": "run-1",
            "kind": "subagent",
            "status": "queued",
            "display_name": "Review",
            "thread_id": "child-thread",
            "parent_thread_id": kwargs["parent_thread_id"],
            "profile_id": "builtin:review",
            "profile_slug": "review",
            "profile_display_name": "Review",
            "workspace_id": kwargs.get("developer_workspace_id", ""),
            "workspace_path": "D:/tmp/repo-wt" if kwargs.get("use_worktree") else "",
            "workspace_mode": "worktree" if kwargs.get("use_worktree") else "auto",
        }

    monkeypatch.setattr(agent_tool.agent_runner, "spawn_agent_run", fake_spawn)

    payload = json.loads(agent_tool._delegate_work(
        objective="Review the diff.",
        profile="quality_reviewer",
        context="Changed files: app.py",
        parent_thread_id="parent-thread",
        developer_workspace_id="dev_parent",
        use_worktree=True,
        wait=False,
    ))

    assert payload["ok"] is True
    assert payload["run"]["id"] == "run-1"
    assert payload["run"]["profile"]["slug"] == "review"
    assert calls["objective"] == "Review the diff."
    assert calls["kwargs"]["profile"] == "quality_reviewer"
    assert calls["kwargs"]["context"] == "Changed files: app.py"
    assert calls["kwargs"]["parent_thread_id"] == "parent-thread"
    assert calls["kwargs"]["orchestration_id"]
    assert calls["kwargs"]["orchestration_required"] is True
    from row_bot.models import get_current_model

    assert calls["kwargs"]["model_override"] == get_current_model()
    assert calls["kwargs"]["developer_workspace_id"] == "dev_parent"
    assert calls["kwargs"]["use_worktree"] is True
    assert payload["run"]["workspace"]["mode"] == "worktree"
    assert payload["run"]["workspace"]["id"] == "dev_parent"
    assert "agent_wait(orchestration_id=" in payload["next_action"]
    assert "continue useful independent work" in payload["next_action"].lower()


def test_optional_background_child_is_still_a_durable_member(
    tmp_path,
    monkeypatch,
):
    agent_tool, _agent_runs = _fresh_agent_tool_modules(tmp_path, monkeypatch)
    calls = {}

    def fake_spawn(objective, **kwargs):
        calls["kwargs"] = kwargs
        return {
            "id": "optional-run",
            "kind": "subagent",
            "status": "queued",
            "display_name": "Background",
            "thread_id": "optional-thread",
            "parent_thread_id": kwargs["parent_thread_id"],
        }

    monkeypatch.setattr(agent_tool.agent_runner, "spawn_agent_run", fake_spawn)
    payload = json.loads(
        agent_tool._delegate_work(
            objective="Watch this in the background.",
            parent_thread_id="parent-thread",
            required=False,
            wait=False,
        )
    )

    assert payload["ok"] is True
    assert payload["orchestration"]["id"]
    assert payload["orchestration"]["required"] is False
    assert calls["kwargs"]["orchestration_id"] == payload["orchestration"]["id"]
    assert calls["kwargs"]["orchestration_required"] is False


def test_delegate_work_resolves_optional_model_to_canonical_ref(tmp_path, monkeypatch):
    selection = _isolated_model_choices(tmp_path, monkeypatch)
    selection.add_quick_choice_for_model(
        "claude-sonnet-4-5",
        provider_id="anthropic",
        display_name="Claude Work",
        capabilities_snapshot=_chat_snapshot(),
    )
    agent_tool, _agent_runs = _fresh_agent_tool_modules(tmp_path, monkeypatch)
    calls = {}

    def fake_spawn(objective, **kwargs):
        calls["objective"] = objective
        calls["kwargs"] = kwargs
        return {
            "id": "run-model",
            "kind": "subagent",
            "status": "queued",
            "display_name": "Research",
            "thread_id": "child-thread",
            "parent_thread_id": kwargs["parent_thread_id"],
            "profile_id": "builtin:research",
            "profile_slug": "research",
            "profile_display_name": "Research",
            "model_override": kwargs["model_override"],
        }

    monkeypatch.setattr(agent_tool.agent_runner, "spawn_agent_run", fake_spawn)

    payload = json.loads(agent_tool._delegate_work(
        objective="Research current docs.",
        profile="research",
        model="Claude Work",
        parent_thread_id="parent-thread",
    ))

    assert payload["ok"] is True
    assert calls["kwargs"]["model_override"] == "model:anthropic:claude-sonnet-4-5"
    assert payload["run"]["model_override"] == "model:anthropic:claude-sonnet-4-5"


def test_delegate_work_rejects_unknown_or_ambiguous_model_without_spawning(tmp_path, monkeypatch):
    selection = _isolated_model_choices(tmp_path, monkeypatch)
    selection.add_quick_choice_for_model(
        "lab-chat",
        provider_id="openai",
        display_name="Shared Model",
        capabilities_snapshot=_chat_snapshot(),
    )
    selection.add_quick_choice_for_model(
        "lab-chat",
        provider_id="anthropic",
        display_name="Shared Model",
        capabilities_snapshot=_chat_snapshot(),
    )
    agent_tool, _agent_runs = _fresh_agent_tool_modules(tmp_path, monkeypatch)
    calls = {"count": 0}

    def fake_spawn(objective, **kwargs):
        calls["count"] += 1
        return {}

    monkeypatch.setattr(agent_tool.agent_runner, "spawn_agent_run", fake_spawn)

    unknown = json.loads(agent_tool._delegate_work(
        objective="Try unknown.",
        model="not-a-pinned-model",
    ))
    ambiguous = json.loads(agent_tool._delegate_work(
        objective="Try ambiguous.",
        model="Shared Model",
    ))

    assert unknown["ok"] is False
    assert "not pinned for Brain" in unknown["message"]
    assert ambiguous["ok"] is False
    assert "Ambiguous model selection" in ambiguous["message"]
    assert calls["count"] == 0


def test_delegate_work_rejects_unpinned_canonical_model_without_spawning(tmp_path, monkeypatch):
    _isolated_model_choices(tmp_path, monkeypatch)
    agent_tool, _agent_runs = _fresh_agent_tool_modules(tmp_path, monkeypatch)
    calls = {"count": 0}

    def fake_spawn(objective, **kwargs):
        calls["count"] += 1
        return {}

    monkeypatch.setattr(agent_tool.agent_runner, "spawn_agent_run", fake_spawn)

    payload = json.loads(agent_tool._delegate_work(
        objective="Try unpinned.",
        model="model:openai:gpt-4o-mini",
    ))

    assert payload["ok"] is False
    assert "not pinned for Brain" in payload["message"]
    assert calls["count"] == 0


def test_agents_guide_mentions_pinned_model_resolution() -> None:
    from pathlib import Path

    guide = Path("tool_guides/agents_guide/SKILL.md").read_text(encoding="utf-8").lower()

    assert "pinned brain choices" in guide
    assert "row_bot_status category='model'" in guide
    assert "delegate_work(model=...)" in guide
    assert "delegate_work(wait=false)" in guide
    assert "parent thread stays responsive" in guide
    assert "use `wait=true` only when the user explicitly asks" in guide
    assert "do not delegate" in guide
    assert "trivial" in guide
    assert "smallest useful wave" in guide
    assert "inherits the parent model" in guide
    assert "required=true" in guide
    assert "required=false" in guide
    assert "complete material" in guide
    assert "real later wave" in guide
    assert "launch order only" in guide
    assert "does not transfer" in guide
    assert "natural concise update" in guide
    assert "agent_wait(orchestration_id=...)" in guide
    assert "before finalizing" in guide
    assert "later wave" in guide
    assert "do not loop on `agent_status`" in guide


def test_delegate_work_schema_is_async_first() -> None:
    from row_bot.tools.agent_tool import _DelegateWorkInput, AgentsTool

    wait_description = str(_DelegateWorkInput.model_fields["wait"].description or "").lower()
    assert "prefer false" in wait_description
    assert "asynchronously" in wait_description
    assert "explicitly asks" in wait_description
    worktree_description = str(
        _DelegateWorkInput.model_fields["use_worktree"].description or ""
    ).lower()
    assert "local git worktree" in worktree_description
    context_description = str(
        _DelegateWorkInput.model_fields["context"].description or ""
    ).lower()
    dependency_description = str(
        _DelegateWorkInput.model_fields["depends_on"].description or ""
    ).lower()
    assert "complete material" in context_description
    assert "artifact path" in context_description
    assert "launch order only" in dependency_description
    assert "never transfers" in dependency_description

    delegate_tool = next(
        tool
        for tool in AgentsTool().as_langchain_tools()
        if tool.name == "delegate_work"
    )
    assert "async background" in str(delegate_tool.description).lower()
    assert "continue useful independent work" in str(delegate_tool.description).lower()

    wait_tool = next(
        tool
        for tool in AgentsTool().as_langchain_tools()
        if tool.name == "agent_wait"
    )
    assert "required cohort" in str(wait_tool.description).lower()
    assert "before final" in str(wait_tool.description).lower()


def test_agent_wait_schema_accepts_exactly_one_target() -> None:
    from row_bot.tools.agent_tool import _AgentWaitInput

    assert _AgentWaitInput.model_validate({"run_id": "run-1"}).run_id == "run-1"
    assert (
        _AgentWaitInput.model_validate({"orchestration_id": "orch-1"}).orchestration_id
        == "orch-1"
    )
    with pytest.raises(ValueError, match="exactly one"):
        _AgentWaitInput.model_validate({})
    with pytest.raises(ValueError, match="exactly one"):
        _AgentWaitInput.model_validate({
            "run_id": "run-1",
            "orchestration_id": "orch-1",
        })


def test_agent_wait_group_rejects_another_parent_thread(tmp_path, monkeypatch):
    agent_tool, _agent_runs = _fresh_agent_tool_modules(tmp_path, monkeypatch)
    import row_bot.agent_orchestrator as orchestrator

    orchestration = orchestrator.create_or_get_orchestration(
        parent_thread_id="owned-parent",
        parent_generation_id="owned-generation",
        root_objective="Owned work",
        model_ref="provider:model",
        approval_mode="block",
        runtime_surface="normal_chat",
        orchestration_version=2,
    )
    monkeypatch.setattr(
        agent_tool,
        "_runtime_context",
        lambda: {"thread_id": "different-parent"},
    )

    payload = json.loads(
        agent_tool._agent_wait(orchestration_id=orchestration["id"])
    )

    assert payload["ok"] is False
    assert "another parent thread" in payload["message"]


def test_agent_wait_group_returns_ordered_terminal_results_and_events(
    tmp_path,
    monkeypatch,
):
    agent_tool, agent_runs = _fresh_agent_tool_modules(tmp_path, monkeypatch)
    import row_bot.agent_orchestrator as orchestrator

    orchestration = orchestrator.create_or_get_orchestration(
        parent_thread_id="parent-thread",
        parent_generation_id="group-generation",
        root_objective="Join the required group",
        model_ref="provider:model",
        approval_mode="block",
        runtime_surface="normal_chat",
        orchestration_version=2,
    )
    for run_id in ("first-run", "second-run"):
        agent_runs.create_agent_run(
            run_id=run_id,
            status="running",
            parent_thread_id="parent-thread",
            thread_id=f"thread-{run_id}",
            display_name=run_id,
        )
        orchestrator.register_member(orchestration["id"], run_id, required=True)
    agent_runs.finish_agent_run("second-run", "failed", error="Permanent failure")
    agent_runs.finish_agent_run("first-run", "completed", summary="First result")
    monkeypatch.setattr(
        agent_tool,
        "_runtime_context",
        lambda: {"thread_id": "parent-thread"},
    )

    payload = json.loads(
        agent_tool._agent_wait(
            orchestration_id=orchestration["id"],
            timeout_seconds=0,
        )
    )

    assert payload["ok"] is True
    assert [run["id"] for run in payload["runs"]] == ["first-run", "second-run"]
    assert [run["status"] for run in payload["runs"]] == ["completed", "failed"]
    assert payload["barrier_complete"] is True
    assert payload["timed_out"] is False
    assert payload["outstanding_run_ids"] == []
    assert len(payload["child_event_ids"]) == 2


def test_agent_wait_group_timeout_reports_outstanding_without_completion(
    tmp_path,
    monkeypatch,
):
    agent_tool, agent_runs = _fresh_agent_tool_modules(tmp_path, monkeypatch)
    import row_bot.agent_orchestrator as orchestrator

    orchestration = orchestrator.create_or_get_orchestration(
        parent_thread_id="parent-thread",
        parent_generation_id="timeout-generation",
        root_objective="Wait briefly",
        model_ref="provider:model",
        approval_mode="block",
        runtime_surface="normal_chat",
        orchestration_version=2,
    )
    agent_runs.create_agent_run(
        run_id="slow-run",
        status="running",
        parent_thread_id="parent-thread",
        thread_id="thread-slow-run",
    )
    orchestrator.register_member(orchestration["id"], "slow-run", required=True)
    monkeypatch.setattr(
        agent_tool,
        "_runtime_context",
        lambda: {"thread_id": "parent-thread"},
    )

    payload = json.loads(
        agent_tool._agent_wait(
            orchestration_id=orchestration["id"],
            timeout_seconds=0,
        )
    )

    assert payload["ok"] is True
    assert payload["barrier_complete"] is False
    assert payload["timed_out"] is True
    assert payload["outstanding_run_ids"] == ["slow-run"]
    assert payload["child_event_ids"] == []


def test_delegate_work_wait_timeout_message_is_explicit(tmp_path, monkeypatch):
    agent_tool, _agent_runs = _fresh_agent_tool_modules(tmp_path, monkeypatch)

    def fake_spawn(objective, **kwargs):
        return {
            "id": "run-timeout",
            "kind": "subagent",
            "status": "running",
            "display_name": "Slow Agent",
        }

    monkeypatch.setattr(agent_tool.agent_runner, "spawn_agent_run", fake_spawn)

    payload = json.loads(agent_tool._delegate_work(
        objective="Do slow work.",
        parent_thread_id="parent-thread",
        wait=True,
        timeout_seconds=0.01,
    ))

    assert payload["message"] == "Child Agent is still running after the wait timeout."


def test_agent_status_profiles_and_profile_save(tmp_path, monkeypatch):
    agent_tool, agent_runs = _fresh_agent_tool_modules(tmp_path, monkeypatch)

    run = agent_runs.create_agent_run(
        run_id="status-run",
        kind="subagent",
        status="completed",
        parent_thread_id="parent-thread",
        thread_id="child-thread",
        display_name="Status Run",
        profile_id="quality_reviewer",
        summary="Looks good.",
    )
    agent_runs.append_agent_event(run["id"], "summary.updated", {"summary": "Looks good."})
    agent_runs.append_agent_parent_message(run["id"], "Prefer concise evidence.")

    status_payload = json.loads(agent_tool._agent_status(
        run_id="status-run",
        include_events=True,
    ))
    assert status_payload["ok"] is True
    assert status_payload["run"]["status"] == "completed"
    assert status_payload["run"]["parent_message_count"] == 1
    assert status_payload["run"]["latest_parent_message"] == "Prefer concise evidence."
    assert any(event["type"] == "summary.updated" for event in status_payload["events"])

    list_payload = json.loads(agent_tool._agent_status(parent_thread_id="parent-thread"))
    assert [item["id"] for item in list_payload["runs"]] == ["status-run"]

    profiles_payload = json.loads(agent_tool._agent_profiles(query="review"))
    assert any(profile["slug"] == "review" for profile in profiles_payload["profiles"])
    assert all(profile["slug"] != "quality_reviewer" for profile in profiles_payload["profiles"])
    quality_profile = next(
        profile for profile in profiles_payload["profiles"]
        if profile["slug"] == "review"
    )
    assert quality_profile["tool_mode"] == "selected_tools"

    saved_payload = json.loads(agent_tool._agent_profile_save(
        slug="release_reviewer",
        display_name="Release Reviewer",
        description="Review releases.",
        when_to_use="Before shipping.",
        instructions="Review release risk.",
        allow_tools=["filesystem"],
        skills=["release_notes"],
    ))
    assert saved_payload["ok"] is True
    assert saved_payload["profile"]["slug"] == "release_reviewer"
    assert saved_payload["profile"]["allow_tools"] == ["filesystem"]
    assert saved_payload["profile"]["skills"] == ["release_notes"]


def test_agent_promote_creates_profile_and_disabled_workflow(tmp_path, monkeypatch):
    agent_tool, agent_runs = _fresh_agent_tool_modules(tmp_path, monkeypatch)
    import row_bot.tasks as tasks

    run = agent_runs.create_agent_run(
        run_id="promote-run",
        kind="subagent",
        status="completed",
        display_name="Release Check",
        prompt="Review the release checklist.",
        context_summary="Changed files: release.py",
        profile_id="quality_reviewer",
        model_override="",
        tools_override=["filesystem"],
        skills_override=["release_notes"],
        approval_mode="approve",
        summary="Release checklist passed.",
    )

    profile_payload = json.loads(agent_tool._agent_promote(run["id"], target="profile"))
    assert profile_payload["ok"] is True
    assert profile_payload["profile"]["slug"] == "promoted_promote_run"

    workflow_payload = json.loads(agent_tool._agent_promote(run["id"], target="workflow"))
    assert workflow_payload["ok"] is True
    workflow = workflow_payload["workflow"]
    task = tasks.get_task(workflow["id"])

    assert workflow["enabled"] is False
    assert task["enabled"] is False
    assert task["advanced_mode"] is True
    assert task["agent_profile_id"] == "builtin:review"
    assert task["tools_override"] is None
    assert task["skills_override"] is None
    assert task["safety_mode"] == "approve"
    assert "Review the release checklist." in task["steps"][0]["prompt"]
    assert "Release checklist passed." in task["steps"][0]["prompt"]


def test_agent_message_records_parent_steering_for_nonterminal_run(tmp_path, monkeypatch):
    agent_tool, agent_runs = _fresh_agent_tool_modules(tmp_path, monkeypatch)
    queued = agent_runs.create_agent_run(
        run_id="queued-message",
        kind="subagent",
        status="queued",
        display_name="Queued Message",
    )

    payload = json.loads(agent_tool._agent_message(
        queued["id"],
        "Prefer the smaller refactor.",
    ))

    assert payload["ok"] is True
    assert payload["run"]["status_message"] == "Parent message queued"
    events = agent_runs.get_agent_events(queued["id"])
    assert events[-2]["type"] == "parent.message"
    assert events[-2]["payload_json"]["message"] == "Prefer the smaller refactor."

    agent_runs.finish_agent_run(queued["id"], "completed", summary="Done")
    terminal = json.loads(agent_tool._agent_message(queued["id"], "Too late"))
    assert terminal["ok"] is False
    assert "cannot be steered" in terminal["message"]


def test_agents_guide_is_parent_tool_guide():
    text = open("tool_guides/agents_guide/SKILL.md", encoding="utf-8").read()

    assert "name: agents_guide" in text
    assert "tools:\n  - agents" in text
    assert "delegate_work" in text
    assert "agent_profile_save" in text
    assert "agent_message" in text
    assert "workflow" in text
