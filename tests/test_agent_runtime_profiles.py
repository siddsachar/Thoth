from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace

from langchain_core.messages import HumanMessage, SystemMessage

from row_bot.agent_budget import new_execution_budget


def _fresh_runtime_modules(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(data_dir))
    for name in (
        "row_bot.tasks",
        "row_bot.threads",
        "row_bot.agent_profiles",
        "row_bot.agent",
        "row_bot.ui.streaming",
        "row_bot.ui.profile_picker",
        "row_bot.ui.profile_library",
    ):
        sys.modules.pop(name, None)

    import row_bot.tasks as tasks
    import row_bot.threads as threads
    import row_bot.agent_profiles as agent_profiles
    import row_bot.agent as agent
    import row_bot.ui.streaming as streaming
    import row_bot.ui.profile_picker as profile_picker
    import row_bot.ui.profile_library as profile_library

    tasks = importlib.reload(tasks)
    threads = importlib.reload(threads)
    agent_profiles = importlib.reload(agent_profiles)
    agent = importlib.reload(agent)
    streaming = importlib.reload(streaming)
    profile_picker = importlib.reload(profile_picker)
    profile_library = importlib.reload(profile_library)
    return threads, agent_profiles, agent, streaming, profile_picker, profile_library


def _prompt_text(result: dict) -> str:
    return "\n".join(str(message.content) for message in result["llm_input_messages"])


def test_graph_interrupt_result_extracts_paused_state_for_non_streaming_invoke(
    tmp_path,
    monkeypatch,
):
    _threads, _profiles, agent, _streaming, _picker, _library = _fresh_runtime_modules(
        tmp_path,
        monkeypatch,
    )
    state = SimpleNamespace(
        next=("tools",),
        tasks=[
            SimpleNamespace(
                interrupts=[
                    SimpleNamespace(
                        id="interrupt-parent-1",
                        value={
                            "tool": "run_command",
                            "approval_reason": "Authorize the disposable smoke file.",
                        },
                    )
                ]
            )
        ],
    )

    assert agent._graph_interrupt_result(state) == {
        "type": "interrupt",
        "interrupts": [
            {
                "tool": "run_command",
                "approval_reason": "Authorize the disposable smoke file.",
                "__interrupt_id": "interrupt-parent-1",
            }
        ],
    }


def test_thread_agent_profile_is_injected_into_agent_mode_prompt(tmp_path, monkeypatch):
    threads, _profiles, agent, _streaming, _picker, _library = _fresh_runtime_modules(tmp_path, monkeypatch)
    thread_id = threads.create_thread("Profile runtime")
    threads._set_thread_agent_profile(thread_id, "quality_reviewer")

    agent._set_active_runtime_context(thread_id=thread_id, enabled_tool_names=[])
    trimmed = agent._pre_model_trim({
        "execution_budget": new_execution_budget(f"profile-{thread_id}"),
        "messages": [
            SystemMessage(content="Base system"),
            HumanMessage(content="Review this change."),
        ]
    })
    prompt = _prompt_text(trimmed)

    assert "AGENT PROFILE: Review" in prompt
    assert "Findings first" in prompt
    assert "capability=read_only" in prompt


def test_thread_agent_profile_is_injected_into_chat_only_prompt(tmp_path, monkeypatch):
    threads, _profiles, agent, _streaming, _picker, _library = _fresh_runtime_modules(tmp_path, monkeypatch)
    thread_id = threads.create_thread("Chat only profile")
    threads._set_thread_agent_profile(thread_id, "planner")

    agent._set_active_runtime_context(thread_id=thread_id, enabled_tool_names=[])
    messages = agent._build_chat_only_messages(thread_id, "Plan this.", context_window=4096)
    text = "\n".join(str(message.content) for message in messages if isinstance(message, SystemMessage))

    assert "AGENT PROFILE: Plan" in text
    assert "Turn fuzzy goals into a concise plan" in text


