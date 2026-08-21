from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

import row_bot.agent as agent


pytestmark = pytest.mark.subsystem


def _inputs(
    messages,
    *,
    raw=None,
    mode="agent",
    effective=1_000,
    usable=850,
    compact_at=750,
    model_ref="model:openai:test-context",
    tools=(),
):
    policy = SimpleNamespace(
        native_limit_tokens=effective,
        effective_limit_tokens=effective,
        usable_input_tokens=usable,
        compact_at_tokens=compact_at,
        capacity_source="test_metadata",
        capacity_state="ready",
        limit_kind="combined",
    )
    return agent.PreparationInputs(
        complete_messages=tuple(messages),
        raw_messages=tuple(raw if raw is not None else messages[1:]),
        canonical_tools=tuple(tools),
        policy=policy,
        mode=mode,
        model_ref=model_ref,
        provider_id="openai",
        checkpoint_revision="rev-1",
        prompt_fingerprint="prompt",
        tool_fingerprint="tools",
        policy_fingerprint="policy",
    )


def test_prepared_input_counts_final_messages_and_canonical_tools_once(monkeypatch):
    captured = []

    def fake_count(messages, **kwargs):
        captured.append((list(messages), kwargs))
        return 321

    monkeypatch.setattr(agent, "count_tokens_approximately", fake_count)
    tools = ({"type": "function", "function": {"name": "lookup"}},)
    inputs = _inputs(
        [SystemMessage(content="system"), HumanMessage(content="hello")],
        tools=tools,
    )

    prepared = agent._prepare_model_input(
        inputs.raw_messages,
        inputs,
        None,
        mode="agent",
    )

    assert prepared.usage.estimated_input_tokens == 321
    assert len(captured) == 1
    assert captured[0][0] == list(inputs.complete_messages)
    assert captured[0][1] == {
        "tools": list(tools),
        "tokens_per_image": 1600,
        "use_usage_metadata_scaling": False,
    }


def test_mode_tagged_summary_replaces_only_complete_aged_groups():
    first_ai = AIMessage(
        content="",
        tool_calls=[{"id": "call-1", "name": "lookup", "args": {"q": "old"}}],
    )
    raw = [
        HumanMessage(content="old request"),
        first_ai,
        ToolMessage(content="secret old body", name="lookup", tool_call_id="call-1"),
        HumanMessage(content="recent request"),
        AIMessage(content="recent answer"),
        HumanMessage(content="newest request"),
    ]
    inputs = _inputs([SystemMessage(content="system"), *raw], raw=raw)
    summary = {
        "schema_version": 1,
        "mode": "agent",
        "model_ref": inputs.model_ref,
        "prompt_fingerprint": inputs.prompt_fingerprint,
        "tool_fingerprint": inputs.tool_fingerprint,
        "policy_fingerprint": inputs.policy_fingerprint,
        "boundary_message_count": 3,
        "summary": "## Current Goal\nContinue safely",
    }

    prepared = agent._prepare_model_input(raw, inputs, summary, mode="agent")
    text = "\n".join(str(message.content) for message in prepared.messages)

    assert "HISTORICAL_CONTEXT" in text
    assert "old request" not in text
    assert "secret old body" not in text
    assert "recent request" in text
    assert "newest request" in text
    assert isinstance(prepared.messages[-1], HumanMessage)


def test_summary_never_crosses_agent_and_chat_only_modes():
    raw = [HumanMessage(content="raw user context"), AIMessage(content="raw answer")]
    inputs = _inputs(
        [SystemMessage(content="chat system"), *raw],
        raw=raw,
        mode="chat_only",
    )
    agent_summary = {
        "schema_version": 1,
        "mode": "agent",
        "boundary_message_count": 2,
        "summary": "PRIVATE AGENT TOOL DETAILS",
    }

    prepared = agent._prepare_model_input(raw, inputs, agent_summary, mode="chat_only")
    text = "\n".join(str(message.content) for message in prepared.messages)

    assert "PRIVATE AGENT TOOL DETAILS" not in text
    assert "raw user context" in text


def test_agent_hook_runs_shared_preparation_on_every_model_loop(monkeypatch):
    calls = []
    inputs = _inputs([SystemMessage(content="system"), HumanMessage(content="turn")])

    monkeypatch.setattr(
        agent,
        "_collect_agent_preparation_inputs",
        lambda state, config=None: calls.append(list(state["messages"])) or inputs,
    )
    monkeypatch.setattr(
        agent,
        "_prepare_with_compaction",
        lambda value: agent._prepare_model_input(value.raw_messages, value, None, mode="agent"),
    )

    state = {"messages": [HumanMessage(content="turn")]}
    agent._pre_model_trim(state)
    agent._pre_model_trim(state)

    assert len(calls) == 2


