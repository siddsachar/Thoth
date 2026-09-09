"""An oversized input and an unresolved explicit selection stay request-local."""
from __future__ import annotations

from dataclasses import replace
from langchain_core.messages import HumanMessage
import pytest

from tests.subsystem.agents.test_context_compaction import _inputs


def test_f_r05_overflowed_request_does_not_quarantine_valid_same_model_input(monkeypatch):
    from row_bot import agent
    first = _inputs([HumanMessage(content="oversized synthetic input" * 1000)])
    second = replace(_inputs([HumanMessage(content="valid synthetic input")]), checkpoint_revision="other-request")
    monkeypatch.setattr(agent, "_validated_summary_for_inputs", lambda _: None)
    monkeypatch.setattr(agent, "_emit_context_event", lambda *_: None)
    monkeypatch.setattr(agent, "_persist_context_usage", lambda *_: None)
    agent._CONTEXT_OVERFLOWED_MODELS.clear()
    agent._mark_context_overflow(first)
    prepared = agent._prepare_with_compaction(second)
    assert prepared.messages[-1].content == "valid synthetic input"
    assert second.model_ref not in agent._CONTEXT_OVERFLOWED_MODELS


@pytest.mark.parametrize("entry", ["normal", "resume_stream", "child", "child_resume", "channel"])
def test_f_r11_unresolved_explicit_selection_never_reaches_default_model(entry, monkeypatch):
    from row_bot import agent, agent_runner
    from row_bot.providers import resolution
    from row_bot.channels.runtime import prepare_channel_turn_config
    import threading
    import contextvars
    calls = []
    monkeypatch.setattr(agent, "get_current_model", lambda: "configured-default")
    monkeypatch.setattr(agent, "is_model_local", lambda _: False)
    monkeypatch.setattr(agent, "is_cloud_model", lambda _: False)
    monkeypatch.setattr(resolution, "resolve_provider_config", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("missing")))
    monkeypatch.setattr(agent, "_ensure_agent_mode_ready", lambda model: calls.append(model))
    monkeypatch.setattr(agent, "_route_waiting_parent_input", lambda *_: None)
    config = {"configurable": {"model_override": "unresolved-explicit-model", "runtime_surface": "normal_chat", "thread_id": "synthetic-selection"}}
    stop = threading.Event()
    def dispatch():
        if entry == "normal":
            list(agent.stream_agent("Synthetic input", [], config, stop_event=stop))
        elif entry == "resume_stream":
            list(agent.resume_stream_agent([], config, True, stop_event=stop))
        elif entry == "child":
            config["configurable"]["runtime_surface"] = "child"
            agent_runner._invoke_agent("Synthetic mission", [], config, stop_event=stop)
        elif entry == "child_resume":
            config["configurable"]["runtime_surface"] = "child"
            agent_runner._resume_invoke_agent([], config, True, stop_event=stop)
        else:
            config["configurable"].pop("runtime_surface")
            channel = prepare_channel_turn_config(config, "Synthetic channel input")
            assert channel["configurable"]["model_override"] == "unresolved-explicit-model"
            assert channel["configurable"]["runtime_surface"] == "channel"
            list(agent.stream_agent("Synthetic channel input", [], channel, stop_event=stop))
    # Runtime entrypoints intentionally retain tool authorization for their
    # owning worker lifetime. Direct tests must use that same context boundary.
    before = agent.get_active_runtime_context()
    with pytest.raises(ValueError, match="explicitly selected model is unavailable"):
        contextvars.Context().run(dispatch)
    assert agent.get_active_runtime_context() == before
    assert calls == []


@pytest.mark.parametrize("mode", ["focused", "recent"])
def test_f_r07_child_packet_and_full_tool_envelope_are_checked_together(mode, monkeypatch):
    from row_bot import agent, agent_context
    from langchain_core.messages import SystemMessage
    from tests.subsystem.agents.test_context_preparation import _inputs as complete_inputs
    monkeypatch.setattr(agent_context, "load_parent_context", lambda *args, **kwargs: {
        "summary": "Old context " * 50000, "recent": "Complete earlier tool group " * 50000,
        "full": "Complete earlier tool group " * 50000, "message_count": 2000})
    packet = agent_context.build_child_agent_prompt(objective="Required mission", context="Supplement " * 50000,
        context_mode=mode, profile_snapshot={"instructions": "Mandatory safety policy",
                                            "context_policy_json": {"max_context_tokens": 400}})
    messages = [SystemMessage(content="Mandatory runtime restrictions"), HumanMessage(content=packet["prompt"])]
    tools = ({"type": "function", "function": {"name": "bounded_tool", "description": "schema " * 25000}},)
    oversized = complete_inputs(messages, tools=tools, effective=2500, usable=2000, compact_at=1900)
    monkeypatch.setattr(agent, "_validated_summary_for_inputs", lambda _: None)
    monkeypatch.setattr(agent, "_emit_context_event", lambda *_: None)
    monkeypatch.setattr(agent, "_persist_context_usage", lambda *_: None)
    with pytest.raises(agent.ContextCompactionError, match="fixed prompt and tool schemas"):
        agent._prepare_with_compaction(oversized)
    fitted = replace(oversized, canonical_tools=({"type": "function", "function": {"name": "bounded_tool"}},))
    prepared = agent._prepare_with_compaction(fitted)
    assert prepared.usage.estimated_input_tokens <= 2000
    assert "Required mission" in prepared.messages[-1].content
    assert "Mandatory safety policy" in prepared.messages[-1].content
    assert prepared.messages[0].content == "Mandatory runtime restrictions"
