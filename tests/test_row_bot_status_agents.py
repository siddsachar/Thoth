from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _fresh_status_modules(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(data_dir))
    for name in (
        "row_bot.tasks",
        "row_bot.threads",
        "row_bot.agent_profiles",
        "row_bot.agent_runs",
        "row_bot.goals",
        "row_bot.agent",
        "row_bot.tools.row_bot_status_tool",
    ):
        sys.modules.pop(name, None)

    import row_bot.tasks as tasks
    import row_bot.threads as threads
    import row_bot.agent_profiles as agent_profiles
    import row_bot.agent_runs as agent_runs
    import row_bot.goals as goals
    import row_bot.agent as agent
    import row_bot.tools.row_bot_status_tool as row_bot_status_tool

    tasks = importlib.reload(tasks)
    threads = importlib.reload(threads)
    agent_profiles = importlib.reload(agent_profiles)
    agent_runs = importlib.reload(agent_runs)
    goals = importlib.reload(goals)
    agent = importlib.reload(agent)
    row_bot_status_tool = importlib.reload(row_bot_status_tool)
    return threads, agent_profiles, agent_runs, goals, agent, row_bot_status_tool


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


def test_row_bot_status_reports_agents_profiles_and_goals(tmp_path, monkeypatch):
    threads, profiles, agent_runs, goals, agent, status_tool = _fresh_status_modules(
        tmp_path,
        monkeypatch,
    )
    thread_id = threads.create_thread("Status thread")
    threads._set_thread_agent_profile(thread_id, "quality_reviewer")
    profiles.save_agent_profile(
        slug="disabled_release_reviewer",
        display_name="Disabled Release Reviewer",
        description="Review releases when enabled.",
        instructions="Review release risks.",
        tool_policy_json={"capability": "read_only"},
        context_policy_json={"default_context_mode": "focused"},
        workspace_policy_json={"workspace_mode_default": "read_only"},
        enabled=False,
    )
    agent_runs.create_agent_run(
        run_id="status-run-1",
        kind="subagent",
        status="running",
        status_message="Working through a delegated task",
        parent_thread_id=thread_id,
        thread_id="child-thread",
        profile_id="quality_reviewer",
        display_name="Status child",
        max_turns=3,
        turns_used=1,
    )
    assert agent_runs.acquire_agent_write_lock(
        "thread:status",
        "status-run-1",
        thread_id="child-thread",
        workspace_path=str(tmp_path),
    )
    goal = goals.start_goal(thread_id, "ship status coverage", max_turns=5)
    goals.update_goal_progress(
        goal_id=goal["id"],
        progress="Seeded the status checks",
        evidence=["tests/test_row_bot_status_agents.py"],
        next_step="Run focused pytest",
    )
    goals.pause_goal(thread_id, reason="Waiting on status smoke")
    agent._set_active_runtime_context(
        thread_id=thread_id,
        enabled_tool_names=[],
        tool_allowlist=[
            "conversation_search",
            "documents",
            "filesystem",
            "memory",
            "row_bot_status",
            "system_info",
            "url_reader",
        ],
        agent_profile_id="review",
    )

    agents = status_tool._row_bot_status("agents")
    assert "**Agents**" in agents
    assert "Current runs:" in agents
    assert "Recent runs:" not in agents
    assert "Status child" in agents
    assert "subagent/running" in agents
    assert "thread:status" in agents
    assert "90 model rounds" in agents
    assert "3 active per parent" in agents
    assert "Dispatcher: 0 active, 0 queued" in agents

    agent_profiles = status_tool._row_bot_status("agent_profiles")
    assert "**Agent Profiles**" in agent_profiles
    assert "Active thread profile: Review" in agent_profiles
    assert "builtin " in agent_profiles
    assert "user_created 1" in agent_profiles
    assert "enabled /" in agent_profiles
    assert "total" in agent_profiles
    assert "Tool modes:" in agent_profiles
    assert "inherited enabled tools" in agent_profiles
    assert "selected tools" in agent_profiles
    assert "Selected tool sources:" in agent_profiles
    assert "core=" in agent_profiles
    assert "Active profile tool mode: selected tools" in agent_profiles
    assert "Active profile selected tools:" in agent_profiles
    assert "(filesystem)" in agent_profiles
    assert "(row_bot_status)" in agent_profiles
    assert "runtime-bound" in agent_profiles
    assert "other global tools are not bound" in agent_profiles

    goal_status = status_tool._row_bot_status("goals")
    assert "**Goals**" in goal_status
    assert "Current goals:" in goal_status
    assert "Recent goals:" not in goal_status
    assert "ship status coverage" in goal_status
    assert "paused" in goal_status
    assert "Seeded the status checks" in goal_status
    assert "Default turn budget: 20" in goal_status


