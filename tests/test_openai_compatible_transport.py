import json
import threading

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from row_bot.cancellation import CancellationScope, use_cancellation_scope
from row_bot.providers.custom import custom_endpoint_profile
from row_bot.providers.reasoning import ReasoningCapabilities, ReasoningRequestPlan, ReasoningSelection
from row_bot.providers.tool_protocol import format_validation_retry_result
from row_bot.providers.transports.openai_compatible import ChatOpenAICompatible


class _Response:
    status_code = 200
    text = "{}"

    def __init__(self, payload=None):
        self._payload = payload or {"choices": [{"message": {"content": "hello"}}]}

    def json(self):
        return self._payload


class _Client:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response()


class _StreamResponse:
    status_code = 200
    text = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def iter_lines(self):
        yield b'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}'
        yield b'data: {"choices":[{"delta":{"content":"answer"}}]}'
        yield b"data: [DONE]"


class _StreamingClient(_Client):
    def stream(self, method, url, **kwargs):
        self.calls.append((url, kwargs))
        return _StreamResponse()


class _FailingStreamResponse:
    status_code = 200
    text = ""

    def __init__(self, exc: Exception, *lines: bytes):
        self.exc = exc
        self.lines = lines

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def iter_lines(self):
        yield from self.lines
        raise self.exc


class _RetryingStreamingClient(_Client):
    def __init__(self, exc: Exception, *first_lines: bytes, always_fail: bool = False):
        super().__init__()
        self.exc = exc
        self.first_lines = first_lines
        self.always_fail = always_fail

    def stream(self, method, url, **kwargs):
        self.calls.append((url, kwargs))
        if len(self.calls) == 1 or self.always_fail:
            return _FailingStreamResponse(self.exc, *self.first_lines)
        return _StreamResponse()


class _TextStreamResponse:
    status_code = 200
    text = ""

    def __init__(self, *parts: str):
        self.parts = parts

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def iter_lines(self):
        for part in self.parts:
            yield ("data: " + json.dumps({"choices": [{"delta": {"content": part}}]})).encode("utf-8")
        yield b"data: [DONE]"


class _TextStreamingClient(_Client):
    def __init__(self, *parts: str):
        super().__init__()
        self.parts = parts

    def stream(self, method, url, **kwargs):
        self.calls.append((url, kwargs))
        return _TextStreamResponse(*self.parts)


class _EmptyStreamResponse:
    status_code = 200
    text = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def iter_lines(self):
        yield b"data: [DONE]"


class _EmptyStreamingClient(_Client):
    def stream(self, method, url, **kwargs):
        self.calls.append((url, kwargs))
        return _EmptyStreamResponse()


class _ToolStreamResponse:
    status_code = 200
    text = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def iter_lines(self):
        yield b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"lookup"}}]}}]}'
        yield b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"q\\":"}}]}}]}'
        yield b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"x\\"}"}}]}}]}'
        yield b"data: [DONE]"


class _ToolStreamingClient(_Client):
    def stream(self, method, url, **kwargs):
        self.calls.append((url, kwargs))
        return _ToolStreamResponse()


class _InterleavedToolStreamResponse:
    status_code = 200
    text = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def iter_lines(self):
        yield b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_a","type":"function","function":{"name":"first","arguments":"{\\"a\\":"}},{"index":1,"id":"call_b","type":"function","function":{"name":"second","arguments":"{\\"b\\":"}}]}}]}'
        yield b'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"function":{"arguments":"2}"}},{"index":0,"function":{"arguments":"1}"}}]}}]}'
        yield b"data: [DONE]"


class _InterleavedToolStreamingClient(_Client):
    def stream(self, method, url, **kwargs):
        self.calls.append((url, kwargs))
        return _InterleavedToolStreamResponse()


class _MalformedToolStreamResponse:
    status_code = 200
    text = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def iter_lines(self):
        yield b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"category\\":\\"overview\\"}"}}]}}]}'
        yield b"data: [DONE]"


class _MalformedToolStreamingClient(_Client):
    def stream(self, method, url, **kwargs):
        self.calls.append((url, kwargs))
        return _MalformedToolStreamResponse()


class _NativeAndReasoningToolStreamResponse:
    status_code = 200
    text = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def iter_lines(self):
        yield b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"lookup","arguments":"{}"}}]}}]}'
        yield b'data: {"choices":[{"delta":{"reasoning_content":"<tool_call><function=row_bot_status><parameter=category>tools</parameter></function></tool_call>"}}]}'
        yield b"data: [DONE]"


class _NativeAndReasoningToolStreamingClient(_Client):
    def stream(self, method, url, **kwargs):
        self.calls.append((url, kwargs))
        return _NativeAndReasoningToolStreamResponse()


class _ReasoningTextToolStreamResponse:
    status_code = 200
    text = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def iter_lines(self):
        yield b'data: {"choices":[{"delta":{"reasoning_content":"I should retry.\\n<tool_call>\\n<function=row_bot_status>\\n<parameter=category>\\ntools\\n</parameter>\\n</function>\\n</tool_call>"}}]}'
        yield b"data: [DONE]"


class _ReasoningTextToolStreamingClient(_Client):
    def stream(self, method, url, **kwargs):
        self.calls.append((url, kwargs))
        return _ReasoningTextToolStreamResponse()


