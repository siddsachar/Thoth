from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from row_bot.agent_budget import new_execution_budget


def _fresh_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.agent as agent

    return importlib.reload(agent)


def _has_cache_control(message) -> bool:
    content = getattr(message, "content", "")
    return isinstance(content, list) and any(
        isinstance(block, dict) and "cache_control" in block
        for block in content
    )


@pytest.mark.parametrize(
    ("provider_id", "model_id", "uses_anthropic_messages"),
    [
        ("minimax", "MiniMax-M2.7", True),
        ("opencode_zen", "anthropic/claude-sonnet-4-5", True),
        ("opencode_go", "anthropic/claude-sonnet-4-5", True),
        ("claude_subscription", "claude-sonnet-4-5", True),
        ("codex", "gpt-5.5", False),
        ("xai_oauth", "grok-4", False),
        ("openai", "gpt-5.5", False),
        ("openrouter", "anthropic/claude-sonnet-4-5", True),
        ("requesty", "anthropic/claude-sonnet-4-5", True),
        ("custom_openai_lab", "local-chat", False),
        ("atlascloud", "anthropic/claude-sonnet-4-5", False),
    ],
)
def test_prompt_cache_markers_are_provider_gated(
    tmp_path,
    monkeypatch,
    provider_id: str,
    model_id: str,
    uses_anthropic_messages: bool,
):
    agent = _fresh_agent(tmp_path, monkeypatch)
    model_ref = f"model:{provider_id}:{model_id}"
    decision = SimpleNamespace(
        allowed=False,
        reason="disabled",
        candidates_seen=0,
        selected=[],
        trace={},
    )

    monkeypatch.setattr(agent, "get_context_size", lambda: 65_536)
    monkeypatch.setattr(agent, "get_current_model", lambda: model_ref)
    monkeypatch.setattr(agent, "is_cloud_model", lambda model: True)
    monkeypatch.setattr(agent, "get_cloud_provider", lambda model: provider_id)
    monkeypatch.setattr(agent, "is_background_workflow", lambda: False)
    monkeypatch.setattr(
        agent,
        "_provider_uses_anthropic_messages",
        lambda pid, _model_id=None: bool(uses_anthropic_messages and pid == provider_id),
    )
    monkeypatch.setattr("row_bot.self_knowledge.build_static_self_knowledge_block", lambda: "SELF_SENTINEL")
    monkeypatch.setattr("row_bot.self_knowledge.build_dynamic_self_knowledge_block", lambda: "")
    monkeypatch.setattr("row_bot.skills.get_skills_prompt", lambda *args, **kwargs: "")
    monkeypatch.setattr("row_bot.plugins.registry.get_skills_prompt", lambda *args, **kwargs: "")
    monkeypatch.setattr("row_bot.memory_policy.build_auto_recall", lambda *args, **kwargs: decision)
    monkeypatch.setattr("row_bot.memory_policy.touch_selected_memories", lambda recall_decision: None)
    monkeypatch.setattr("row_bot.memory_policy.record_recall_trace", lambda recall_decision, **kwargs: None)

    agent._set_active_runtime_context(thread_id=f"cache-{provider_id}", enabled_tool_names=())
    agent.set_active_model_override(model_ref)
    try:
        result = agent._pre_model_trim({
            "execution_budget": new_execution_budget(f"cache-{provider_id}"),
            "messages": [
                SystemMessage(content="ROOT_SENTINEL"),
                HumanMessage(content="hello"),
            ]
        })["llm_input_messages"]
    finally:
        agent.set_active_model_override("")

    assert not any(_has_cache_control(message) for message in result)