def test_row_bot_status_agent_goal_categories_are_discoverable(tmp_path, monkeypatch):
    *_modules, status_tool = _fresh_status_modules(tmp_path, monkeypatch)

    assert {"agents", "agent_profiles", "goals", "plugins"} <= set(status_tool._QUERY_HANDLERS)
    tool = next(
        item
        for item in status_tool.RowBotStatusTool().as_langchain_tools()
        if item.name == "row_bot_status"
    )
    assert "tools, plugins, mcp" in tool.description

    guide = Path("tool_guides/row_bot_status_guide/SKILL.md").read_text(encoding="utf-8").lower()
    assert "category='agents'" in guide
    assert "category='agent_profiles'" in guide
    assert "category='goals'" in guide
    assert "category='plugins'" in guide
    assert "current durable agent runs" in guide
    assert "current goal mode status" in guide
    assert "global enabled/disabled tools" in guide
    assert "effective thread tool scope" in guide
    assert "runtime-bound" in guide
    assert "read-only through row_bot_status in v1" in guide
    assert "pinned brain choices" in guide
    assert "row_bot_status with category='model'" in guide
    assert "canonical ref" in guide

    manifest_source = Path("scripts/app_payload_manifest.py").read_text(encoding="utf-8")
    assert '"tool_guides"' in manifest_source


def test_row_bot_status_reports_plugin_tools_and_stale_plugins(tmp_path, monkeypatch):
    *_modules, status_tool = _fresh_status_modules(tmp_path, monkeypatch)

    from row_bot.plugins import loader as plugin_loader
    from row_bot.plugins import registry as plugin_registry
    from row_bot.plugins import state as plugin_state

    manifest = SimpleNamespace(
        id="rss-reader",
        name="RSS Reader",
        version="1.0.0",
    )
    monkeypatch.setattr(plugin_registry, "get_loaded_manifests", lambda: [manifest])
    monkeypatch.setattr(plugin_state, "is_plugin_enabled", lambda plugin_id: plugin_id == "rss-reader")
    monkeypatch.setattr(
        plugin_registry,
        "get_enabled_plugin_tool_records",
        lambda: [{
            "runtime_name": "rss_fetch_feed",
            "plugin_id": "rss-reader",
            "plugin_name": "RSS Reader",
            "label": "Fetch Feed",
            "description": "Read RSS feeds",
            "destructive": False,
        }],
    )
    stale = plugin_loader.LoadResult(
        plugin_id="old-thoth",
        success=True,
        stale=True,
        stale_path=str(tmp_path / "data" / "stale_plugins" / "old-thoth"),
    )
    monkeypatch.setattr(
        plugin_loader,
        "get_load_summary",
        lambda: {"total": 2, "loaded": 1, "failed": 0, "stale": 1, "results": [stale]},
    )

    plugins = status_tool._row_bot_status("plugins")
    tools = status_tool._row_bot_status("tools")

    assert "**Plugins**" in plugins
    assert "RSS Reader (rss-reader) v1.0.0: enabled" in plugins
    assert "RSS Reader: Fetch Feed (rss_fetch_feed)" in plugins
    assert "old-thoth" in plugins
    assert "Plugin tools:" in tools
    assert "rss_fetch_feed" in tools


def test_row_bot_status_agents_goals_empty_state_and_overview(tmp_path, monkeypatch):
    *_modules, status_tool = _fresh_status_modules(tmp_path, monkeypatch)

    agents = status_tool._row_bot_status("agents")
    assert "No durable Agent Runs recorded." in agents
    assert "90 model rounds" in agents
    assert "3 active per parent" in agents

    goals = status_tool._row_bot_status("goals")
    assert "No Goal Mode records yet." in goals
    assert "Default turn budget: 20" in goals

    overview = status_tool._row_bot_status("overview")
    assert "**Agents**" in overview
    assert "**Agent Profiles**" in overview
    assert "**Goals**" in overview