def test_disabled_thread_profile_warns_instead_of_silent_fallback(tmp_path, monkeypatch):
    threads, profiles, agent, _streaming, _picker, _library = _fresh_runtime_modules(tmp_path, monkeypatch)
    thread_id = threads.create_thread("Disabled profile")
    saved = profiles.save_agent_profile(
        slug="brief_reviewer",
        display_name="Brief Reviewer",
        description="Review briefly.",
        instructions="Only review the risk.",
        tool_policy_json={"capability": "read_only"},
        context_policy_json={"default_context_mode": "focused"},
        workspace_policy_json={"workspace_mode_default": "read_only"},
    )
    threads._set_thread_agent_profile(thread_id, saved["id"])
    profiles.save_agent_profile({**saved, "enabled": False})

    agent._set_active_runtime_context(thread_id=thread_id, enabled_tool_names=[])
    trimmed = agent._pre_model_trim({
        "execution_budget": new_execution_budget(f"disabled-profile-{thread_id}"),
        "messages": [
            SystemMessage(content="Base system"),
            HumanMessage(content="Review this change."),
        ]
    })
    prompt = _prompt_text(trimmed)

    assert "THREAD AGENT PROFILE WARNING" in prompt
    assert "brief_reviewer" in prompt
    assert "disabled" in prompt


def test_thread_profile_allowlist_flows_into_chat_stream_config(tmp_path, monkeypatch):
    threads, profiles, _agent, streaming, _picker, _library = _fresh_runtime_modules(tmp_path, monkeypatch)
    thread_id = threads.create_thread("Profile tool filter")
    saved = profiles.save_agent_profile(
        slug="tool_filter_smoke",
        display_name="Tool Filter Smoke",
        description="Constrain tools for smoke checks.",
        instructions="Use only the selected tools.",
        tool_policy_json={
            "capability": "read_only",
            "allow_tools": ["filesystem", "row_bot_status"],
        },
        context_policy_json={"default_context_mode": "focused"},
        workspace_policy_json={"workspace_mode_default": "read_only"},
    )
    threads._set_thread_agent_profile(thread_id, saved["id"])

    config = streaming._profile_runtime_config_for_thread(thread_id)

    assert config["agent_profile_id"] == saved["id"]
    assert config["agent_profile_snapshot"]["slug"] == "tool_filter_smoke"
    assert config["tool_allowlist"] == ["filesystem", "row_bot_status"]
    assert streaming._profile_runtime_config_for_thread("missing-thread") == {}


def test_top_selector_profile_use_flows_into_direct_runtime_config(tmp_path, monkeypatch):
    threads, profiles, _agent, streaming, profile_picker, _library = _fresh_runtime_modules(tmp_path, monkeypatch)
    thread_id = threads.create_thread("Picker profile")
    research = profiles.require_agent_profile("research")

    result = profile_picker._apply_profile_picker_selection(
        thread_id,
        research["id"],
        profiles_by_value={research["id"]: research},
    )
    config = streaming._profile_runtime_config_for_thread(thread_id)

    assert result["stored"] == {"id": "builtin:research", "slug": "research"}
    assert threads._get_thread_agent_profile(thread_id) == result["stored"]
    assert threads.get_thread_skills_override(thread_id) == ["deep_research", "web_navigator"]
    assert config["agent_profile_id"] == "builtin:research"
    assert config["tool_allowlist"] == [
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
    ]


def test_profile_library_chat_button_flows_into_direct_runtime_config(tmp_path, monkeypatch):
    threads, profiles, _agent, streaming, _picker, profile_library = _fresh_runtime_modules(tmp_path, monkeypatch)
    research = profiles.require_agent_profile("research")

    thread_id, title = profile_library._create_profile_chat_thread(research)
    config = streaming._profile_runtime_config_for_thread(thread_id)

    assert title == "Research chat"
    assert threads._get_thread_agent_profile(thread_id) == {
        "id": "builtin:research",
        "slug": "research",
    }
    assert threads.get_thread_skills_override(thread_id) == ["deep_research", "web_navigator"]
    assert config["agent_profile_id"] == "builtin:research"
    assert "row_bot_status" in config["tool_allowlist"]
    assert "memory" in config["tool_allowlist"]
    assert "shell" in config["tool_allowlist"]
