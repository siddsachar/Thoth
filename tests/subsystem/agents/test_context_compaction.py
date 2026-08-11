from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

import row_bot.agent as agent


pytestmark = pytest.mark.subsystem


def _inputs(raw, *, mode: str = "agent") -> agent.PreparationInputs:
    policy = SimpleNamespace(
        native_limit_tokens=5_334,
        requested_limit_tokens=None,
        effective_limit_tokens=5_334,
        usable_input_tokens=4_533,
        compact_at_tokens=4_000,
        capacity_source="test",
        capacity_state="ready",
        limit_kind="combined",
    )
    return agent.PreparationInputs(
        complete_messages=(SystemMessage(content="system"), *raw),
        raw_messages=tuple(raw),
        canonical_tools=(),
        policy=policy,
        mode=mode,
        model_ref="model:openai:test",
        provider_id="openai",
        checkpoint_revision="rev-1",
        prompt_fingerprint="prompt",
        tool_fingerprint="tools",
        policy_fingerprint="policy",
    )


def _structured_summary() -> str:
    return "\n".join(
        (
            "## Current Goal",
            "Continue the task.",
            "## Constraints and Decisions",
            "Keep recent turns intact.",
            "## Completed Work",
            "Older work was summarized.",
            "## Current State",
            "Ready.",
            "## Relevant Details",
            "No hidden ranges.",
            "## Next Step",
            "Continue.",
        )
    )


def test_summary_prompt_requires_exact_user_designated_opaque_identifiers() -> None:
    required = (
        "Copy exact opaque identifiers verbatim when the user marks them as important "
        "or needed later"
    )

    assert required in agent._SUMMARY_SYSTEM_PROMPT
    assert "Never transform, infer, or invent them." in agent._SUMMARY_SYSTEM_PROMPT


def test_ollama_compactor_binds_output_limit_in_supported_options(monkeypatch):
    captured = {}

    class FakeOllama:
        num_ctx = 32_768
        reasoning = True

        def bind(self, **kwargs):
            captured.update(kwargs)
            return self

    monkeypatch.setattr(agent, "_chat_only_llm", lambda _model_ref: FakeOllama())

    bound = agent._compaction_model(
        replace(_inputs([]), provider_id="ollama"),
        512,
    )

    assert isinstance(bound, FakeOllama)
    assert captured == {
        "options": {"num_predict": 512, "num_ctx": 32_768},
        "reasoning": False,
    }


