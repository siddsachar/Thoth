from __future__ import annotations

import importlib
from types import SimpleNamespace

from langchain_core.messages import HumanMessage, SystemMessage


def _fresh_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.agent as agent

    return importlib.reload(agent)


def _quiet_prompt_dependencies(agent, monkeypatch) -> None:
    decision = SimpleNamespace(
        allowed=False,
        reason="disabled_for_test",
        candidates_seen=0,
        selected=[],
        trace={},
    )
    monkeypatch.setattr(agent, "get_context_size", lambda *args, **kwargs: 200_000)
    monkeypatch.setattr("row_bot.prompts.get_platform_context", lambda: "")
    monkeypatch.setattr("row_bot.self_knowledge.build_static_self_knowledge_block", lambda: "")
    monkeypatch.setattr("row_bot.self_knowledge.build_dynamic_self_knowledge_block", lambda: "")
    monkeypatch.setattr("row_bot.skills.get_skills_prompt", lambda *args, **kwargs: "")
    monkeypatch.setattr("row_bot.plugins.registry.get_skills_prompt", lambda *args, **kwargs: "")
    monkeypatch.setattr("row_bot.memory_policy.build_auto_recall", lambda *args, **kwargs: decision)
    monkeypatch.setattr("row_bot.memory_policy.record_recall_trace", lambda *args, **kwargs: None)
    monkeypatch.setattr("row_bot.memory_policy.touch_selected_memories", lambda *args, **kwargs: None)


def _prompt_for(agent, *, surface: str, selected_mode: str = "agent", channel_streaming: bool = False) -> str:
    agent._set_active_runtime_context(
        thread_id=f"progress-{surface}-{selected_mode}",
        runtime_surface=surface,
        requested_runtime_mode="agent",
        selected_runtime_mode=selected_mode,
        enabled_tool_names=[],
        channel_streaming=channel_streaming,
    )
    result = agent._pre_model_trim({
        "execution_budget": agent.new_execution_budget(f"progress-{surface}-{selected_mode}"),
        "messages": [
            SystemMessage(content="Base system"),
            HumanMessage(content="Please do a multi-step check."),
        ]
    })
    return "\n".join(
        str(getattr(message, "content", ""))
        for message in result["llm_input_messages"]
        if getattr(message, "type", "") == "system"
    )


def test_interactive_progress_contract_is_injected_for_interactive_agent_surfaces(tmp_path, monkeypatch):
    agent = _fresh_agent(tmp_path, monkeypatch)
    _quiet_prompt_dependencies(agent, monkeypatch)

    for surface in ("normal_chat", "designer", "developer"):
        prompt = _prompt_for(agent, surface=surface)
        assert "INTERACTIVE PROGRESS:" in prompt
        assert "stream occasional short user-facing progress updates" in prompt
        assert "do not narrate every tool call" in prompt


def test_streaming_channel_gets_sparse_progress_contract(tmp_path, monkeypatch):
    agent = _fresh_agent(tmp_path, monkeypatch)
    _quiet_prompt_dependencies(agent, monkeypatch)

    prompt = _prompt_for(agent, surface="channel", channel_streaming=True)

    assert "INTERACTIVE PROGRESS:" in prompt
    assert "keep progress updates especially sparse" in prompt


def test_progress_contract_is_omitted_for_noninteractive_or_nonstreaming_paths(tmp_path, monkeypatch):
    agent = _fresh_agent(tmp_path, monkeypatch)
    _quiet_prompt_dependencies(agent, monkeypatch)

    assert "INTERACTIVE PROGRESS:" not in _prompt_for(agent, surface="approval")
    assert "INTERACTIVE PROGRESS:" not in _prompt_for(
        agent,
        surface="normal_chat",
        selected_mode="chat_only",
    )
    assert "INTERACTIVE PROGRESS:" not in _prompt_for(
        agent,
        surface="channel",
        channel_streaming=False,
    )

    token = agent._background_workflow_var.set(True)
    try:
        prompt = _prompt_for(agent, surface="normal_chat")
    finally:
        agent._background_workflow_var.reset(token)
    assert "INTERACTIVE PROGRESS:" not in prompt