class _UnknownReasoningTextToolStreamResponse:
    status_code = 200
    text = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def iter_lines(self):
        yield b'data: {"choices":[{"delta":{"reasoning_content":"<tool_call><function=unknown_tool><parameter=value>x</parameter></function></tool_call>"}}]}'
        yield b"data: [DONE]"


class _UnknownReasoningTextToolStreamingClient(_Client):
    def stream(self, method, url, **kwargs):
        self.calls.append((url, kwargs))
        return _UnknownReasoningTextToolStreamResponse()


class _ReasoningOnlyStreamResponse:
    status_code = 200
    text = ""

    def __init__(self, reasoning: str):
        self.reasoning = reasoning

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def iter_lines(self):
        payload = {
            "choices": [{
                "delta": {
                    "reasoning_content": self.reasoning,
                }
            }]
        }
        yield f"data: {json.dumps(payload)}".encode("utf-8")
        yield b"data: [DONE]"


class _ReasoningOnlyStreamingClient(_Client):
    def __init__(self, reasoning: str):
        super().__init__()
        self.reasoning = reasoning

    def stream(self, method, url, **kwargs):
        self.calls.append((url, kwargs))
        return _ReasoningOnlyStreamResponse(self.reasoning)


class _UnreadErrorStreamResponse:
    status_code = 400

    @property
    def text(self):
        raise RuntimeError("Attempted to access streaming response content")

    def json(self):
        raise RuntimeError("response body has not been read")

    def read(self):
        return b'{"error":{"message":"prompt exceeds context window"}}'

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def iter_lines(self):
        return iter(())


class _UnreadErrorStreamingClient(_Client):
    def stream(self, method, url, **kwargs):
        self.calls.append((url, kwargs))
        return _UnreadErrorStreamResponse()


class _AnthropicTextStreamResponse:
    status_code = 200
    text = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def iter_lines(self):
        yield b'data: {"type":"message_start","message":{"id":"msg_1","type":"message"}}'
        yield b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}'
        yield b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hel"}}'
        yield b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"lo"}}'
        yield b'data: {"type":"content_block_stop","index":0}'
        yield b'data: {"type":"message_stop"}'


class _AnthropicTextStreamingClient(_Client):
    def stream(self, method, url, **kwargs):
        self.calls.append((url, kwargs))
        return _AnthropicTextStreamResponse()


class _AnthropicToolStreamResponse:
    status_code = 200
    text = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def iter_lines(self):
        yield b'data: {"type":"message_start","message":{"id":"msg_1","type":"message"}}'
        yield b'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu_1","name":"lookup","input":{}}}'
        yield b'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"q\\":"}}'
        yield b'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"\\"x\\"}"}}'
        yield b'data: {"type":"content_block_stop","index":1}'
        yield b'data: {"type":"message_delta","delta":{"stop_reason":"tool_use","stop_sequence":null}}'
        yield b'data: {"type":"message_stop"}'


class _AnthropicToolStreamingClient(_Client):
    def stream(self, method, url, **kwargs):
        self.calls.append((url, kwargs))
        return _AnthropicToolStreamResponse()


class _AnthropicEmptyStreamResponse:
    status_code = 200
    text = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def iter_lines(self):
        yield b'data: {"type":"message_start","message":{"id":"msg_1","type":"message"}}'
        yield b'data: {"type":"message_stop"}'


class _AnthropicEmptyStreamingClient(_Client):
    def stream(self, method, url, **kwargs):
        self.calls.append((url, kwargs))
        return _AnthropicEmptyStreamResponse()


class _ReasoningResponse(_Response):
    def __init__(self):
        super().__init__({
            "choices": [{
                "message": {
                    "content": "hello",
                    "reasoning_content": "thinking",
                },
            }],
        })


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (None, 900.0),
        ("720", 720.0),
        ("not-a-timeout", 900.0),
    ],
)
def test_openai_compatible_transport_uses_granular_configurable_read_timeout(
    monkeypatch,
    configured,
    expected,
):
    if configured is None:
        monkeypatch.delenv("ROW_BOT_OPENAI_COMPATIBLE_READ_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("ROW_BOT_OPENAI_COMPATIBLE_READ_TIMEOUT", configured)
    client = _Client()
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:8000/v1",
        http_client=client,
    )

    model.invoke([HumanMessage(content="hi")])

    timeout = client.calls[0][1]["timeout"]
    assert model.timeout == expected
    assert timeout.connect == 10.0
    assert timeout.read == expected
    assert timeout.write == 120.0
    assert timeout.pool == 10.0


def test_openai_compatible_transport_moves_system_messages_first():
    client = _Client()
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:8000/v1",
        endpoint={"system_message_mode": "system_first", "message_content_mode": "string_text"},
        http_client=client,
    )

    model.invoke([HumanMessage(content="hi"), SystemMessage(content="rules")])

    body = client.calls[0][1]["json"]
    assert body["messages"] == [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "hi"},
    ]


def test_openai_compatible_string_text_keeps_text_only_content_as_string():
    client = _Client()
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={"message_content_mode": "string_text"},
        http_client=client,
    )

    model.invoke([HumanMessage(content=[{"type": "text", "text": "hello"}, {"type": "text", "text": " world"}])])

    body = client.calls[0][1]["json"]
    assert body["messages"][0]["content"] == "hello world"


