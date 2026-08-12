from types import SimpleNamespace
import logging
import pytest


def test_chat_only_prompt_is_compact_and_guards_against_imagined_tasks():
    from row_bot.prompts import get_chat_only_system_prompt

    prompt = get_chat_only_system_prompt()

    assert "Do not answer an imagined or unrelated task" in prompt
    assert "If the user only greets you" in prompt
    assert "tools are not available here" in prompt
    assert "access to tools" not in prompt
    assert "Developer Studio" not in prompt
    assert "Designer Studio" not in prompt
    assert "background workflows" not in prompt
    assert "long-term memory requires Agent Mode" in prompt


def _chat_ready_result():
    from row_bot.providers.models import TransportMode
    from row_bot.providers.readiness import ChatReadinessResult

    return ChatReadinessResult(
        ready=True,
        provider_id="custom_openai_lab",
        model_id="local-chat",
        runtime_model="local-chat",
        selection_ref="model:custom_openai_lab:local-chat",
        transport=TransportMode.OPENAI_CHAT,
        context_window=32768,
        credential_status="configured",
    )


def _chat_unknown_capacity_result():
    from row_bot.providers.models import TransportMode
    from row_bot.providers.readiness import ChatReadinessResult, CONTEXT_CAPACITY_ACTION

    return ChatReadinessResult(
        ready=False,
        provider_id="openai",
        model_id="opaque-model",
        runtime_model="opaque-model",
        selection_ref="model:openai:opaque-model",
        transport=TransportMode.OPENAI_CHAT,
        context_window=None,
        credential_status="configured",
        errors=["context window could not be determined"],
        actions=[CONTEXT_CAPACITY_ACTION],
    )