def test_compaction_failure_below_usable_continues_once_with_intact_input(monkeypatch):
    messages = [SystemMessage(content="system"), HumanMessage(content="x" * 800)]
    inputs = _inputs(messages, effective=1_000, usable=900, compact_at=100, model_ref="model:test:below")
    agent._COMPACTION_FAILURES.clear()
    monkeypatch.setattr(agent, "_validated_summary_for_inputs", lambda value: None)
    monkeypatch.setattr(
        agent,
        "_choose_compaction_boundary",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("summarizer offline")),
    )
    monkeypatch.setattr(agent, "_emit_context_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(agent, "_persist_context_usage", lambda *args, **kwargs: None)

    prepared = agent._prepare_with_compaction(inputs)

    assert prepared.messages == messages
    assert prepared.usage.status == "failed"


def test_compaction_failure_above_usable_aborts_without_middle_trim(monkeypatch):
    messages = [SystemMessage(content="system"), HumanMessage(content="x" * 4_000)]
    inputs = _inputs(messages, effective=1_000, usable=120, compact_at=100, model_ref="model:test:above")
    agent._COMPACTION_FAILURES.clear()
    monkeypatch.setattr(agent, "_validated_summary_for_inputs", lambda value: None)
    monkeypatch.setattr(
        agent,
        "_choose_compaction_boundary",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("summarizer offline")),
    )
    monkeypatch.setattr(agent, "_emit_context_event", lambda *args, **kwargs: None)

    with pytest.raises(agent.ContextCompactionError, match="larger-context model"):
        agent._prepare_with_compaction(inputs)


def test_approximately_40k_fixed_agent_envelope_fails_at_32k_and_fits_at_64k(monkeypatch):
    messages = [SystemMessage(content="fixed agent prompt"), HumanMessage(content="run the task")]
    tools = ({"type": "function", "function": {"name": "large_tool_schema"}},)
    at_32k = _inputs(
        messages,
        raw=messages[1:],
        effective=32_768,
        usable=int(32_768 * 0.85),
        compact_at=int(32_768 * 0.75),
        model_ref="model:ollama:agent-32k",
        tools=tools,
    )
    at_64k = _inputs(
        messages,
        raw=messages[1:],
        effective=65_536,
        usable=int(65_536 * 0.85),
        compact_at=int(65_536 * 0.75),
        model_ref="model:ollama:agent-64k",
        tools=tools,
    )
    monkeypatch.setattr(agent, "_count_prepared_tokens", lambda *args, **kwargs: 40_000)
    emitted = []
    recorded = []
    monkeypatch.setattr(agent, "_emit_context_event", lambda *args: emitted.append(args))
    monkeypatch.setattr(
        agent,
        "_record_compaction_event",
        lambda *args, **kwargs: recorded.append(kwargs)
        or {"payload": {"display_copy": kwargs.get("display_copy", "")}},
    )

    with pytest.raises(agent.ContextCompactionError) as exc_info:
        agent._prepare_with_compaction(at_32k)

    message = str(exc_info.value)
    assert "fixed prompt and tool schemas require an estimated 40,000 input tokens" in message
    assert "selected 32,768-token context provides 27,852 usable input tokens" in message
    assert "Reduce enabled tools, increase the context setting" in message
    assert recorded[0]["display_copy"] == message
    assert emitted[0][0] == "compaction_failed"
    assert emitted[0][1]["display_copy"] == message

    prepared = agent._prepare_with_compaction(at_64k)
    assert prepared.usage.estimated_input_tokens == 40_000
    assert prepared.usage.status == "ready"


def test_reduced_agent_envelope_can_fit_explicit_32k_context(monkeypatch):
    messages = [SystemMessage(content="reduced prompt"), HumanMessage(content="hello")]
    inputs = _inputs(
        messages,
        raw=messages[1:],
        effective=32_768,
        usable=int(32_768 * 0.85),
        compact_at=int(32_768 * 0.75),
        model_ref="model:ollama:reduced-tools",
        tools=(),
    )
    monkeypatch.setattr(agent, "_count_prepared_tokens", lambda *args, **kwargs: 10_000)
    monkeypatch.setattr(agent, "_emit_context_event", lambda *args, **kwargs: None)

    prepared = agent._prepare_with_compaction(inputs)

    assert prepared.usage.estimated_input_tokens == 10_000
    assert prepared.usage.status == "ready"


def test_context_overflow_classifier_is_terminal_and_specific():
    assert agent._is_context_overflow_error("context_length_exceeded")
    assert agent._is_context_overflow_error("prompt is too long for maximum context")
    assert not agent._is_context_overflow_error("temporary gateway timeout")