def test_openai_compatible_string_text_preserves_multimodal_content():
    client = _Client()
    data_url = "data:image/png;base64,abc123"
    model = ChatOpenAICompatible(
        model_name="qwen-vl",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={"message_content_mode": "string_text"},
        http_client=client,
    )

    model.invoke([HumanMessage(content=[
        {"type": "text", "text": "describe"},
        {"type": "image_url", "image_url": data_url},
    ])])

    content = client.calls[0][1]["json"]["messages"][0]["content"]
    assert content == [
        {"type": "text", "text": "describe"},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]


@pytest.mark.parametrize("profile", ["lmstudio", "llama_cpp"])
def test_openai_compatible_profile_preserves_image_blocks(profile):
    client = _Client()
    endpoint = custom_endpoint_profile(profile)
    endpoint["provider_id"] = f"custom_openai_{profile}"
    model = ChatOpenAICompatible(
        model_name="qwen-vl",
        base_url="http://127.0.0.1:1234/v1",
        endpoint=endpoint,
        http_client=client,
    )

    model.invoke([HumanMessage(content=[
        {"type": "text", "text": "read this"},
        {"type": "input_image", "image": {"url": "data:image/jpeg;base64,abc123"}},
    ])])

    content = client.calls[0][1]["json"]["messages"][0]["content"]
    assert isinstance(content, list)
    assert content[1] == {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,abc123"}}


def test_openai_compatible_generic_string_text_custom_endpoint_preserves_images():
    client = _Client()
    model = ChatOpenAICompatible(
        model_name="generic-vl",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={"provider_id": "custom_openai_generic", "message_content_mode": "string_text"},
        http_client=client,
    )

    model.invoke([HumanMessage(content=[
        {"type": "text", "text": "what is this?"},
        {"type": "input_image", "image_url": "data:image/png;base64,abc123"},
    ])])

    content = client.calls[0][1]["json"]["messages"][0]["content"]
    assert content == [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc123"}},
    ]


def test_openai_compatible_multimodal_logging_does_not_emit_base64(caplog):
    client = _StreamingClient()
    secret_b64 = "abc123SECRETBASE64"
    model = ChatOpenAICompatible(
        model_name="qwen-vl",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={"provider_id": "custom_openai_lm-studio", "message_content_mode": "string_text"},
        http_client=client,
    )

    list(model.stream([HumanMessage(content=[
        {"type": "text", "text": "describe"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{secret_b64}"}},
    ])]))

    assert secret_b64 not in caplog.text


def test_openai_compatible_transport_consolidates_multiple_system_messages():
    client = _Client()
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:8080/v1",
        endpoint={"system_message_mode": "system_first", "message_content_mode": "string_text"},
        http_client=client,
    )

    model.invoke([
        SystemMessage(content="Root rules"),
        HumanMessage(content="hi"),
        SystemMessage(content="Late context"),
        HumanMessage(content="continue"),
    ])

    body = client.calls[0][1]["json"]
    assert body["messages"] == [
        {"role": "system", "content": "Root rules\n\nLate context"},
        {"role": "user", "content": "hi"},
        {"role": "user", "content": "continue"},
    ]


def test_openai_compatible_transport_keeps_native_tool_history_for_custom_profiles():
    client = _Client()
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:8000/v1",
        endpoint={
            "system_message_mode": "system_first",
            "message_content_mode": "string_text",
            "tool_history_mode": "native_required",
            "drop_unsupported_params": True,
        },
        http_client=client,
    )

    model.invoke([
        HumanMessage(content="hi"),
        AIMessage(content="", tool_calls=[{"name": "lookup", "args": {"q": "x"}, "id": "call_1"}]),
        ToolMessage(content="result", tool_call_id="call_1"),
    ], tools=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}])

    body = client.calls[0][1]["json"]
    assert "tools" in body
    assert body["messages"][1]["tool_calls"][0]["function"]["name"] == "lookup"
    assert body["messages"][-1] == {"role": "tool", "content": "result", "tool_call_id": "call_1"}


def test_atlascloud_anthropic_flattens_prior_tool_history_but_keeps_current_tools():
    client = _Client()
    model = ChatOpenAICompatible(
        model_name="anthropic/claude-opus-4.8",
        base_url="https://api.atlascloud.ai/v1",
        endpoint={"provider_id": "atlascloud", "profile": "atlascloud"},
        http_client=client,
    )

    model.invoke(
        [
            HumanMessage(content="hi"),
            AIMessage(content="", tool_calls=[{"name": "lookup", "args": {"q": "x"}, "id": "call_1"}]),
            ToolMessage(content="result", name="lookup", tool_call_id="call_1"),
            HumanMessage(content="continue"),
        ],
        tools=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
    )

    body = client.calls[0][1]["json"]
    assert "tools" in body
    assert body["tools"][0]["function"]["name"] == "lookup"
    assert "tool_calls" not in body["messages"][1]
    assert body["messages"][1] == {"role": "assistant", "content": ""}
    assert body["messages"][2] == {"role": "user", "content": "[Tool result from lookup]: result"}


def test_atlascloud_non_anthropic_preserves_native_tool_history():
    client = _Client()
    model = ChatOpenAICompatible(
        model_name="openai/gpt-5.5",
        base_url="https://api.atlascloud.ai/v1",
        endpoint={"provider_id": "atlascloud", "profile": "atlascloud"},
        http_client=client,
    )

    model.invoke(
        [
            HumanMessage(content="hi"),
            AIMessage(content="", tool_calls=[{"name": "lookup", "args": {"q": "x"}, "id": "call_1"}]),
            ToolMessage(content="result", name="lookup", tool_call_id="call_1"),
        ],
        tools=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
    )

    body = client.calls[0][1]["json"]
    assert body["messages"][1]["tool_calls"][0]["function"]["name"] == "lookup"
    assert body["messages"][2] == {"role": "tool", "content": "result", "tool_call_id": "call_1"}