def test_build_chat_only_messages_for_fresh_thread_has_no_hidden_history(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    import row_bot.agent as agent
    import row_bot.threads as threads

    monkeypatch.setattr(threads, "get_latest_checkpoint_messages", lambda thread_id: [])

    messages = agent._build_chat_only_messages("fresh-thread", "hello", context_window=32768)

    assert [message.type for message in messages] == ["system", "human"]
    assert messages[-1].content == "hello"
    assert "Do not answer an imagined or unrelated task" in messages[0].content


def test_chat_only_history_marks_prior_tools_without_tool_bodies():
    import row_bot.agent as agent

    content = agent._chat_only_content_from_ui_message({
        "content": "Earlier answer",
        "tool_results": [
            {
                "name": "row_bot_status",
                "content": "SECRET_STATUS_BODY with current model and enabled tools",
            }
        ],
    })

    assert "Earlier Agent Mode turn used tool(s):" in content
    assert "- row_bot_status" in content
    assert "SECRET_STATUS_BODY" not in content
    assert "enabled tools" not in content


def test_stream_chat_only_streams_and_persists_without_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from langchain_core.messages import AIMessageChunk
    import row_bot.agent as agent
    import row_bot.providers.readiness as readiness
    import row_bot.threads as threads

    class FakeLLM:
        def stream(self, messages):
            self.messages = messages
            yield AIMessageChunk(content="", additional_kwargs={"reasoning_content": "thinking"})
            yield AIMessageChunk(content="hello")
            yield AIMessageChunk(content=" world")

    persisted = []
    monkeypatch.setattr(readiness, "evaluate_chat_readiness", lambda model_label: _chat_ready_result())
    monkeypatch.setattr(agent, "_chat_only_llm", lambda model_label: FakeLLM())
    monkeypatch.setattr(threads, "get_latest_checkpoint_messages", lambda thread_id: [])
    monkeypatch.setattr(threads, "append_checkpoint_messages", lambda thread_id, messages: persisted.extend(messages) or True)

    events = list(agent.stream_chat_only(
        "hi",
        {"configurable": {"thread_id": "thread-chat", "model_override": "model:custom_openai_lab:local-chat"}},
    ))

    assert ("thinking_token", "thinking") in events
    assert [payload for event_type, payload in events if event_type == "done"] == ["hello world"]
    assert [getattr(message, "type", "") for message in persisted] == ["human", "ai"]
    assert getattr(persisted[-1], "content", "") == "hello world"


def test_stream_chat_only_unknown_auto_blocks_before_model_or_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    import row_bot.agent as agent
    import row_bot.providers.readiness as readiness
    import row_bot.threads as threads

    calls = {"model": 0, "checkpoint": 0}
    monkeypatch.setattr(
        readiness,
        "evaluate_chat_readiness",
        lambda model_label: _chat_unknown_capacity_result(),
    )
    monkeypatch.setattr(
        agent,
        "_chat_only_llm",
        lambda model_label: calls.__setitem__("model", calls["model"] + 1),
    )
    monkeypatch.setattr(
        threads,
        "append_checkpoint_messages",
        lambda *args, **kwargs: calls.__setitem__("checkpoint", calls["checkpoint"] + 1),
    )

    events = list(agent.stream_chat_only(
        "do not persist me",
        {"configurable": {"thread_id": "thread-blocked", "model_override": "model:openai:opaque-model"}},
    ))

    assert calls == {"model": 0, "checkpoint": 0}
    assert len(events) == 1 and events[0][0] == "error"
    assert "Refresh the provider catalog" in events[0][1]
    assert "Advanced context override" in events[0][1]
    assert "known-capacity model" in events[0][1]


def test_stream_chat_only_reasoning_only_returns_error_without_persisting(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from langchain_core.messages import AIMessageChunk
    import row_bot.agent as agent
    import row_bot.providers.readiness as readiness
    import row_bot.threads as threads

    class FakeLLM:
        def stream(self, messages):
            yield AIMessageChunk(content="", additional_kwargs={"reasoning_content": "thinking only"})

    persisted = []
    monkeypatch.setattr(readiness, "evaluate_chat_readiness", lambda model_label: _chat_ready_result())
    monkeypatch.setattr(agent, "_chat_only_llm", lambda model_label: FakeLLM())
    monkeypatch.setattr(threads, "get_latest_checkpoint_messages", lambda thread_id: [])
    monkeypatch.setattr(threads, "append_checkpoint_messages", lambda thread_id, messages: persisted.extend(messages) or True)

    events = list(agent.stream_chat_only(
        "hi",
        {"configurable": {"thread_id": "thread-chat", "model_override": "model:custom_openai_lab:local-chat"}},
    ))

    assert ("thinking_token", "thinking only") in events
    errors = [payload for event_type, payload in events if event_type == "error"]
    assert errors == ["The model returned reasoning but no final answer. Try again or switch models."]
    assert persisted == []


def test_chat_only_context_overflow_is_never_replayed(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    import row_bot.agent as agent
    import row_bot.providers.readiness as readiness
    import row_bot.threads as threads

    class OverflowLLM:
        def __init__(self):
            self.stream_calls = 0
            self.invoke_calls = 0

        def stream(self, messages):
            self.stream_calls += 1
            if False:
                yield None
            raise ValueError("context_length_exceeded")

        def invoke(self, messages):
            self.invoke_calls += 1
            raise AssertionError("overflow must not be replayed")

    llm = OverflowLLM()
    marked = []
    monkeypatch.setattr(readiness, "evaluate_chat_readiness", lambda model_label: _chat_ready_result())
    monkeypatch.setattr(agent, "_chat_only_llm", lambda model_label: llm)
    monkeypatch.setattr(agent, "_mark_context_overflow", lambda inputs: marked.append(inputs.model_ref))
    monkeypatch.setattr(threads, "get_latest_checkpoint_messages", lambda thread_id: [])

    events = list(agent.stream_chat_only(
        "hi",
        {
            "configurable": {
                "thread_id": "thread-chat",
                "model_override": "model:custom_openai_lab:local-chat",
            }
        },
    ))

    assert llm.stream_calls == 1
    assert llm.invoke_calls == 0
    assert marked == ["model:custom_openai_lab:local-chat"]
    assert len([payload for kind, payload in events if kind == "error"]) == 1


def test_stream_chat_only_llm_creation_error_names_selected_model(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    import row_bot.agent as agent
    import row_bot.providers.readiness as readiness
    import row_bot.threads as threads

    selected = "model:ollama:vendor/non-tool-chat:14b"
    monkeypatch.setattr(readiness, "evaluate_chat_readiness", lambda model_label: _chat_ready_result())
    monkeypatch.setattr(agent, "_chat_only_llm", lambda model_label: (_ for _ in ()).throw(ValueError("status code: 400: does not support tools")))
    monkeypatch.setattr(threads, "get_latest_checkpoint_messages", lambda thread_id: [])

    events = list(agent.stream_chat_only(
        "hi",
        {"configurable": {"thread_id": "thread-chat", "model_override": selected}},
    ))

    errors = [payload for event_type, payload in events if event_type == "error"]
    assert errors
    assert "vendor/non-tool-chat:14b" in errors[0]
    assert "agent-default:cloud" not in errors[0]


def test_friendly_api_error_does_not_call_generic_400_tool_error(monkeypatch):
    import row_bot.agent as agent

    message = agent._friendly_api_error("status code: 400: bad request", "model:ollama:local-chat:14b")

    assert "does not support tool calling" not in message
    assert "API error" in message


def test_stream_agent_auto_routes_visible_chat_only_without_graph(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    import row_bot.agent as agent
    import row_bot.providers.readiness as readiness

    def _boom(*args, **kwargs):
        raise AssertionError("Agent graph should not be constructed for Chat Only routing")

    monkeypatch.setattr(agent, "get_agent_graph", _boom)
    monkeypatch.setattr(agent, "stream_chat_only", lambda *args, **kwargs: iter([("token", "chat"), ("done", "chat")]))
    captured = {}

    def _readiness(model_label, **kwargs):
        captured["probe_ollama_tools"] = kwargs.get("probe_ollama_tools")
        return SimpleNamespace(selected_mode="chat_only", selection_reason="chat ready")

    monkeypatch.setattr(readiness, "evaluate_runtime_readiness", _readiness)

    events = list(agent.stream_agent(
        "hi",
        [],
        {"configurable": {"thread_id": "thread-chat", "runtime_surface": "normal_chat", "runtime_mode": "auto"}},
    ))

    assert events == [("token", "chat"), ("done", "chat")]
    assert captured["probe_ollama_tools"] is False


def test_stream_agent_unknown_auto_blocks_before_graph_creation(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    import row_bot.agent as agent
    import row_bot.providers.readiness as readiness

    calls = {"graph": 0}
    message = (
        "Context limit is unknown. Refresh the provider catalog, set an Advanced "
        "context override, or choose a known-capacity model."
    )
    monkeypatch.setattr(
        readiness,
        "evaluate_runtime_readiness",
        lambda *args, **kwargs: SimpleNamespace(
            selected_mode="blocked",
            selection_reason=message,
            agent=SimpleNamespace(context_window=None),
            chat=SimpleNamespace(context_window=None),
        ),
    )
    monkeypatch.setattr(
        agent,
        "get_agent_graph",
        lambda *args, **kwargs: calls.__setitem__("graph", calls["graph"] + 1),
    )

    events = list(agent.stream_agent(
        "do not send",
        [],
        {
            "configurable": {
                "thread_id": "thread-blocked",
                "runtime_surface": "normal_chat",
                "runtime_mode": "auto",
                "model_override": "model:openai:opaque-model",
            }
        },
    ))

    assert calls["graph"] == 0
    assert events == [("error", message)]


def test_resume_stream_agent_surfaces_actionable_capacity_error(monkeypatch):
    import row_bot.agent as agent
    from row_bot.providers.models import TransportMode
    from row_bot.providers.readiness import (
        AgentCompatibilityError,
        AgentReadinessResult,
        CONTEXT_CAPACITY_ACTION,
    )

    result = AgentReadinessResult(
        ready=False,
        provider_id="openai",
        model_id="opaque-model",
        runtime_model="opaque-model",
        selection_ref="model:openai:opaque-model",
        transport=TransportMode.OPENAI_CHAT,
        context_window=None,
        errors=["context window could not be determined"],
        actions=[CONTEXT_CAPACITY_ACTION],
    )
    monkeypatch.setattr(
        agent,
        "get_agent_graph",
        lambda *args, **kwargs: (_ for _ in ()).throw(AgentCompatibilityError(result)),
    )

    events = list(agent.resume_stream_agent(
        [],
        {"configurable": {"thread_id": "thread-resume", "model_override": result.selection_ref}},
        True,
    ))

    assert len(events) == 1 and events[0][0] == "error"
    assert "Refresh the provider catalog" in events[0][1]
    assert "Advanced context override" in events[0][1]
    assert "known-capacity model" in events[0][1]


def test_stream_agent_logs_resolved_runtime_decision(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    import row_bot.agent as agent
    import row_bot.providers.readiness as readiness

    monkeypatch.setattr(
        readiness,
        "evaluate_runtime_readiness",
        lambda model_label, **kwargs: SimpleNamespace(
            selected_mode="agent",
            selection_reason="agent ready",
            agent=SimpleNamespace(context_window=65536),
            chat=SimpleNamespace(context_window=65536),
        ),
    )
    monkeypatch.setattr(agent, "get_agent_graph", lambda *args, **kwargs: object())
    monkeypatch.setattr(agent, "_stream_graph", lambda *args, **kwargs: iter([("done", "agent")]))

    with caplog.at_level(logging.INFO, logger="row_bot.agent"):
        events = list(agent.stream_agent(
            "hi",
            ["row_bot_status"],
            {"configurable": {"thread_id": "thread-chat", "runtime_surface": "normal_chat", "runtime_mode": "auto"}},
        ))

    assert events == [("done", "agent")]
    runtime_logs = [record.message for record in caplog.records if "runtime decision:" in record.message]
    assert runtime_logs
    assert "requested=auto" in runtime_logs[-1]
    assert "selected=agent" in runtime_logs[-1]
    assert "tools_enabled=1" in runtime_logs[-1]
    assert "tools_bound=True" in runtime_logs[-1]


def test_row_bot_status_model_reports_effective_runtime(monkeypatch):
    import row_bot.agent as agent
    import row_bot.models as models
    import row_bot.providers.readiness as readiness
    import row_bot.providers.resolution as resolution
    import row_bot.tools.row_bot_status_tool as row_bot_status_tool

    override_token = models._active_model_override.set("model:ollama:local-chat:14b")
    agent._set_active_runtime_context(
        thread_id="thread-chat",
        runtime_surface="normal_chat",
        requested_runtime_mode="auto",
        selected_runtime_mode="chat_only",
        runtime_reason="chat model",
        model_override="model:ollama:local-chat:14b",
        enabled_tool_names=(),
    )
    monkeypatch.setattr(models, "get_current_model", lambda: "model:ollama:qwen3.6:27b")
    monkeypatch.setattr(models, "get_context_size", lambda model=None: 32768)
    monkeypatch.setattr(models, "get_provider_emoji", lambda model: "model")
    monkeypatch.setattr(models, "get_user_context_size", lambda: 32768)
    monkeypatch.setattr(models, "get_cloud_context_size", lambda: 131072)
    monkeypatch.setattr(
        resolution,
        "resolve_provider_config",
        lambda *args, **kwargs: SimpleNamespace(
            runtime_model="local-chat:14b",
            provider_id="ollama",
            provider_display_name="Ollama Local",
            execution_location="local",
            risk_label="local_private",
        ),
    )
    monkeypatch.setattr(
        readiness,
        "evaluate_runtime_readiness",
        lambda *args, **kwargs: SimpleNamespace(
            selected_mode="chat_only",
            selection_reason="chat ready",
        ),
    )

    try:
        output = row_bot_status_tool._query_model()
    finally:
        models._active_model_override.reset(override_token)

    assert "model:ollama:local-chat:14b" in output
    assert "Runtime model: local-chat:14b" in output
    assert "Readiness: Chat Only - tools and actions are off (chat ready)" in output
    assert "Active turn runtime: Chat Only - tools and actions are off, requested auto on normal_chat" in output
    assert "Override active (global default: model:ollama:qwen3.6:27b)" in output


def test_row_bot_status_model_labels_endpoint_types(monkeypatch):
    import row_bot.agent as agent
    import row_bot.models as models
    import row_bot.providers.readiness as readiness
    import row_bot.providers.resolution as resolution
    import row_bot.tools.row_bot_status_tool as row_bot_status_tool

    cases = [
        ("model:ollama:qwen", "ollama", "Ollama Local", "local", "local_private", "Local (Ollama)"),
        ("model:custom_openai_lm-studio:qwen", "custom_openai_lm-studio", "LM Studio", "local", "local_private", "Local custom endpoint"),
        ("model:custom_openai_proxy:qwen", "custom_openai_proxy", "Proxy", "remote", "custom_endpoint", "Custom endpoint"),
        ("model:openai:gpt-5", "openai", "OpenAI", "remote", "api_key", "Provider (OpenAI)"),
    ]

    monkeypatch.setattr(models, "get_context_size", lambda model=None: 32768)
    monkeypatch.setattr(models, "get_provider_emoji", lambda model: "model")
    monkeypatch.setattr(models, "get_user_context_size", lambda: 32768)
    monkeypatch.setattr(models, "get_cloud_context_size", lambda: 131072)
    monkeypatch.setattr(
        readiness,
        "evaluate_runtime_readiness",
        lambda *args, **kwargs: SimpleNamespace(selected_mode="agent", selection_reason="ready"),
    )
    agent._set_active_runtime_context(thread_id="", runtime_surface="", requested_runtime_mode="", selected_runtime_mode="")

    for model, provider_id, provider_label, execution_location, risk_label, expected_type in cases:
        monkeypatch.setattr(models, "get_current_model", lambda model=model: model)
        monkeypatch.setattr(
            resolution,
            "resolve_provider_config",
            lambda *args, provider_id=provider_id, provider_label=provider_label, execution_location=execution_location, risk_label=risk_label, **kwargs: SimpleNamespace(
                runtime_model="runtime",
                provider_id=provider_id,
                provider_display_name=provider_label,
                execution_location=execution_location,
                risk_label=risk_label,
            ),
        )

        output = row_bot_status_tool._query_model()

        assert f"Type: {expected_type}" in output


def test_ollama_parameter_schema_error_is_agent_mode_failure():
    import row_bot.agent as agent

    message = "expected element type <function> but have <parameter> (status code: -1)"

    assert agent._tool_support_error(message) is True
    assert "does not support tool calling" in agent._friendly_api_error(
        message,
        "model:ollama:qwen3.6:27b",
    )


def test_stream_agent_auto_does_not_silently_fallback_on_tool_schema_error(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    import row_bot.agent as agent
    import row_bot.providers.readiness as readiness

    message = "expected element type <function> but have <parameter> (status code: -1)"
    monkeypatch.setattr(
        readiness,
        "evaluate_runtime_readiness",
        lambda model_label, **kwargs: SimpleNamespace(selected_mode="agent", selection_reason="agent ready"),
    )
    monkeypatch.setattr(agent, "get_agent_graph", lambda *args, **kwargs: object())
    monkeypatch.setattr(agent, "_stream_graph", lambda *args, **kwargs: iter([("error", message)]))
    monkeypatch.setattr(agent, "stream_chat_only", lambda *args, **kwargs: iter([("token", "chat"), ("done", "chat")]))

    events = list(agent.stream_agent(
        "hi",
        [],
        {"configurable": {"thread_id": "thread-chat", "runtime_surface": "normal_chat", "runtime_mode": "auto"}},
    ))

    assert events == [("error", message)]


def test_agent_runtime_context_overrides_stale_chat_only_claims():
    import row_bot.agent as agent

    context = agent._agent_runtime_system_context()

    assert "Agent Mode is active" in context
    assert "Do not claim this turn is Chat Only" in context
    assert "long-term memory" in context


def test_agent_graph_uses_provider_qualified_override(monkeypatch):
    import row_bot.agent as agent

    captured = {}
    def _ready(model_label):
        captured["ready"] = model_label
        return SimpleNamespace(
            provider_id="ollama",
            runtime_model="vendor/non-tool-chat:14b",
            capability_source="test",
            confidence="high",
        )

    def _llm(model_label):
        captured["llm"] = model_label
        return object()

    monkeypatch.setattr(agent, "get_current_model", lambda: "model:ollama:qwen3:14b")
    monkeypatch.setattr(agent, "_ensure_agent_mode_ready", _ready)
    monkeypatch.setattr(agent, "get_llm_for", _llm)
    monkeypatch.setattr(agent, "get_context_size", lambda model_label=None: 32768)
    monkeypatch.setattr(agent, "create_react_agent", lambda **kwargs: {"model": kwargs["model"], "tools": kwargs["tools"]})
    monkeypatch.setattr(agent.tool_registry, "get_tool", lambda name: None)
    monkeypatch.setattr(agent, "get_agent_system_prompt", lambda: "system")
    agent._agent_cache.clear()

    graph = agent.get_agent_graph([], model_override="model:ollama:vendor/non-tool-chat:14b")

    assert graph
    assert captured["ready"] == "model:ollama:vendor/non-tool-chat:14b"
    assert captured["llm"] == "model:ollama:vendor/non-tool-chat:14b"


def test_stream_agent_forced_workflow_uses_agent_path(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    import row_bot.agent as agent

    monkeypatch.setattr(agent, "get_agent_graph", lambda *args, **kwargs: object())
    monkeypatch.setattr(agent, "_stream_graph", lambda *args, **kwargs: iter([("done", "agent")]))
    monkeypatch.setattr(
        agent,
        "stream_chat_only",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("workflow must not route Chat Only")),
    )

    events = list(agent.stream_agent(
        "run workflow",
        [],
        {"configurable": {"thread_id": "workflow-thread", "runtime_surface": "workflow", "runtime_mode": "agent"}},
    ))

    assert events == [("done", "agent")]


def test_stream_agent_ignores_caller_recursion_limit_and_derives_internal_ceiling(monkeypatch):
    import row_bot.agent as agent

    captured = {}

    class FakeGraph:
        def stream(self, input_data, config=None, stream_mode=None):
            captured["recursion_limit"] = config["recursion_limit"]
            assert config["recursion_limit"] > 0
            return iter([])

        def get_state(self, config):
            return None

    monkeypatch.setattr(agent, "get_agent_graph", lambda *args, **kwargs: FakeGraph())

    events = list(agent.stream_agent(
        "hi",
        [],
        {
            "configurable": {
                "thread_id": "thread-agent",
                "runtime_surface": "workflow",
                "runtime_mode": "agent",
            },
            "recursion_limit": "50",
        },
    ))

    assert events == [("done", "")]
    assert captured["recursion_limit"] == agent.framework_recursion_limit(90)
    assert isinstance(captured["recursion_limit"], int)


@pytest.mark.parametrize("tool_name", ["row_bot_status", "analyze_image"])
def test_stream_graph_finalizes_reasoning_only_after_successful_tool_result(monkeypatch, tool_name):
    import row_bot.agent as agent
    from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

    tool_call = {"name": tool_name, "args": {}, "id": "call_1", "type": "tool_call"}
    tool_result = ToolMessage(content="Tool says the answer is 42.", name=tool_name, tool_call_id="call_1")
    state_messages = [
        HumanMessage(content="use the tool"),
        AIMessage(content="", tool_calls=[tool_call]),
        tool_result,
        AIMessage(content="", additional_kwargs={"reasoning_content": "I should summarize the tool result."}),
    ]
    persisted = []
    final_messages = {}

    class FakeState:
        next = []
        tasks = []
        values = {"messages": state_messages}

    class FakeGraph:
        def stream(self, input_data, config=None, stream_mode=None):
            yield ("updates", {"tools": {"messages": [tool_result]}})
            yield (
                "messages",
                (
                    AIMessageChunk(content="", additional_kwargs={"reasoning_content": "I should summarize the tool result."}),
                    {"langgraph_node": "agent"},
                ),
            )

        def get_state(self, config):
            return FakeState()

        def update_state(self, config, update):
            persisted.extend(update["messages"])

    class FakeFinalLLM:
        def stream(self, messages):
            final_messages["messages"] = messages
            yield AIMessageChunk(content="", additional_kwargs={"reasoning_content": "repair reasoning"})
            yield AIMessageChunk(content="The answer is 42.")

    monkeypatch.setattr(agent, "_chat_only_llm", lambda model_label: FakeFinalLLM())

    events = list(agent._stream_graph(
        FakeGraph(),
        {"messages": [("human", "use the tool")]},
        {"configurable": {"thread_id": "thread-agent", "model_override": "model:custom_openai_lab:local"}},
    ))

    assert ("thinking_token", "I should summarize the tool result.") in events
    assert ("thinking_token", "repair reasoning") in events
    assert ("token", "The answer is 42.") in events
    assert events[-1] == ("done", "The answer is 42.")
    assert persisted[-1].content == "The answer is 42."
    assert persisted[-1].additional_kwargs["reasoning_content"] == "repair reasoning"
    assert [message.type for message in final_messages["messages"]] == ["system", "human"]
    assert tool_name in final_messages["messages"][1].content


def test_stream_graph_finalization_failure_preserves_tool_result_without_fake_answer(monkeypatch):
    import row_bot.agent as agent
    from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

    tool_result = ToolMessage(content="Tool says the answer is 42.", name="row_bot_status", tool_call_id="call_1")
    state_messages = [
        HumanMessage(content="use the tool"),
        AIMessage(content="", tool_calls=[{"name": "row_bot_status", "args": {}, "id": "call_1", "type": "tool_call"}]),
        tool_result,
        AIMessage(content="", additional_kwargs={"reasoning_content": "I should summarize the tool result."}),
    ]
    persisted = []

    class FakeState:
        next = []
        tasks = []
        values = {"messages": state_messages}

    class FakeGraph:
        def stream(self, input_data, config=None, stream_mode=None):
            yield ("updates", {"tools": {"messages": [tool_result]}})
            yield (
                "messages",
                (
                    AIMessageChunk(content="", additional_kwargs={"reasoning_content": "I should summarize the tool result."}),
                    {"langgraph_node": "agent"},
                ),
            )

        def get_state(self, config):
            return FakeState()

        def update_state(self, config, update):
            persisted.extend(update["messages"])

    class FakeFinalLLM:
        def stream(self, messages):
            yield AIMessageChunk(content="", additional_kwargs={"reasoning_content": "still only reasoning"})

    monkeypatch.setattr(agent, "_chat_only_llm", lambda model_label: FakeFinalLLM())

    events = list(agent._stream_graph(
        FakeGraph(),
        {"messages": [("human", "use the tool")]},
        {"configurable": {"thread_id": "thread-agent", "model_override": "model:custom_openai_lab:local"}},
    ))

    tool_done = next(payload for event_type, payload in events if event_type == "tool_done")
    assert tool_done["raw_name"] == "row_bot_status"
    assert tool_done["content"] == "Tool says the answer is 42."
    assert ("thinking_token", "still only reasoning") in events
    assert events[-1] == ("error", "The model returned reasoning but no final answer. Try again or switch models.")
    assert persisted == []


def test_stream_graph_finalizes_whitespace_only_answer_after_successful_tool_result(monkeypatch):
    import row_bot.agent as agent
    from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

    tool_result = ToolMessage(
        content="heatmap_regional_sales.png\nmarketing_content_design.png\nnotes.md",
        name="workspace_list_directory",
        tool_call_id="call_1",
    )
    state_messages = [
        HumanMessage(content="what image files do i have in my workspace?"),
        AIMessage(content="", tool_calls=[{
            "name": "workspace_list_directory",
            "args": {"dir_path": "."},
            "id": "call_1",
            "type": "tool_call",
        }]),
        tool_result,
        AIMessage(content="", additional_kwargs={"reasoning_content": "I should list only image files."}),
    ]
    persisted = []

    class FakeState:
        next = []
        tasks = []
        values = {"messages": state_messages}

    class FakeGraph:
        def stream(self, input_data, config=None, stream_mode=None):
            yield ("updates", {"tools": {"messages": [tool_result]}})
            yield (
                "messages",
                (
                    AIMessageChunk(content="\n\n", additional_kwargs={"reasoning_content": "I should list only image files."}),
                    {"langgraph_node": "agent"},
                ),
            )

        def get_state(self, config):
            return FakeState()

        def update_state(self, config, update):
            persisted.extend(update["messages"])

    class FakeFinalLLM:
        def stream(self, messages):
            yield AIMessageChunk(content="", additional_kwargs={"reasoning_content": "repair reasoning"})
            yield AIMessageChunk(content="Image files include heatmap_regional_sales.png and marketing_content_design.png.")

    monkeypatch.setattr(agent, "_chat_only_llm", lambda model_label: FakeFinalLLM())

    events = list(agent._stream_graph(
        FakeGraph(),
        {"messages": [("human", "what image files do i have in my workspace?")]},
        {"configurable": {"thread_id": "thread-agent", "model_override": "model:custom_openai_lab:local"}},
    ))

    assert ("token", "\n\n") not in events
    assert ("thinking_token", "repair reasoning") in events
    assert events[-1] == ("done", "Image files include heatmap_regional_sales.png and marketing_content_design.png.")
    assert persisted[-1].content == "Image files include heatmap_regional_sales.png and marketing_content_design.png."


def test_stream_graph_logs_provider_call_segments_around_tool_result(caplog):
    import row_bot.agent as agent
    from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

    tool_call = {"name": "row_bot_status", "args": {}, "id": "call_1", "type": "tool_call"}
    tool_result = ToolMessage(content="Status is green.", name="row_bot_status", tool_call_id="call_1")
    state_messages = [
        HumanMessage(content="check status"),
        AIMessage(content="", tool_calls=[tool_call]),
        tool_result,
        AIMessage(content="Everything is green."),
    ]

    class FakeState:
        next = []
        tasks = []
        values = {"messages": state_messages}

    class FakeGraph:
        def stream(self, input_data, config=None, stream_mode=None):
            yield ("updates", {"agent": {"messages": [AIMessage(content="", tool_calls=[tool_call])]}})
            yield ("updates", {"tools": {"messages": [tool_result]}})
            yield (
                "messages",
                (
                    AIMessageChunk(content="Everything is green."),
                    {"langgraph_node": "agent", "run_id": "provider-call-2"},
                ),
            )

        def get_state(self, config):
            return FakeState()

    with caplog.at_level(logging.INFO, logger="row_bot.agent"):
        events = list(agent._stream_graph(
            FakeGraph(),
            {"messages": [("human", "check status")]},
            {
                "configurable": {
                    "thread_id": "thread-agent",
                    "model_override": "model:custom_openai_lab:local",
                    "generation_id": "thread-agent:gen-1",
                }
            },
        ))

    assert any(event_type == "tool_call" and "Row-Bot Status" in payload for event_type, payload in events)
    assert events[-1] == ("done", "Everything is green.")
    completion_logs = [
        record.getMessage()
        for record in caplog.records
        if "stream completion diagnostics=" in record.getMessage()
    ]
    assert completion_logs
    message = completion_logs[-1]
    assert "'generation.provider_call_count': 2" in message
    assert "'generation.provider_calls':" in message
    assert "'end_reason': 'tool_result'" in message
    assert "'end_reason': 'complete'" in message
    assert "'tool_gap_ms':" in message
    assert "'first_answer_token_ms':" in message


def test_forced_agent_surfaces_are_wired_in_callers():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    ui_streaming = (root / "src" / "row_bot" / "ui" / "streaming.py").read_text(encoding="utf-8")
    tasks = (root / "src" / "row_bot" / "tasks.py").read_text(encoding="utf-8")
    approval = (root / "src" / "row_bot" / "channels" / "approval.py").read_text(encoding="utf-8")
    task_dialog = (root / "src" / "row_bot" / "ui" / "task_dialog.py").read_text(encoding="utf-8")

    assert '"runtime_surface": runtime_surface' in ui_streaming
    assert '"runtime_mode": "agent"' in ui_streaming
    assert "_agent_ready_forced_surface" in ui_streaming
    assert 'append_checkpoint_messages(gen.thread_id' in ui_streaming
    assert 'model_override=configurable.get("model_override")' in ui_streaming
    assert "get_agent_graph()" not in ui_streaming
    assert '"runtime_surface": "workflow"' in tasks
    assert '"runtime_mode": "agent"' in tasks
    assert '"runtime_surface": "approval"' in approval
    assert '"runtime_mode": "agent"' in approval
    assert "evaluate_agent_readiness(cur_model_ov)" in task_dialog
    assert "Workflows require Agent Mode" in task_dialog