def test_fake_summary_preserves_designated_identifier_in_prepared_input(monkeypatch):
    opaque_id = "QA-ROWBOT-7f9c2a11"
    raw = []
    for index in range(5):
        content = (
            f"Preserve {opaque_id} exactly; it is needed later."
            if index == 0
            else f"user-{index}"
        )
        raw.extend((HumanMessage(content=content), AIMessage(content=f"answer-{index}")))
    inputs = _inputs(raw)
    summary = _structured_summary().replace("No hidden ranges.", f"Opaque ID: {opaque_id}")

    agent._COMPACTION_FAILURES.clear()
    monkeypatch.setattr(agent, "count_tokens_approximately", _group_weight_count)
    monkeypatch.setattr(agent, "_validated_summary_for_inputs", lambda _inputs: None)
    monkeypatch.setattr(agent, "_invoke_compaction_model", lambda *args, **kwargs: summary)
    monkeypatch.setattr(agent, "_emit_context_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(agent, "_persist_context_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr(agent, "_record_compaction_event", lambda *args, **kwargs: {"id": 1, "payload": {}})
    monkeypatch.setattr("row_bot.threads.context_boundary_digest", lambda *args, **kwargs: "a" * 64)
    monkeypatch.setattr("row_bot.threads.save_summary_state_cas", lambda *args, **kwargs: True)
    token = agent._current_thread_id_var.set("thread-opaque")
    try:
        prepared = agent._prepare_with_compaction(inputs)
    finally:
        agent._current_thread_id_var.reset(token)

    historical = [
        str(message.content)
        for message in prepared.messages
        if "<HISTORICAL_CONTEXT" in str(getattr(message, "content", ""))
    ]
    assert historical
    assert opaque_id in historical[0]


def _group_weight_count(messages, tools=(), **_kwargs) -> int:
    total = len(tools) * 50
    for message in messages:
        content = str(getattr(message, "content", "") or "")
        if isinstance(message, SystemMessage):
            continue
        if "HISTORICAL_CONTEXT" in content:
            total += 100
        elif "ROLE=" in content or content:
            total += 500
    return total


def test_successful_compaction_preserves_two_atomic_groups_and_emits_one_event(monkeypatch):
    raw = []
    for index in range(5):
        raw.extend(
            (
                HumanMessage(content=f"user-{index}"),
                AIMessage(content=f"answer-{index}"),
            )
        )
    inputs = _inputs(raw)
    saved = []
    emitted = []

    agent._COMPACTION_FAILURES.clear()
    monkeypatch.setattr(agent, "count_tokens_approximately", _group_weight_count)
    monkeypatch.setattr(agent, "_validated_summary_for_inputs", lambda _inputs: None)
    monkeypatch.setattr(agent, "_invoke_compaction_model", lambda *args, **kwargs: _structured_summary())
    monkeypatch.setattr(agent, "_emit_context_event", lambda kind, payload: emitted.append((kind, payload)))
    monkeypatch.setattr(agent, "_persist_context_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr(agent, "_record_compaction_event", lambda *args, **kwargs: {"id": 9, "payload": {"display_copy": "Context compacted"}})
    monkeypatch.setattr(
        "row_bot.threads.context_boundary_digest",
        lambda *args, **kwargs: "a" * 64,
    )
    monkeypatch.setattr(
        "row_bot.threads.save_summary_state_cas",
        lambda thread_id, state, expected_revision: saved.append(dict(state)) or True,
    )
    token = agent._current_thread_id_var.set("thread-1")
    try:
        prepared = agent._prepare_with_compaction(inputs)
    finally:
        agent._current_thread_id_var.reset(token)

    assert prepared.usage.estimated_input_tokens < inputs.policy.compact_at_tokens
    assert len(saved) == 1
    assert saved[0]["boundary_message_count"] == 6
    assert [message.content for message in prepared.messages[-4:]] == [
        "user-3",
        "answer-3",
        "user-4",
        "answer-4",
    ]
    successes = [payload for kind, payload in emitted if kind == "compaction_succeeded"]
    assert len(successes) == 1
    assert successes[0]["event_id"] == 9


def test_rolling_summary_covers_exact_new_middle_range(monkeypatch):
    raw = []
    for index in range(5):
        raw.extend((HumanMessage(content=f"user-{index}"), AIMessage(content=f"answer-{index}")))
    inputs = _inputs(raw)
    previous = {
        "schema_version": 1,
        "mode": "agent",
        "boundary_message_count": 2,
        "summary": _structured_summary(),
    }
    calls = []

    monkeypatch.setattr(agent, "count_tokens_approximately", _group_weight_count)

    def fake_invoke(_inputs, *, prior_summary, transcript, output_tokens):
        calls.append((prior_summary, transcript, output_tokens))
        return _structured_summary()

    monkeypatch.setattr(agent, "_invoke_compaction_model", fake_invoke)
    summary = agent._summarize_aged_range(
        inputs,
        previous_state=previous,
        boundary=6,
        compactable_tokens=3_000,
    )

    assert summary == _structured_summary()
    combined = "\n".join(call[1] for call in calls)
    assert calls[0][0] == previous["summary"]
    assert "user-1" in combined and "user-2" in combined
    assert "user-0" not in combined and "user-3" not in combined


def test_chat_only_compaction_projection_never_contains_tool_body():
    raw = (
        HumanMessage(content="look this up"),
        AIMessage(
            content="",
            tool_calls=[{"id": "call-1", "name": "lookup", "args": {"q": "safe"}}],
        ),
        ToolMessage(
            content="TOP SECRET TOOL BODY",
            name="lookup",
            tool_call_id="call-1",
        ),
        AIMessage(content="The lookup completed."),
    )

    projected = agent._chat_only_projected_messages(raw)
    text = "\n".join(str(message.content) for message in projected)

    assert "TOP SECRET TOOL BODY" not in text
    assert "lookup" in text
    assert all(not isinstance(message, ToolMessage) for message in projected)


@pytest.mark.parametrize(
    ("provider_id", "metadata", "expected"),
    (
        ("openai", {"token_usage": {"prompt_tokens": 123}}, 123),
        ("google", {"usage_metadata": {"prompt_token_count": 456}}, 456),
        ("ollama", {"prompt_eval_count": 789}, 789),
        (
            "anthropic",
            {
                "usage": {
                    "input_tokens": 100,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 30,
                }
            },
            150,
        ),
    ),
)
def test_provider_input_usage_is_diagnostic_only(provider_id, metadata, expected):
    assert agent._normalized_confirmed_input_tokens(metadata, provider_id) == expected


def test_agent_provider_overflow_is_terminal_without_graph_replay(monkeypatch):
    class OverflowGraph:
        def __init__(self) -> None:
            self.calls = 0

        def stream(self, *args, **kwargs):
            self.calls += 1
            if False:
                yield None
            raise ValueError("maximum context length exceeded")

    graph = OverflowGraph()
    marked = []
    monkeypatch.setattr(
        "row_bot.threads.repair_thread_checkpoint_versions",
        lambda thread_id: None,
    )
    monkeypatch.setattr(agent, "_mark_context_overflow", lambda inputs: marked.append(inputs))

    events = list(agent._stream_graph(
        graph,
        {"messages": [HumanMessage(content="hello")]},
        {"configurable": {"thread_id": "thread-1"}},
    ))

    assert graph.calls == 1
    assert len([payload for kind, payload in events if kind == "error"]) == 1
    assert len(marked) == 1


def test_stop_during_compaction_discards_result_before_cas(monkeypatch):
    import threading

    raw = []
    for index in range(5):
        raw.extend((HumanMessage(content=f"user-{index}"), AIMessage(content=f"answer-{index}")))
    inputs = _inputs(raw)
    stop_event = threading.Event()
    stop_event.set()

    agent._COMPACTION_FAILURES.clear()
    monkeypatch.setattr(agent, "count_tokens_approximately", _group_weight_count)
    monkeypatch.setattr(agent, "_validated_summary_for_inputs", lambda _inputs: None)
    monkeypatch.setattr(agent, "_emit_context_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(agent, "_persist_context_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "row_bot.threads.save_summary_state_cas",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("CAS must not run")),
    )
    thread_token = agent._current_thread_id_var.set("thread-stop")
    stop_token = agent._current_stop_event_var.set(stop_event)
    try:
        with pytest.raises(agent.TaskStoppedError):
            agent._prepare_with_compaction(inputs)
    finally:
        agent._current_stop_event_var.reset(stop_token)
        agent._current_thread_id_var.reset(thread_token)