def test_openai_compatible_transport_applies_runtime_context_param(monkeypatch):
    monkeypatch.setattr("row_bot.models.get_context_size", lambda model_name=None: 32_768)
    client = _Client()
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:8080/v1",
        endpoint={
            "provider_id": "custom_openai_llamacpp",
            "supports_runtime_context_override": True,
            "context_param_name": "n_ctx",
        },
        http_client=client,
    )

    model.invoke([HumanMessage(content="hi")])

    body = client.calls[0][1]["json"]
    assert body["n_ctx"] == 32_768


def test_openai_compatible_transport_preserves_reasoning_content():
    client = _Client()
    client.post = lambda url, **kwargs: _Response({
        "choices": [{
            "message": {
                "content": "hello",
                "reasoning_content": "thinking",
            },
        }],
    })
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={"provider_id": "custom_openai_lm-studio"},
        http_client=client,
    )

    result = model.invoke([HumanMessage(content="hi")])

    assert result.content == "hello"
    assert result.additional_kwargs["reasoning_content"] == "thinking"


def test_openai_compatible_transport_replays_reasoning_only_when_endpoint_allows():
    client = _Client()
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={"provider_id": "custom_openai_lm-studio", "supports_reasoning_replay": True},
        http_client=client,
    )

    model.invoke([
        AIMessage(content="", additional_kwargs={"reasoning_content": "native reasoning"}),
        HumanMessage(content="continue"),
    ])

    assistant = client.calls[0][1]["json"]["messages"][0]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == ""
    assert assistant["reasoning_content"] == "native reasoning"


def test_openai_compatible_transport_does_not_replay_reasoning_when_endpoint_disallows():
    client = _Client()
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={"provider_id": "custom_openai_generic", "supports_reasoning_replay": False},
        http_client=client,
    )

    model.invoke([
        AIMessage(content="hello", additional_kwargs={"reasoning_content": "native reasoning"}),
        HumanMessage(content="continue"),
    ])

    assistant = client.calls[0][1]["json"]["messages"][0]
    assert "reasoning_content" not in assistant


def test_openai_compatible_transport_reasoning_off_sets_template_flag():
    client = _Client()
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={
            "provider_id": "custom_openai_llamacpp",
            "profile": "llama_cpp",
            "reasoning_mode": "off",
            "extra_body": {"chat_template_kwargs": {"foo": "bar"}, "reasoning": True},
        },
        http_client=client,
    )

    model.invoke([HumanMessage(content="hi")])

    body = client.calls[0][1]["json"]
    assert body["chat_template_kwargs"] == {"foo": "bar", "enable_thinking": False}
    assert "reasoning" not in body


def test_openai_compatible_transport_streams_reasoning_chunks():
    client = _StreamingClient()
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={"provider_id": "custom_openai_lm-studio"},
        http_client=client,
    )

    chunks = list(model.stream([HumanMessage(content="hi")]))

    assert chunks[0].additional_kwargs["reasoning_content"] == "thinking"
    assert chunks[1].content == "answer"


@pytest.mark.parametrize(
    "transport_error",
    [
        httpx.ReadTimeout("read timed out"),
        httpx.RemoteProtocolError("peer closed the stream"),
    ],
)
def test_openai_compatible_stream_retries_transport_failure_before_first_event(transport_error):
    client = _RetryingStreamingClient(transport_error)
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={"provider_id": "custom_openai_lm-studio"},
        http_client=client,
    )

    events = list(model._iter_stream_events({"stream": True, "messages": []}))

    assert len(client.calls) == 2
    assert [event["choices"][0]["delta"] for event in events] == [
        {"reasoning_content": "thinking"},
        {"content": "answer"},
    ]


def test_openai_compatible_stream_does_not_retry_after_first_event():
    first_event = b'data: {"choices":[{"delta":{"content":"partial"}}]}'
    client = _RetryingStreamingClient(httpx.ReadTimeout("read timed out"), first_event)
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={"provider_id": "custom_openai_lm-studio"},
        http_client=client,
    )
    events = model._iter_stream_events({"stream": True, "messages": []})

    assert next(events)["choices"][0]["delta"] == {"content": "partial"}
    with pytest.raises(httpx.ReadTimeout):
        next(events)
    assert len(client.calls) == 1


def test_openai_compatible_stream_retry_is_bounded_to_two_attempts():
    client = _RetryingStreamingClient(httpx.ReadTimeout("read timed out"), always_fail=True)
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={"provider_id": "custom_openai_lm-studio"},
        http_client=client,
    )

    with pytest.raises(httpx.ReadTimeout):
        list(model._iter_stream_events({"stream": True, "messages": []}))

    assert len(client.calls) == 2