def test_row_bot_status_tools_distinguishes_global_and_effective_profile_scope(tmp_path, monkeypatch):
    threads, _profiles, _agent_runs, _goals, agent, status_tool = _fresh_status_modules(
        tmp_path,
        monkeypatch,
    )
    thread_id = threads.create_thread("Browser status thread")
    threads._set_thread_agent_profile(thread_id, "web_ui_checker")
    agent._set_active_runtime_context(
        thread_id=thread_id,
        enabled_tool_names=[],
        tool_allowlist=["browser", "filesystem", "row_bot_status", "system_info", "vision"],
        agent_profile_id="ui_check",
    )

    tools = status_tool._row_bot_status("tools")

    assert "**Tools**" in tools
    assert "Global catalog:" in tools
    assert "**Effective Thread Tool Scope**" in tools
    assert "Active profile: UI Check" in tools
    assert "selected tools are runtime-bound" in tools
    assert "other global tools are not bound" in tools
    assert "Profile-selected tool groups:" in tools
    assert "skill_search and skill_load" not in tools
    for tool_id in ("browser", "filesystem", "row_bot_status", "system_info", "vision"):
        assert f"({tool_id})" in tools


@pytest.mark.parametrize("runtime_surface", ["agent_child", "agent_child_resume"])
def test_row_bot_status_tools_explains_separate_bounded_child_skill_bridges(
    tmp_path,
    monkeypatch,
    runtime_surface,
):
    threads, _profiles, _agent_runs, _goals, agent, status_tool = _fresh_status_modules(
        tmp_path,
        monkeypatch,
    )
    thread_id = threads.create_thread("Restricted child status")
    threads._set_thread_agent_profile(thread_id, "web_ui_checker")
    agent._set_active_runtime_context(
        thread_id=thread_id,
        enabled_tool_names=[],
        tool_allowlist=["browser", "row_bot_status"],
        agent_profile_id="ui_check",
        runtime_surface=runtime_surface,
    )

    tools = status_tool._row_bot_status("tools")

    assert "Profile-selected tool groups:" in tools
    assert "skill_search and skill_load" in tools
    assert "separate from the tool-group allowlist" in tools
    assert "cannot add tools" in tools
    assert "profile, approval, or workspace boundaries" in tools


def test_row_bot_status_tools_keeps_global_catalog_without_profile_allowlist(tmp_path, monkeypatch):
    *_modules, status_tool = _fresh_status_modules(tmp_path, monkeypatch)

    tools = status_tool._row_bot_status("tools")

    assert "**Tools**" in tools
    assert "Global catalog:" in tools
    assert "**Effective Thread Tool Scope**" not in tools
    assert "runtime-bound" not in tools


def test_row_bot_status_tools_reports_inherited_profile_scope(tmp_path, monkeypatch):
    threads, _profiles, _agent_runs, _goals, agent, status_tool = _fresh_status_modules(
        tmp_path,
        monkeypatch,
    )
    thread_id = threads.create_thread("Worker status thread")
    threads._set_thread_agent_profile(thread_id, "worker")
    agent._set_active_runtime_context(
        thread_id=thread_id,
        enabled_tool_names=[],
        agent_profile_id="worker",
    )

    tools = status_tool._row_bot_status("tools")
    agent_profiles = status_tool._row_bot_status("agent_profiles")

    assert "**Effective Thread Tool Scope**" in tools
    assert "Active profile: Worker" in tools
    assert "inherits all globally enabled tools" in tools
    assert "selected tools are runtime-bound" not in tools
    assert "Active profile tool mode: inherited enabled tools" in agent_profiles