def test_atlascloud_streams_visible_content_after_tool_result_with_tools_bound():
    client = _TextStreamingClient("final ", "answer")
    model = ChatOpenAICompatible(
        model_name="openai/gpt-5.5",
        base_url="https://api.atlascloud.ai/v1",
        endpoint={"provider_id": "atlascloud", "profile": "atlascloud"},
        http_client=client,
    )

    chunks = list(model.stream(
        [
            HumanMessage(content="what tools?"),
            AIMessage(content="", tool_calls=[{"name": "lookup", "args": {"q": "x"}, "id": "call_1"}]),
            ToolMessage(content="result", name="lookup", tool_call_id="call_1"),
        ],
        tools=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
    ))

    visible = [str(chunk.content or "") for chunk in chunks if chunk.content]
    assert visible == ["final ", "answer"]
    assert client.calls[0][1]["json"]["stream"] is True
    assert len(client.calls) == 1


def test_custom_openai_transport_still_buffers_content_after_tool_result_with_tools_bound():
    client = _TextStreamingClient("final ", "answer")
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={
            "provider_id": "custom_openai_tools",
            "last_probe": {"streaming_ok": True, "tool_calling": True, "streaming_tool_calling": True},
        },
        http_client=client,
    )

    chunks = list(model.stream(
        [
            HumanMessage(content="what tools?"),
            AIMessage(content="", tool_calls=[{"name": "lookup", "args": {"q": "x"}, "id": "call_1"}]),
            ToolMessage(content="result", name="lookup", tool_call_id="call_1"),
        ],
        tools=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
    ))

    visible = [str(chunk.content or "") for chunk in chunks if chunk.content]
    assert visible == ["final answer"]
    assert client.calls[0][1]["json"]["stream"] is True
    assert len(client.calls) == 1


def test_atlascloud_anthropic_text_stream_is_parsed():
    client = _AnthropicTextStreamingClient()
    model = ChatOpenAICompatible(
        model_name="anthropic/claude-opus-4.8",
        base_url="https://api.atlascloud.ai/v1",
        endpoint={"provider_id": "atlascloud", "profile": "atlascloud"},
        http_client=client,
    )

    chunks = list(model.stream([HumanMessage(content="hi")]))

    assert [str(chunk.content or "") for chunk in chunks if chunk.content] == ["hel", "lo"]
    assert len(client.calls) == 1


def test_atlascloud_anthropic_tool_stream_is_parsed():
    client = _AnthropicToolStreamingClient()
    model = ChatOpenAICompatible(
        model_name="anthropic/claude-opus-4.8",
        base_url="https://api.atlascloud.ai/v1",
        endpoint={"provider_id": "atlascloud", "profile": "atlascloud"},
        http_client=client,
    )

    chunks = list(model.stream(
        [HumanMessage(content="lookup")],
        tools=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
    ))

    tool_chunks = [chunk.tool_call_chunks[0] for chunk in chunks if chunk.tool_call_chunks]
    assert tool_chunks == [{
        "name": "lookup",
        "args": '{"q":"x"}',
        "id": "toolu_1",
        "index": 1,
        "type": "tool_call_chunk",
    }]
    assert len(client.calls) == 1


def test_atlascloud_anthropic_empty_stream_does_not_retry_non_stream():
    client = _AnthropicEmptyStreamingClient()
    model = ChatOpenAICompatible(
        model_name="anthropic/claude-opus-4.8",
        base_url="https://api.atlascloud.ai/v1",
        endpoint={"provider_id": "atlascloud", "profile": "atlascloud"},
        http_client=client,
    )

    with pytest.raises(RuntimeError, match="Skipped non-stream fallback"):
        list(model.stream([HumanMessage(content="hi")]))

    assert len(client.calls) == 1


def test_openai_compatible_transport_falls_back_when_probe_marks_streaming_bad():
    client = _Client()
    client.post = lambda url, **kwargs: client.calls.append((url, kwargs)) or _ReasoningResponse()
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={
            "provider_id": "custom_openai_lm-studio",
            "last_probe": {"streaming_ok": False},
        },
        http_client=client,
    )

    chunks = list(model.stream([HumanMessage(content="hi")]))

    body = client.calls[0][1]["json"]
    assert body["stream"] is False
    assert chunks[0].additional_kwargs["reasoning_content"] == "thinking"
    assert chunks[1].content == "hello"


def test_openai_compatible_transport_retries_empty_stream_with_non_stream():
    client = _EmptyStreamingClient()
    client.post = lambda url, **kwargs: client.calls.append((url, kwargs)) or _ReasoningResponse()
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={
            "provider_id": "custom_openai_lm-studio",
            "last_probe": {"streaming_ok": True},
        },
        http_client=client,
    )

    chunks = list(model.stream([HumanMessage(content="hi")]))

    stream_body = client.calls[0][1]["json"]
    fallback_body = client.calls[1][1]["json"]
    assert stream_body["stream"] is True
    assert fallback_body["stream"] is False
    assert chunks[0].additional_kwargs["reasoning_content"] == "thinking"
    assert chunks[1].content == "hello"


def test_openai_compatible_transport_streams_tool_call_chunks():
    client = _ToolStreamingClient()
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={
            "provider_id": "custom_openai_tools",
            "last_probe": {"streaming_ok": True, "tool_calling": True, "streaming_tool_calling": True},
        },
        http_client=client,
    )

    chunks = list(model.stream([HumanMessage(content="weather?")]))

    tool_chunks = [chunk for chunk in chunks if chunk.tool_call_chunks]
    assert tool_chunks[0].tool_call_chunks == [{
        "name": "lookup",
        "args": '{"q":"x"}',
        "id": "call_1",
        "index": 0,
        "type": "tool_call_chunk",
    }]
    assert len(tool_chunks) == 1
    assert "openai_call_0" not in json.dumps(tool_chunks[0].tool_call_chunks)


def test_openai_compatible_transport_assembles_interleaved_tool_call_chunks():
    client = _InterleavedToolStreamingClient()
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={
            "provider_id": "custom_openai_tools",
            "last_probe": {"streaming_ok": True, "tool_calling": True, "streaming_tool_calling": True},
        },
        http_client=client,
    )

    chunks = list(model.stream([HumanMessage(content="two tools")]))

    tool_chunks = [chunk.tool_call_chunks[0] for chunk in chunks if chunk.tool_call_chunks]
    assert tool_chunks == [
        {
            "name": "first",
            "args": '{"a":1}',
            "id": "call_a",
            "index": 0,
            "type": "tool_call_chunk",
        },
        {
            "name": "second",
            "args": '{"b":2}',
            "id": "call_b",
            "index": 1,
            "type": "tool_call_chunk",
        },
    ]


def test_openai_compatible_transport_drops_malformed_argument_only_tool_stream(caplog):
    client = _MalformedToolStreamingClient()
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={
            "provider_id": "custom_openai_tools",
            "last_probe": {"streaming_ok": True, "tool_calling": True, "streaming_tool_calling": True},
        },
        http_client=client,
    )

    chunks = list(model.stream([HumanMessage(content="bad tool stream")]))

    assert [chunk for chunk in chunks if chunk.tool_call_chunks] == []
    assert "dropped streamed tool call without name" in caplog.text


def test_openai_compatible_transport_tool_requests_fallback_when_streaming_tool_probe_false():
    client = _Client()
    client.post = lambda url, **kwargs: client.calls.append((url, kwargs)) or _Response({
        "choices": [{
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{\"q\":\"x\"}"},
                }],
            },
        }],
    })
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={
            "provider_id": "custom_openai_tools",
            "last_probe": {"streaming_ok": True, "tool_calling": True, "streaming_tool_calling": False},
        },
        http_client=client,
    )

    chunks = list(model.stream(
        [HumanMessage(content="lookup")],
        tools=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
    ))

    assert client.calls[0][1]["json"]["stream"] is False
    assert chunks[0].tool_calls[0]["name"] == "lookup"


def test_openai_compatible_transport_tool_requests_fallback_when_streaming_tool_probe_missing():
    client = _Client()
    client.post = lambda url, **kwargs: client.calls.append((url, kwargs)) or _Response({
        "choices": [{
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }],
            },
        }],
    })
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={
            "provider_id": "custom_openai_tools",
            "last_probe": {"streaming_ok": True, "tool_calling": True},
        },
        http_client=client,
    )

    list(model.stream(
        [HumanMessage(content="lookup")],
        tools=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
    ))

    assert client.calls[0][1]["json"]["stream"] is False


def test_openai_compatible_transport_plain_chat_streams_without_streaming_tool_probe():
    client = _StreamingClient()
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={
            "provider_id": "custom_openai_tools",
            "last_probe": {"streaming_ok": True, "tool_calling": True},
        },
        http_client=client,
    )

    chunks = list(model.stream([HumanMessage(content="hi")]))

    assert client.calls[0][1]["json"]["stream"] is True
    assert chunks[1].content == "answer"


def test_openai_compatible_transport_tool_requests_stream_when_streaming_tool_probe_true():
    client = _ToolStreamingClient()
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={
            "provider_id": "custom_openai_tools",
            "last_probe": {"streaming_ok": True, "tool_calling": True, "streaming_tool_calling": True},
        },
        http_client=client,
    )

    list(model.stream(
        [HumanMessage(content="lookup")],
        tools=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
    ))

    assert client.calls[0][1]["json"]["stream"] is True


def test_openai_compatible_transport_recovers_reasoning_text_tool_call():
    client = _ReasoningTextToolStreamingClient()
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={
            "provider_id": "custom_openai_tools",
            "last_probe": {"streaming_ok": True, "tool_calling": True, "streaming_tool_calling": True},
        },
        http_client=client,
    )

    chunks = list(model.stream([HumanMessage(content="what tools?")]))

    assert chunks[0].additional_kwargs["reasoning_content"].startswith("I should retry.")
    tool_chunks = [chunk for chunk in chunks if chunk.tool_call_chunks]
    assert tool_chunks[0].tool_call_chunks == [{
        "name": "row_bot_status",
        "args": '{"category": "tools"}',
        "id": "text_call_0",
        "index": 0,
        "type": "tool_call_chunk",
    }]


def test_openai_compatible_transport_filters_recovered_unknown_tool_when_schemas_supplied():
    client = _UnknownReasoningTextToolStreamingClient()
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={
            "provider_id": "custom_openai_tools",
            "last_probe": {"streaming_ok": True, "tool_calling": True, "streaming_tool_calling": True},
        },
        http_client=client,
    )

    chunks = list(model.stream(
        [HumanMessage(content="what tools?")],
        tools=[{"type": "function", "function": {"name": "row_bot_status", "parameters": {"type": "object"}}}],
    ))

    assert [chunk for chunk in chunks if chunk.tool_call_chunks] == []