def test_row_bot_status_model_setting_uses_strict_canonical_refs(tmp_path, monkeypatch):
    selection = _isolated_model_choices(tmp_path, monkeypatch)
    selection.add_quick_choice_for_model(
        "gpt-4o-mini",
        provider_id="openai",
        display_name="Fast OpenAI",
        capabilities_snapshot=_chat_snapshot(),
    )
    *_modules, agent, status_tool = _fresh_status_modules(tmp_path, monkeypatch)
    captured = {}

    monkeypatch.setattr(status_tool, "_approval_gate_bool", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("row_bot.models.set_model", lambda value: captured.setdefault("model", value))
    monkeypatch.setattr(agent, "clear_agent_cache", lambda: captured.setdefault("cache_cleared", True))

    result = status_tool._update_setting("model", "Fast OpenAI")

    assert result == "Active model changed to: model:openai:gpt-4o-mini"
    assert captured["model"] == "model:openai:gpt-4o-mini"
    assert captured["cache_cleared"] is True


def test_row_bot_status_thread_model_setting_writes_thread_override(tmp_path, monkeypatch):
    selection = _isolated_model_choices(tmp_path, monkeypatch)
    selection.add_quick_choice_for_model(
        "gpt-4o-mini",
        provider_id="openai",
        display_name="Fast OpenAI",
        capabilities_snapshot=_chat_snapshot(),
    )
    threads, *_modules, agent, status_tool = _fresh_status_modules(tmp_path, monkeypatch)
    thread_id = threads.create_thread("Thread model scope")
    captured = {}

    agent._set_active_runtime_context(thread_id=thread_id, runtime_surface="channel")
    monkeypatch.setattr(status_tool, "_approval_gate_bool", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("row_bot.models.set_model", lambda _value: pytest.fail("thread_model must not change global default"))
    monkeypatch.setattr(agent, "clear_agent_cache", lambda: captured.setdefault("cache_cleared", True))

    try:
        result = status_tool._update_setting("thread_model", "Fast OpenAI")
    finally:
        agent._set_active_runtime_context()

    assert result == "Thread model override changed to: model:openai:gpt-4o-mini"
    assert threads._get_thread_model_override(thread_id) == "model:openai:gpt-4o-mini"
    assert captured["cache_cleared"] is True


def test_row_bot_status_thread_model_default_clears_override(tmp_path, monkeypatch):
    selection = _isolated_model_choices(tmp_path, monkeypatch)
    selection.add_quick_choice_for_model(
        "gpt-4o-mini",
        provider_id="openai",
        display_name="Fast OpenAI",
        capabilities_snapshot=_chat_snapshot(),
    )
    threads, *_modules, agent, status_tool = _fresh_status_modules(tmp_path, monkeypatch)
    thread_id = threads.create_thread("Thread model clear")
    threads._set_thread_model_override(thread_id, "model:openai:gpt-4o-mini")
    captured = {}

    agent._set_active_runtime_context(thread_id=thread_id, runtime_surface="normal_chat")
    monkeypatch.setattr(status_tool, "_approval_gate_bool", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(agent, "clear_agent_cache", lambda: captured.setdefault("cache_cleared", True))

    try:
        result = status_tool._update_setting("thread_model", "default")
    finally:
        agent._set_active_runtime_context()

    assert result.startswith("Thread model override cleared; using global default:")
    assert threads._get_thread_model_override(thread_id) == ""
    assert captured["cache_cleared"] is True


def test_row_bot_status_default_model_setting_changes_global_default(tmp_path, monkeypatch):
    selection = _isolated_model_choices(tmp_path, monkeypatch)
    selection.add_quick_choice_for_model(
        "gpt-4o-mini",
        provider_id="openai",
        display_name="Fast OpenAI",
        capabilities_snapshot=_chat_snapshot(),
    )
    *_modules, agent, status_tool = _fresh_status_modules(tmp_path, monkeypatch)
    captured = {}

    monkeypatch.setattr(status_tool, "_approval_gate_bool", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("row_bot.models.set_model", lambda value: captured.setdefault("model", value))
    monkeypatch.setattr(agent, "clear_agent_cache", lambda: captured.setdefault("cache_cleared", True))

    result = status_tool._update_setting("default_model", "Fast OpenAI")

    assert result == "Global default model changed to: model:openai:gpt-4o-mini"
    assert captured["model"] == "model:openai:gpt-4o-mini"
    assert captured["cache_cleared"] is True


def test_row_bot_status_legacy_model_refuses_threaded_ambiguity(tmp_path, monkeypatch):
    selection = _isolated_model_choices(tmp_path, monkeypatch)
    selection.add_quick_choice_for_model(
        "gpt-4o-mini",
        provider_id="openai",
        display_name="Fast OpenAI",
        capabilities_snapshot=_chat_snapshot(),
    )
    threads, *_modules, agent, status_tool = _fresh_status_modules(tmp_path, monkeypatch)
    thread_id = threads.create_thread("Ambiguous model scope")

    agent._set_active_runtime_context(thread_id=thread_id, runtime_surface="channel")
    monkeypatch.setattr("row_bot.models.set_model", lambda _value: pytest.fail("ambiguous model must not change global default"))

    try:
        result = status_tool._update_setting("model", "Fast OpenAI")
    finally:
        agent._set_active_runtime_context()

    assert "Model scope is ambiguous" in result
    assert threads._get_thread_model_override(thread_id) == ""


def test_row_bot_status_model_lists_pinned_brain_choices(tmp_path, monkeypatch):
    selection = _isolated_model_choices(tmp_path, monkeypatch)
    selection.add_quick_choice_for_model(
        "gpt-4o-mini",
        provider_id="openai",
        display_name="Fast OpenAI",
        capabilities_snapshot=_chat_snapshot(),
    )
    *_modules, status_tool = _fresh_status_modules(tmp_path, monkeypatch)

    result = status_tool._row_bot_status("model")

    assert "Pinned Brain Model Choices" in result
    assert "For natural Brain model requests" in result
    assert "Fast OpenAI - OpenAI API" in result
    assert "Canonical ref: model:openai:gpt-4o-mini" in result
    assert "provider_id: openai" in result
    assert "model_id: gpt-4o-mini" in result


def test_row_bot_status_model_lists_all_pinned_brain_choices_but_overview_stays_compact(tmp_path, monkeypatch):
    selection = _isolated_model_choices(tmp_path, monkeypatch)
    for index in range(10):
        selection.add_quick_choice_for_model(
            f"model-{index}",
            provider_id="openai",
            display_name=f"Model {index}",
            capabilities_snapshot=_chat_snapshot(),
        )
    *_modules, status_tool = _fresh_status_modules(tmp_path, monkeypatch)

    full = status_tool._row_bot_status("model")
    compact = status_tool._query_model(compact_pinned=True)

    assert "Model 9 - OpenAI API" in full
    assert "Plus" not in full
    assert "Plus" in compact


def test_row_bot_status_model_setting_blocks_child_runtime_self_switch_before_approval(tmp_path, monkeypatch):
    *_modules, agent, status_tool = _fresh_status_modules(tmp_path, monkeypatch)

    def fail_approval(*_args, **_kwargs):
        raise AssertionError("child model switch should not request approval")

    monkeypatch.setattr(status_tool, "_approval_gate_bool", fail_approval)
    agent._set_active_runtime_context(runtime_surface="agent_child")
    try:
        result = status_tool._update_setting("model", "model:openai:gpt-5.5")
    finally:
        agent._set_active_runtime_context()

    assert "Child Agents cannot switch their own runtime model" in result
    assert "delegate_work(model=...)" in result


def test_row_bot_status_model_setting_rejects_unpinned_canonical_ref(tmp_path, monkeypatch):
    _isolated_model_choices(tmp_path, monkeypatch)
    *_modules, status_tool = _fresh_status_modules(tmp_path, monkeypatch)
    captured = {}

    monkeypatch.setattr(status_tool, "_approval_gate_bool", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("row_bot.models.set_model", lambda value: captured.setdefault("model", value))

    result = status_tool._update_setting("model", "model:openai:gpt-4o-mini")

    assert "not pinned for Brain" in result
    assert captured == {}


def test_row_bot_status_model_setting_rejects_ambiguous_model(tmp_path, monkeypatch):
    selection = _isolated_model_choices(tmp_path, monkeypatch)
    selection.add_quick_choice_for_model(
        "shared-model",
        provider_id="openai",
        display_name="Shared Model",
        capabilities_snapshot=_chat_snapshot(),
    )
    selection.add_quick_choice_for_model(
        "shared-model",
        provider_id="anthropic",
        display_name="Shared Model",
        capabilities_snapshot=_chat_snapshot(),
    )
    *_modules, status_tool = _fresh_status_modules(tmp_path, monkeypatch)
    captured = {}

    monkeypatch.setattr(status_tool, "_approval_gate_bool", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("row_bot.models.set_model", lambda value: captured.setdefault("model", value))

    result = status_tool._update_setting("model", "Shared Model")

    assert "Ambiguous model selection" in result
    assert captured == {}