def test_openai_compatible_transport_does_not_recover_text_tool_call_when_native_stream_exists():
    client = _NativeAndReasoningToolStreamingClient()
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={
            "provider_id": "custom_openai_tools",
            "last_probe": {"streaming_ok": True, "tool_calling": True, "streaming_tool_calling": True},
        },
        http_client=client,
    )

    chunks = list(model.stream([HumanMessage(content="what tools?")]))

    tool_chunks = [chunk.tool_call_chunks[0] for chunk in chunks if chunk.tool_call_chunks]
    assert [chunk["name"] for chunk in tool_chunks] == ["lookup"]


def test_openai_compatible_transport_keeps_reasoning_only_after_successful_tool_result_private():
    client = _Client()
    client.post = lambda url, **kwargs: _Response({
        "choices": [{
            "message": {
                "content": "",
                "reasoning_content": "Enabled tools: 29. Disabled tools: 3.",
            },
        }],
    })
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={"provider_id": "custom_openai_tools"},
        http_client=client,
    )

    result = model.invoke([
        HumanMessage(content="what tools?"),
        AIMessage(content="", tool_calls=[{"name": "row_bot_status", "args": {"category": "tools"}, "id": "call_1"}]),
        ToolMessage(content="**Tools** (29 enabled, 3 disabled)", name="row_bot_status", tool_call_id="call_1"),
    ])

    assert result.content == ""
    assert result.additional_kwargs["reasoning_content"] == "Enabled tools: 29. Disabled tools: 3."


def test_openai_compatible_transport_stream_keeps_reasoning_only_after_successful_tool_result_private():
    client = _ReasoningOnlyStreamingClient("Enabled tools: 29. Disabled tools: 3.")
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={"provider_id": "custom_openai_tools"},
        http_client=client,
    )

    chunks = list(model.stream([
        HumanMessage(content="what tools?"),
        AIMessage(content="", tool_calls=[{"name": "row_bot_status", "args": {"category": "tools"}, "id": "call_1"}]),
        ToolMessage(content="**Tools** (29 enabled, 3 disabled)", name="row_bot_status", tool_call_id="call_1"),
    ]))

    assert chunks[0].additional_kwargs["reasoning_content"] == "Enabled tools: 29. Disabled tools: 3."
    assert not any(chunk.content == "Enabled tools: 29. Disabled tools: 3." for chunk in chunks)


def test_openai_compatible_transport_keeps_reasoning_only_after_validation_repair_private():
    client = _Client()
    client.post = lambda url, **kwargs: _Response({
        "choices": [{
            "message": {
                "content": "",
                "reasoning_content": "I need to provide a valid category argument.",
            },
        }],
    })
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={"provider_id": "custom_openai_tools", "display_name": "Local Qwen"},
        http_client=client,
    )
    repair = format_validation_retry_result(
        tool_name="row_bot_status",
        detail="missing or invalid required argument: category",
        fields=["category"],
    )

    result = model.invoke([
        HumanMessage(content="what tools?"),
        AIMessage(content="", tool_calls=[{"name": "row_bot_status", "args": {}, "id": "call_1"}]),
        ToolMessage(content=repair, name="row_bot_status", tool_call_id="call_1"),
    ])

    assert result.content == ""
    assert result.additional_kwargs["reasoning_content"] == "I need to provide a valid category argument."


def test_openai_compatible_transport_still_recovers_text_tool_call_after_validation_repair():
    client = _ReasoningTextToolStreamingClient()
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={
            "provider_id": "custom_openai_tools",
            "last_probe": {"streaming_ok": True, "tool_calling": True, "streaming_tool_calling": True},
        },
        http_client=client,
    )
    repair = format_validation_retry_result(
        tool_name="row_bot_status",
        detail="missing or invalid required argument: category",
        fields=["category"],
    )

    chunks = list(model.stream([
        HumanMessage(content="what tools?"),
        AIMessage(content="", tool_calls=[{"name": "row_bot_status", "args": {}, "id": "call_1"}]),
        ToolMessage(content=repair, name="row_bot_status", tool_call_id="call_1"),
    ]))

    tool_chunks = [chunk for chunk in chunks if chunk.tool_call_chunks]
    assert tool_chunks[0].tool_call_chunks[0]["name"] == "row_bot_status"
    assert tool_chunks[0].tool_call_chunks[0]["args"] == '{"category": "tools"}'


def test_openai_compatible_transport_does_not_recover_text_tool_call_when_content_exists():
    client = _Client()
    client.post = lambda url, **kwargs: _Response({
        "choices": [{
            "message": {
                "content": "Here is text.",
                "reasoning_content": (
                    "<tool_call><function=row_bot_status>"
                    "<parameter=category>tools</parameter>"
                    "</function></tool_call>"
                ),
            },
        }],
    })
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={"provider_id": "custom_openai_tools"},
        http_client=client,
    )

    result = model.invoke([HumanMessage(content="what tools?")])

    assert result.content == "Here is text."
    assert result.tool_calls == []


def test_openai_compatible_transport_recovers_visible_text_tool_call_without_leaking_markup():
    client = _Client()
    client.post = lambda url, **kwargs: _Response({
        "choices": [{
            "message": {
                "content": (
                    "I will calculate this.\n"
                    "<tool_call><function=calculate><parameter=expression>19 * 23</parameter></function></tool_call>"
                    "\nDone."
                ),
            },
        }],
    })
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={"provider_id": "custom_openai_tools"},
        http_client=client,
    )

    result = model.invoke(
        [HumanMessage(content="are you sure?")],
        tools=[{"type": "function", "function": {"name": "calculate", "parameters": {"type": "object"}}}],
    )

    assert result.content == ""
    assert result.tool_calls == [{
        "name": "calculate",
        "args": {"expression": "19 * 23"},
        "id": "text_call_0",
        "type": "tool_call",
    }]


def test_openai_compatible_transport_dedupes_streamed_visible_text_tool_calls():
    class _VisibleTextToolStreamResponse:
        status_code = 200
        text = ""

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def iter_lines(self):
            payload = (
                "Checking.\n"
                "<tool_call><function=calculate><parameter=expression>19 * 23</parameter></function></tool_call>"
                "<tool_call><function=calculate><parameter=expression>19 * 23</parameter></function></tool_call>"
            )
            yield ('data: {"choices":[{"delta":{"content":' + json.dumps(payload) + '}}]}').encode("utf-8")
            yield b"data: [DONE]"

    class _VisibleTextToolClient(_Client):
        def stream(self, method, url, **kwargs):
            self.calls.append((url, kwargs))
            return _VisibleTextToolStreamResponse()

    client = _VisibleTextToolClient()
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={
            "provider_id": "custom_openai_tools",
            "last_probe": {"streaming_ok": True, "tool_calling": True, "streaming_tool_calling": True},
        },
        http_client=client,
    )

    chunks = list(model.stream(
        [HumanMessage(content="are you sure?")],
        tools=[{"type": "function", "function": {"name": "calculate", "parameters": {"type": "object"}}}],
    ))

    visible = "".join(str(chunk.content or "") for chunk in chunks)
    tool_chunks = [chunk for chunk in chunks if chunk.tool_call_chunks]
    assert "<tool_call>" not in visible
    assert visible == ""
    assert len(tool_chunks) == 1
    assert tool_chunks[0].tool_call_chunks[0]["name"] == "calculate"
    assert tool_chunks[0].tool_call_chunks[0]["args"] == '{"expression": "19 * 23"}'


def test_openai_compatible_transport_reports_unread_stream_error_body():
    client = _UnreadErrorStreamingClient()
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:8080/v1",
        endpoint={"display_name": "llama.cpp local"},
        http_client=client,
    )

    try:
        list(model.stream([HumanMessage(content="hi")]))
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("Expected streaming HTTP 400 to raise")

    assert "llama.cpp local rejected the chat request (HTTP 400)" in message
    assert "prompt exceeds context window" in message
    assert "streaming response content" not in message


def test_openai_compatible_stream_cancellation_closes_blocking_response():
    class _BlockingResponse:
        status_code = 200
        text = ""

        def __init__(self) -> None:
            self.iterating = threading.Event()
            self.closed = threading.Event()
            self.close_calls = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def close(self):
            self.close_calls += 1
            self.closed.set()

        def iter_lines(self):
            self.iterating.set()
            assert self.closed.wait(timeout=1)
            if False:
                yield b""

    class _BlockingClient(_Client):
        def __init__(self) -> None:
            super().__init__()
            self.response = _BlockingResponse()

        def stream(self, method, url, **kwargs):
            self.calls.append((url, kwargs))
            return self.response

    client = _BlockingClient()
    model = ChatOpenAICompatible(
        model_name="qwen",
        base_url="http://127.0.0.1:1234/v1",
        endpoint={"provider_id": "custom_openai_tools"},
        http_client=client,
    )
    scope = CancellationScope()
    events: list[dict] = []

    def consume() -> None:
        with use_cancellation_scope(scope):
            events.extend(model._iter_stream_events({"stream": True, "messages": []}))

    worker = threading.Thread(target=consume)
    worker.start()
    assert client.response.iterating.wait(timeout=1)

    scope.cancel("test")
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert client.response.close_calls == 1
    assert events == []


def test_custom_reasoning_precedence_is_extra_then_structured_then_dynamic():
    caps = ReasoningCapabilities(
        supported_efforts=("high",),
        request_style="openai_chat",
    )
    model = ChatOpenAICompatible(
        model_name="exact-model",
        base_url="http://127.0.0.1:8000/v1",
        endpoint={
            "profile": "vllm",
            "supports_reasoning_content": True,
            "extra_body": {
                "reasoning_effort": "low",
                "chat_template_kwargs": {"enable_thinking": False, "custom": True},
            },
            "reasoning_mode": "on",
            "thinking_budget": 2048,
        },
        reasoning_plan=ReasoningRequestPlan(
            "model:custom_openai_exact:exact-model",
            ReasoningSelection(kind="effort", effort="high"),
            caps,
        ),
    )

    body = model._request_body([HumanMessage(content="hello")], stream=False)

    assert body["reasoning_effort"] == "high"
    assert body["chat_template_kwargs"] == {
        "enable_thinking": True,
        "custom": True,
        "thinking_budget": 2048,
    }


def test_custom_provider_default_keeps_static_reasoning_baseline():
    model = ChatOpenAICompatible(
        model_name="exact-model",
        base_url="http://127.0.0.1:8000/v1",
        endpoint={
            "profile": "vllm",
            "supports_reasoning_content": True,
            "extra_body": {"top_k": 10},
            "reasoning_mode": "off",
            "thinking_budget": 1024,
        },
    )

    body = model._request_body([HumanMessage(content="hello")], stream=False)

    assert body["top_k"] == 10
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert "reasoning_effort" not in body
