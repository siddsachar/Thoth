import asyncio
import base64
import sys
import socket
from types import ModuleType, SimpleNamespace

import httpx
import pytest

import row_bot.providers.runtime as runtime
from row_bot.cancellation import CancellationScope, use_cancellation_scope
from row_bot.agent_budget import new_execution_budget
from row_bot.providers.errors import ProviderErrorKind, normalize_provider_error
from row_bot.providers.transports.anthropic_cancellable import CancellableChatAnthropic
from row_bot.providers.transports.openrouter_cancellable import CancellableChatOpenRouter


class _FakeChatOpenAI:
    calls = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls.append(kwargs)


class _CloseCountingStream(httpx.SyncByteStream):
    def __init__(self) -> None:
        self.close_calls = 0

    def __iter__(self):
        return iter(())

    def close(self) -> None:
        self.close_calls += 1


def _assert_sync_client_registered_with_cancellation_scope(client: httpx.Client) -> None:
    stream = _CloseCountingStream()
    response = httpx.Response(200, request=httpx.Request("GET", "https://example.test"), stream=stream)
    scope = CancellationScope()

    with use_cancellation_scope(scope):
        for hook in client.event_hooks["response"]:
            hook(response)

    assert stream.close_calls == 0
    scope.cancel("test")
    assert stream.close_calls == 1


def test_openai_gpt5_family_uses_responses_api(monkeypatch):
    fake_module = ModuleType("langchain_openai")
    fake_module.ChatOpenAI = _FakeChatOpenAI
    _FakeChatOpenAI.calls.clear()
    monkeypatch.setitem(sys.modules, "langchain_openai", fake_module)
    monkeypatch.setattr(runtime, "get_provider_secret", lambda provider_id: "sk-test")

    model = runtime.create_chat_model("gpt-5.5-pro", provider_id="openai")

    assert model.kwargs["model"] == "gpt-5.5-pro"
    assert model.kwargs["use_responses_api"] is True
    assert model.kwargs["output_version"] == "responses/v1"
    assert "reasoning" not in model.kwargs
    assert "reasoning_effort" not in model.kwargs
    assert isinstance(model.kwargs["http_client"], httpx.Client)


def test_openai_legacy_chat_model_keeps_chat_completions_path(monkeypatch):
    fake_module = ModuleType("langchain_openai")
    fake_module.ChatOpenAI = _FakeChatOpenAI
    _FakeChatOpenAI.calls.clear()
    monkeypatch.setitem(sys.modules, "langchain_openai", fake_module)
    monkeypatch.setattr(runtime, "get_provider_secret", lambda provider_id: "sk-test")

    model = runtime.create_chat_model("gpt-4o", provider_id="openai")

    assert model.kwargs["model"] == "gpt-4o"
    assert "use_responses_api" not in model.kwargs
    assert "reasoning" not in model.kwargs
    assert "reasoning_effort" not in model.kwargs
    assert isinstance(model.kwargs["http_client"], httpx.Client)


def test_anthropic_provider_constructor_is_preserved(monkeypatch):
    monkeypatch.setattr(runtime, "get_provider_secret", lambda provider_id: "anthropic-key")

    model = runtime.create_chat_model("claude-sonnet-4-5", provider_id="anthropic")

    assert isinstance(model, CancellableChatAnthropic)
    assert model.model == "claude-sonnet-4-5"
    assert model.anthropic_api_key.get_secret_value() == "anthropic-key"
    assert model.effort is None
    assert model.thinking is None
    assert "http_client" not in model.model_kwargs
    _assert_sync_client_registered_with_cancellation_scope(model._client._client)


def test_google_provider_constructor_is_preserved(monkeypatch):
    fake_module = ModuleType("langchain_google_genai")

    class _FakeChatGoogleGenerativeAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake_module.ChatGoogleGenerativeAI = _FakeChatGoogleGenerativeAI
    monkeypatch.setitem(sys.modules, "langchain_google_genai", fake_module)
    monkeypatch.setattr(runtime, "get_provider_secret", lambda provider_id: "google-key")

    model = runtime.create_chat_model("gemini-2.5-pro", provider_id="google")

    assert model.kwargs["model"] == "gemini-2.5-pro"
    assert model.kwargs["google_api_key"] == "google-key"
    assert "thinking_budget" not in model.kwargs
    assert "thinking_level" not in model.kwargs


def test_google_provider_constructor_preserves_reasoning_and_default_semantics(monkeypatch):
    from row_bot.providers.reasoning import (
        ReasoningCapabilities,
        ReasoningRequestPlan,
        ReasoningSelection,
    )
    from row_bot.providers.transports.reasoning_fallback import ReasoningFallbackChatModel

    fake_module = ModuleType("langchain_google_genai")

    class _FakeChatGoogleGenerativeAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake_module.ChatGoogleGenerativeAI = _FakeChatGoogleGenerativeAI
    monkeypatch.setitem(sys.modules, "langchain_google_genai", fake_module)
    monkeypatch.setattr(runtime, "get_provider_secret", lambda provider_id: "google-key")
    caps = ReasoningCapabilities(
        supported_efforts=("low", "high"),
        mandatory=True,
        request_style="google_level",
    )
    plan = ReasoningRequestPlan(
        "model:google:gemini-3.1-pro-preview",
        ReasoningSelection(kind="effort", effort="high"),
        caps,
    )

    model = runtime.create_chat_model(
        "gemini-3.1-pro-preview",
        provider_id="google",
        reasoning_plan=plan,
    )

    assert isinstance(model, ReasoningFallbackChatModel)
    assert model.primary.kwargs["thinking_level"] == "high"
    assert "thinking_budget" not in model.primary.kwargs
    assert "thinking_level" not in model.provider_default.kwargs
    assert "thinking_budget" not in model.provider_default.kwargs


def test_google_consolidated_sdk_preserves_tools_streaming_multimodal_and_thought_replay():
    from google.genai import types
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langchain_google_genai import ChatGoogleGenerativeAI
    from langchain_google_genai.chat_models import _parse_chat_history

    model = ChatGoogleGenerativeAI(
        model="gemini-3.1-pro-preview",
        google_api_key="test-key",
    )
    bound = model.bind_tools([
        {
            "name": "lookup",
            "description": "Look up a value",
            "parameters": {"type": "object", "properties": {}},
        }
    ])
    assert "tools" in bound.kwargs

    signature = base64.b64encode(b"rb337-signature").decode("ascii")
    messages = [
        HumanMessage(content=[
            {"type": "text", "text": "Inspect this image"},
            {
                "type": "media",
                "mime_type": "image/png",
                "data": "iVBORw0KGgo=",
            },
        ]),
        AIMessage(
            content="",
            tool_calls=[{
                "name": "lookup",
                "args": {},
                "id": "call_google",
                "type": "tool_call",
            }],
            additional_kwargs={
                "__gemini_function_call_thought_signatures__": {
                    "call_google": signature,
                }
            },
        ),
        ToolMessage(content="ok", tool_call_id="call_google"),
    ]
    _system, history = _parse_chat_history(messages, model="gemini-3.1-pro-preview")

    assert history[0].parts[1].inline_data.mime_type == "image/png"
    assert history[1].parts[0].thought_signature == b"rb337-signature"

    response = types.GenerateContentResponse(candidates=[
        types.Candidate(
            content=types.Content(role="model", parts=[types.Part(text="streamed")])
        )
    ])
    object.__setattr__(
        model,
        "client",
        SimpleNamespace(
            models=SimpleNamespace(
                generate_content_stream=lambda **_kwargs: iter([response])
            )
        ),
    )

    chunks = list(model._stream([HumanMessage(content="hello")]))

    assert [chunk.message.content for chunk in chunks] == ["streamed"]


def test_google_consolidated_sdk_async_stream_propagates_cancellation():
    from langchain_core.messages import HumanMessage
    from langchain_google_genai import ChatGoogleGenerativeAI

    async def _scenario() -> None:
        started = asyncio.Event()

        class _BlockingStream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                started.set()
                await asyncio.Future()

        class _AsyncModels:
            async def generate_content_stream(self, **_kwargs):
                return _BlockingStream()

        model = ChatGoogleGenerativeAI(
            model="gemini-3.1-pro-preview",
            google_api_key="test-key",
        )
        object.__setattr__(
            model,
            "client",
            SimpleNamespace(aio=SimpleNamespace(models=_AsyncModels())),
        )

        async def _consume() -> None:
            async for _chunk in model._astream([HumanMessage(content="hello")]):
                pass

        task = asyncio.create_task(_consume())
        await asyncio.wait_for(started.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_scenario())


def test_xai_provider_constructor_is_preserved(monkeypatch):
    fake_module = ModuleType("langchain_xai")

    class _FakeChatXAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake_module.ChatXAI = _FakeChatXAI
    monkeypatch.setitem(sys.modules, "langchain_xai", fake_module)
    monkeypatch.setattr(runtime, "get_provider_secret", lambda provider_id: "xai-key")

    model = runtime.create_chat_model("grok-4", provider_id="xai")

    assert model.kwargs["model"] == "grok-4"
    assert model.kwargs["api_key"] == "xai-key"
    assert "reasoning" not in model.kwargs
    assert "reasoning_effort" not in model.kwargs
    assert isinstance(model.kwargs["http_client"], httpx.Client)


def test_openrouter_provider_constructor_is_preserved(monkeypatch):
    monkeypatch.setattr(runtime, "get_provider_secret", lambda provider_id: "openrouter-key")

    model = runtime.create_chat_model("anthropic/claude-sonnet-4", provider_id="openrouter")

    assert isinstance(model, CancellableChatOpenRouter)
    assert model.model_name == "anthropic/claude-sonnet-4"
    assert model.openrouter_api_key.get_secret_value() == "openrouter-key"
    assert "reasoning" not in model.model_kwargs
    _assert_sync_client_registered_with_cancellation_scope(model.client.sdk_configuration.client)


def test_codex_provider_constructor_is_preserved(monkeypatch):
    import row_bot.providers.codex as codex

    monkeypatch.setattr(codex, "codex_runtime_available", lambda: True)

    model = runtime.create_chat_model("gpt-5.5", provider_id="codex")

    assert model.model_name == "gpt-5.5"


def test_custom_openai_endpoint_uses_configured_base_url(tmp_path, monkeypatch):
    import row_bot.providers.config as provider_config
    from row_bot.providers.custom import custom_provider_id, save_custom_endpoint

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setenv("ROW_BOT_OPENAI_COMPATIBLE_READ_TIMEOUT", "720")

    save_custom_endpoint({
        "id": "local-vllm",
        "name": "Local vLLM",
        "base_url": "http://127.0.0.1:8000/v1/",
        "auth_required": False,
        "execution_location": "local",
    })

    model = runtime.create_chat_model("meta-llama/Llama-3.1-8B-Instruct", provider_id=custom_provider_id("local-vllm"))

    assert model.model_name == "meta-llama/Llama-3.1-8B-Instruct"
    assert model.base_url == "http://127.0.0.1:8000/v1"
    assert model.api_key == "not-needed"
    assert model.endpoint["provider_id"] == custom_provider_id("local-vllm")
    assert model.timeout == 720.0


def test_custom_endpoint_model_syncs_into_model_facade(tmp_path, monkeypatch):
    import row_bot.models as models
    import row_bot.providers.config as provider_config
    from row_bot.providers.custom import custom_provider_id, save_custom_endpoint

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    models._cloud_model_cache.pop("row-bot-dummy-chat", None)
    save_custom_endpoint({
        "id": "dummy",
        "base_url": "http://127.0.0.1:8000/v1",
        "auth_required": False,
        "models": [{
            "id": "row-bot-dummy-chat",
            "model_id": "row-bot-dummy-chat",
            "label": "row-bot-dummy-chat",
            "ctx": 8192,
            "provider": custom_provider_id("dummy"),
            "capabilities_snapshot": {"tasks": ["chat"], "input_modalities": ["text"], "output_modalities": ["text"]},
        }],
    })

    assert models.is_cloud_model("row-bot-dummy-chat") is True
    assert models.get_cloud_provider("row-bot-dummy-chat") == custom_provider_id("dummy")


def test_model_facade_preserves_provider_refs_for_duplicate_ids(monkeypatch):
    import row_bot.models as models

    monkeypatch.setattr(models, "_sync_custom_model_cache", lambda: None)
    monkeypatch.setitem(models._cloud_model_cache, "gpt-5.5", {"provider": "openai", "ctx": 1_048_576})

    assert models.is_cloud_model("model:openai:gpt-5.5") is True
    assert models.get_cloud_provider("model:openai:gpt-5.5") == "openai"
    assert models.get_cloud_model_context("model:openai:gpt-5.5") == 1_048_576
    assert models.is_cloud_model("model:codex:gpt-5.5") is True
    assert models.get_cloud_provider("model:codex:gpt-5.5") == "codex"


def test_runtime_rejects_known_non_chat_model_before_provider_client():
    try:
        runtime.create_chat_model("text-embedding-3-large", provider_id="openai")
    except ValueError as exc:
        assert "not compatible with chat" in str(exc)
    else:
        raise AssertionError("Expected non-chat model to be rejected")


def test_runtime_uses_cached_provider_snapshot_for_chat_compatibility(monkeypatch):
    import row_bot.models as models

    model_id = "vendor/embed-special"
    monkeypatch.setitem(models._cloud_model_cache, model_id, {
        "provider": "openrouter",
        "ctx": 8192,
        "capabilities_snapshot": {
            "tasks": ["embedding"],
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "tool_calling": False,
            "transport": "openai_chat",
        },
    })

    try:
        runtime.create_chat_model(model_id, provider_id="openrouter")
    except ValueError as exc:
        assert "not compatible with chat" in str(exc)
    else:
        raise AssertionError("Expected cached non-chat OpenRouter model to be rejected")


def test_runtime_rejects_custom_endpoint_non_chat_model(tmp_path, monkeypatch):
    import row_bot.providers.config as provider_config
    from row_bot.providers.custom import custom_provider_id, save_custom_endpoint

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    save_custom_endpoint({
        "id": "dummy",
        "base_url": "http://127.0.0.1:8000/v1",
        "auth_required": False,
        "models": [{
            "id": "row-bot-dummy-embedding",
            "model_id": "row-bot-dummy-embedding",
            "capabilities_snapshot": {"tasks": ["embedding"], "input_modalities": ["text"], "output_modalities": ["text"]},
        }],
    })

    try:
        runtime.create_chat_model("row-bot-dummy-embedding", provider_id=custom_provider_id("dummy"))
    except ValueError as exc:
        assert "not compatible with chat" in str(exc)
    else:
        raise AssertionError("Expected custom embedding model to be rejected")


def test_ollama_provider_runtime_constructs_chat_ollama(monkeypatch):
    fake_module = ModuleType("langchain_ollama")

    class _FakeChatOllama:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake_module.ChatOllama = _FakeChatOllama
    monkeypatch.setitem(sys.modules, "langchain_ollama", fake_module)
    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0:11435")
    monkeypatch.setattr("row_bot.models.get_context_size", lambda model_ref=None: 65_536)

    model = runtime.create_chat_model("qwen3:14b", provider_id="ollama")

    assert model.kwargs == {
        "model": "qwen3:14b",
        "base_url": "http://127.0.0.1:11435",
        "num_ctx": 65_536,
        "reasoning": True,
    }


def test_ollama_provider_ref_unwraps_at_runtime_edge(monkeypatch):
    fake_module = ModuleType("langchain_ollama")

    class _FakeChatOllama:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake_module.ChatOllama = _FakeChatOllama
    monkeypatch.setitem(sys.modules, "langchain_ollama", fake_module)
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:11434")
    monkeypatch.setattr("row_bot.models.get_context_size", lambda model_ref=None: 32_768)

    model = runtime.create_chat_model("model:ollama:qwen3:14b")

    assert model.kwargs["model"] == "qwen3:14b"
    assert model.kwargs["base_url"] == "http://127.0.0.1:11434"
    assert model.kwargs["num_ctx"] == 32_768


def test_ollama_provider_runtime_expands_unique_family_alias(monkeypatch):
    fake_module = ModuleType("langchain_ollama")

    class _FakeChatOllama:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake_module.ChatOllama = _FakeChatOllama
    monkeypatch.setitem(sys.modules, "langchain_ollama", fake_module)
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:11434")
    monkeypatch.setattr("row_bot.models.list_local_models", lambda: ["llama3:latest"])
    monkeypatch.setattr("row_bot.models.get_context_size", lambda model_ref=None: 32_768)

    model = runtime.create_chat_model("model:ollama:llama3")

    assert model.kwargs["model"] == "llama3:latest"
    assert model.kwargs["base_url"] == "http://127.0.0.1:11434"
    assert model.kwargs["num_ctx"] == 32_768


def test_runtime_does_not_implicitly_route_unknown_models_to_openrouter():
    try:
        runtime.create_chat_model("qwen3:14b")
    except ValueError as exc:
        assert "Provider is required" in str(exc)
        assert "OpenRouter" in str(exc)
    else:
        raise AssertionError("Expected unknown bare model to require a provider")


def test_ollama_cloud_provider_runtime_constructs_native_client(monkeypatch):
    monkeypatch.setattr(runtime, "get_provider_secret", lambda provider_id: "ollama-cloud-key")

    model = runtime.create_chat_model("gpt-oss:120b-cloud", provider_id="ollama_cloud")

    assert model.model_name == "gpt-oss:120b-cloud"
    assert model.api_key == "ollama-cloud-key"
    assert model.base_url == "https://ollama.com"


def test_clear_llm_cache_drops_cached_provider_credentials(monkeypatch):
    import row_bot.models as models

    models.clear_llm_cache()
    keys = iter(["old-key", "new-key"])
    monkeypatch.setattr("row_bot.providers.runtime.get_provider_secret", lambda provider_id: next(keys))

    first = models._get_cloud_llm("model:ollama_cloud:gpt-oss:20b")
    models.clear_llm_cache()
    second = models._get_cloud_llm("model:ollama_cloud:gpt-oss:20b")

    assert first.api_key == "old-key"
    assert second.api_key == "new-key"
    models.clear_llm_cache()


def test_ollama_cloud_runtime_posts_native_chat_request():
    from langchain_core.messages import HumanMessage
    from row_bot.providers.transports.ollama_cloud import ChatOllamaCloud

    captured = {}

    class _Response:
        status_code = 200
        text = "{}"

        def json(self):
            return {"message": {"role": "assistant", "content": "hello"}, "done": True}

    class _Client:
        def post(self, url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return _Response()

    model = ChatOllamaCloud(model_name="gpt-oss:120b-cloud", api_key="test-key", http_client=_Client())
    result = model.invoke([HumanMessage(content="Hi")])

    assert result.content == "hello"
    assert captured["url"] == "https://ollama.com/api/chat"
    assert captured["kwargs"]["headers"]["Authorization"] == "Bearer test-key"
    assert captured["kwargs"]["json"]["model"] == "gpt-oss:120b"
    assert captured["kwargs"]["json"]["messages"] == [{"role": "user", "content": "Hi"}]


def test_ollama_cloud_runtime_normalizes_cloud_suffix():
    from row_bot.providers.transports.ollama_cloud import normalize_ollama_cloud_model_name

    assert normalize_ollama_cloud_model_name("gpt-oss:120b-cloud") == "gpt-oss:120b"
    assert normalize_ollama_cloud_model_name("gemma4:31b-cloud") == "gemma4:31b"
    assert normalize_ollama_cloud_model_name("gpt-oss:cloud") == "gpt-oss"
    assert normalize_ollama_cloud_model_name("kimi-k2:thinking") == "kimi-k2:thinking"


def test_ollama_cloud_runtime_normalizes_bearer_prefix():
    from row_bot.providers.transports.ollama_cloud import ChatOllamaCloud

    model = ChatOllamaCloud(model_name="gpt-oss:120b", api_key="Bearer test-key")

    assert model._headers()["Authorization"] == "Bearer test-key"


def test_ollama_cloud_401_has_actionable_message():
    from langchain_core.messages import HumanMessage
    from row_bot.providers.transports.ollama_cloud import ChatOllamaCloud

    class _Response:
        status_code = 401
        text = ""

    class _Client:
        def post(self, url, **kwargs):
            return _Response()

    model = ChatOllamaCloud(model_name="gpt-oss:120b", api_key="bad-key", http_client=_Client())

    try:
        model.invoke([HumanMessage(content="Hi")])
    except RuntimeError as exc:
        assert "Ollama Cloud rejected the API key" in str(exc)
        assert "Settings -> Providers" in str(exc)
    else:
        raise AssertionError("Expected Ollama Cloud 401 to raise")


def test_ollama_cloud_403_has_actionable_message():
    from langchain_core.messages import HumanMessage
    from row_bot.providers.transports.ollama_cloud import ChatOllamaCloud

    class _Response:
        status_code = 403
        text = ""

        def json(self):
            return {"error": "model requires access"}

    class _Client:
        def post(self, url, **kwargs):
            return _Response()

    model = ChatOllamaCloud(model_name="gpt-oss:120b", api_key="test-key", http_client=_Client())

    try:
        model.invoke([HumanMessage(content="Hi")])
    except RuntimeError as exc:
        assert "Ollama Cloud refused this request" in str(exc)
        assert "selected model" in str(exc)
        assert "model requires access" in str(exc)
    else:
        raise AssertionError("Expected Ollama Cloud 403 to raise")


def test_ollama_cloud_400_has_actionable_message():
    from langchain_core.messages import HumanMessage
    from row_bot.providers.transports.ollama_cloud import ChatOllamaCloud

    class _Response:
        status_code = 400
        text = ""

        def json(self):
            return {"error": "invalid message payload"}

    class _Client:
        def post(self, url, **kwargs):
            return _Response()

    model = ChatOllamaCloud(model_name="gpt-oss:120b", api_key="test-key", http_client=_Client())

    try:
        model.invoke([HumanMessage(content="Hi")])
    except RuntimeError as exc:
        assert "Ollama Cloud rejected the chat request" in str(exc)
        assert "invalid message payload" in str(exc)
    else:
        raise AssertionError("Expected Ollama Cloud 400 to raise")


def test_ollama_cloud_500_has_actionable_message():
    from langchain_core.messages import HumanMessage
    from row_bot.providers.transports.ollama_cloud import ChatOllamaCloud

    class _Response:
        status_code = 500
        text = ""

        def json(self):
            return {"error": "upstream failed"}

    class _Client:
        def post(self, url, **kwargs):
            return _Response()

    model = ChatOllamaCloud(model_name="gpt-oss:120b", api_key="test-key", http_client=_Client())

    try:
        model.invoke([HumanMessage(content="Hi")])
    except RuntimeError as exc:
        assert "Ollama Cloud returned a server error" in str(exc)
        assert "selected model or request shape" in str(exc)
        assert "upstream failed" in str(exc)
    else:
        raise AssertionError("Expected Ollama Cloud 500 to raise")


def test_ollama_cloud_runtime_skips_tools_for_non_tool_models():
    from langchain_core.messages import HumanMessage
    from row_bot.providers.transports.ollama_cloud import ChatOllamaCloud

    captured = {}

    class _Response:
        status_code = 200
        text = "{}"

        def json(self):
            return {"message": {"role": "assistant", "content": "hello"}, "done": True}

    class _Client:
        def post(self, url, **kwargs):
            captured["json"] = kwargs["json"]
            return _Response()

    model = ChatOllamaCloud(model_name="kimi-k2:thinking", api_key="test-key", http_client=_Client())
    model.invoke([HumanMessage(content="Hi")], tools=[{"type": "function", "function": {"name": "ping"}}])

    assert "tools" not in captured["json"]


def test_ollama_cloud_runtime_keeps_tools_for_tool_models():
    from langchain_core.messages import HumanMessage
    from row_bot.providers.transports.ollama_cloud import ChatOllamaCloud

    captured = {}

    class _Response:
        status_code = 200
        text = "{}"

        def json(self):
            return {"message": {"role": "assistant", "content": "hello"}, "done": True}

    class _Client:
        def post(self, url, **kwargs):
            captured["json"] = kwargs["json"]
            return _Response()

    model = ChatOllamaCloud(model_name="gpt-oss:120b", api_key="test-key", http_client=_Client())
    model.invoke([HumanMessage(content="Hi")], tools=[{"type": "function", "function": {"name": "ping"}}])

    assert "tools" in captured["json"]


def test_ollama_cloud_runtime_serializes_tool_results_with_tool_name():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from row_bot.providers.transports.ollama_cloud import ChatOllamaCloud

    captured = {}

    class _Response:
        status_code = 200
        text = "{}"

        def json(self):
            return {"message": {"role": "assistant", "content": "hello"}, "done": True}

    class _Client:
        def post(self, url, **kwargs):
            captured["messages"] = kwargs["json"]["messages"]
            return _Response()

    model = ChatOllamaCloud(model_name="gpt-oss:120b", api_key="test-key", http_client=_Client())
    model.invoke([
        HumanMessage(content="weather"),
        AIMessage(content="", tool_calls=[{"name": "get_weather", "args": {"city": "London"}, "id": "call_1"}]),
        ToolMessage(content="rain", name="get_weather", tool_call_id="call_1"),
    ])

    assert captured["messages"][1]["tool_calls"][0]["type"] == "function"
    assert captured["messages"][1]["tool_calls"][0]["function"]["index"] == 0
    assert captured["messages"][2]["role"] == "tool"
    assert captured["messages"][2]["tool_name"] == "get_weather"
    assert "tool_call_id" not in captured["messages"][2]


def test_ollama_cloud_runtime_flattens_tool_history_for_non_tool_models():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from row_bot.providers.transports.ollama_cloud import ChatOllamaCloud

    captured = {}

    class _Response:
        status_code = 200
        text = "{}"

        def json(self):
            return {"message": {"role": "assistant", "content": "hello"}, "done": True}

    class _Client:
        def post(self, url, **kwargs):
            captured["messages"] = kwargs["json"]["messages"]
            return _Response()

    model = ChatOllamaCloud(model_name="gemma3:4b", api_key="test-key", http_client=_Client())
    model.invoke([
        HumanMessage(content="weather"),
        AIMessage(content="", tool_calls=[{"name": "get_weather", "args": {"city": "London"}, "id": "call_1"}]),
        ToolMessage(content="rain", name="get_weather", tool_call_id="call_1"),
    ])

    assert "tool_calls" not in captured["messages"][1]
    assert captured["messages"][2]["role"] == "user"
    assert captured["messages"][2]["content"] == "[Tool result from get_weather]: rain"
    assert "tool_name" not in captured["messages"][2]


def test_ollama_cloud_key_validation_uses_authenticated_chat_probe(monkeypatch):
    import row_bot.models as models

    calls = []

    class _Response:
        def __init__(self, status_code=200, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text

        def json(self):
            return self._payload

    def fake_post(url, **kwargs):
        calls.append(("POST", url, kwargs))
        return _Response(200, {})

    monkeypatch.setattr("httpx.post", fake_post)

    assert models.validate_ollama_cloud_key("Bearer test-key") is True
    post_call = next(call for call in calls if call[0] == "POST")
    assert post_call[2]["headers"]["Authorization"] == "Bearer test-key"
    assert post_call[2]["json"]["model"] == "gpt-oss:20b"
    assert post_call[2]["json"]["options"]["num_predict"] == 1


def test_ollama_cloud_runtime_serializes_vision_images():
    from langchain_core.messages import HumanMessage
    from row_bot.providers.transports.ollama_cloud import ChatOllamaCloud

    captured = {}

    class _Response:
        status_code = 200
        text = "{}"

        def json(self):
            return {"message": {"role": "assistant", "content": "seen"}, "done": True}

    class _Client:
        def post(self, url, **kwargs):
            captured["kwargs"] = kwargs
            return _Response()

    model = ChatOllamaCloud(model_name="gemma4:31b-cloud", api_key="test-key", http_client=_Client())
    result = model.invoke([HumanMessage(content=[
        {"type": "text", "text": "What is in this image?"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
    ])])

    assert result.content == "seen"
    assert captured["kwargs"]["json"]["messages"] == [{
        "role": "user",
        "content": "What is in this image?",
        "images": ["AAAA"],
    }]


def test_ollama_reachable_parses_ollama_host_variants(monkeypatch):
    import row_bot.models as models

    calls = []

    class _FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def _fake_create_connection(address, timeout=None):
        calls.append((address, timeout))
        return _FakeConnection()

    monkeypatch.setattr(socket, "create_connection", _fake_create_connection)

    cases = [
        (None, ("127.0.0.1", 11434), "http://127.0.0.1:11434"),
        ("127.0.0.1", ("127.0.0.1", 11434), "http://127.0.0.1:11434"),
        ("127.0.0.1:11435", ("127.0.0.1", 11435), "http://127.0.0.1:11435"),
        ("http://127.0.0.1:11436", ("127.0.0.1", 11436), "http://127.0.0.1:11436"),
        ("localhost:notaport", ("localhost", 11434), "http://localhost:11434"),
        ("[::1]:11437", ("::1", 11437), "http://[::1]:11437"),
        ("0.0.0.0", ("127.0.0.1", 11434), "http://127.0.0.1:11434"),
        ("0.0.0.0:11438", ("127.0.0.1", 11438), "http://127.0.0.1:11438"),
        ("http://0.0.0.0:11439", ("127.0.0.1", 11439), "http://127.0.0.1:11439"),
        ("[::]:11440", ("::1", 11440), "http://[::1]:11440"),
    ]
    for value, expected_address, expected_base_url in cases:
        if value is None:
            monkeypatch.delenv("OLLAMA_HOST", raising=False)
        else:
            monkeypatch.setenv("OLLAMA_HOST", value)
        calls.clear()
        assert models._ollama_reachable(timeout=0.25) is True
        assert calls == [(expected_address, 0.25)]
        assert models._ollama_base_url() == expected_base_url


def test_ollama_client_uses_normalized_base_url(monkeypatch):
    import row_bot.models as models

    hosts = []

    class _FakeClient:
        def __init__(self, host=None):
            hosts.append(host)

    fake_module = ModuleType("ollama")
    fake_module.Client = _FakeClient
    monkeypatch.setattr(models, "_ollama_mod", fake_module)
    monkeypatch.setenv("OLLAMA_HOST", "http://0.0.0.0:11435")

    client = models._ollama_client()

    assert isinstance(client, _FakeClient)
    assert hosts == ["http://127.0.0.1:11435"]


def test_provider_errors_normalize_unsupported_capability():
    normalized = normalize_provider_error(ValueError("This model does not support tools"))

    assert normalized.kind == ProviderErrorKind.UNSUPPORTED_CAPABILITY
    assert normalized.next_action == "Choose a model whose capability badges match this surface."


def test_minimax_provider_creates_chat_anthropic_with_minimax_base_url(monkeypatch):
    monkeypatch.setattr(runtime, "get_provider_secret", lambda provider_id: "test-minimax-key")

    model = runtime.create_chat_model("MiniMax-M2.7", provider_id="minimax")

    assert isinstance(model, CancellableChatAnthropic)
    assert model.model == "MiniMax-M2.7"
    assert model.anthropic_api_key.get_secret_value() == "test-minimax-key"
    assert "http_client" not in model.model_kwargs
    assert model.anthropic_api_url == "https://api.minimax.io/anthropic"
    _assert_sync_client_registered_with_cancellation_scope(model._client._client)


def test_atlascloud_provider_creates_openai_compatible_client(monkeypatch):
    monkeypatch.setattr(runtime, "get_provider_secret", lambda provider_id: "test-atlas-key")

    model = runtime.create_chat_model("deepseek-ai/DeepSeek-V3-0324", provider_id="atlascloud")

    assert type(model).__name__ == "ChatOpenAICompatible"
    assert model.model_name == "deepseek-ai/DeepSeek-V3-0324"
    assert model.api_key == "test-atlas-key"
    assert model.base_url == "https://api.atlascloud.ai/v1"
    assert model.endpoint["provider_id"] == "atlascloud"
    assert model.endpoint["transport"] == "openai_chat"
    assert model.endpoint["profile"] == "atlascloud"


def test_atlascloud_provider_requires_api_key(monkeypatch):
    monkeypatch.setattr(runtime, "get_provider_secret", lambda provider_id: "")

    try:
        runtime.create_chat_model("deepseek-ai/DeepSeek-V3-0324", provider_id="atlascloud")
    except ValueError as exc:
        assert "Atlas Cloud API key not configured" in str(exc)
    else:
        raise AssertionError("Expected missing Atlas Cloud key to raise")


def test_atlascloud_listed_among_configured_providers(monkeypatch):
    monkeypatch.setattr(
        runtime,
        "is_provider_available",
        lambda provider_id: provider_id == "atlascloud",
    )
    monkeypatch.setattr(runtime, "provider_status", lambda provider_id, **kwargs: {"configured": False})

    configured = runtime.list_configured_provider_ids()

    assert "atlascloud" in configured


def test_minimax_model_facade_fetches_live_catalog_and_capabilities(monkeypatch):
    import httpx
    import row_bot.models as models
    import row_bot.providers.auth_store as auth_store
    from row_bot.providers.capabilities import snapshot_supports_surface

    old_cache = dict(models._cloud_model_cache)
    monkeypatch.setattr(auth_store, "get_provider_secret", lambda provider_id: "test-minimax-key" if provider_id == "minimax" else "")
    calls = []

    class _Response:
        status_code = 200
        text = "{}"

        def __init__(self, body):
            self._body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self._body

    def _fake_get(url, **kwargs):
        calls.append({"url": url, "params": dict(kwargs.get("params") or {})})
        if len(calls) == 1:
            return _Response({
                "data": [
                    {"id": "MiniMax-M3", "display_name": "MiniMax-M3"},
                    {"id": "MiniMax-M2.7", "display_name": "MiniMax-M2.7"},
                ],
                "has_more": True,
                "last_id": "MiniMax-M2.7",
            })
        return _Response({
            "data": [{"id": "MiniMax-M2.5", "display_name": "MiniMax-M2.5"}],
            "has_more": False,
            "last_id": "MiniMax-M2.5",
        })

    monkeypatch.setattr(httpx, "get", _fake_get)
    try:
        models._cloud_model_cache.clear()
        models._cloud_model_cache["MiniMax-M2"] = {
            "provider": "minimax",
            "label": "Stale MiniMax-M2",
            "ctx": 204_800,
        }
        count = models.fetch_cloud_models("minimax")

        assert count == 3
        assert calls[0]["url"] == "https://api.minimax.io/anthropic/v1/models"
        assert calls[0]["params"] == {"limit": 100}
        assert calls[1]["params"] == {"limit": 100, "after_id": "MiniMax-M2.7"}
        assert "MiniMax-M2" not in models._cloud_model_cache
        assert models.is_cloud_model("MiniMax-M3") is True
        assert models.get_cloud_provider("MiniMax-M3") == "minimax"
        assert models.get_cloud_model_context("MiniMax-M3") == 1_000_000
        m3_snapshot = models._cloud_model_cache["MiniMax-M3"]["capabilities_snapshot"]
        assert "chat" in m3_snapshot["tasks"]
        assert "image" in m3_snapshot["input_modalities"]
        assert "video" in m3_snapshot["input_modalities"]
        assert snapshot_supports_surface(m3_snapshot, "vision") is True
        assert snapshot_supports_surface(m3_snapshot, "video") is False
        assert m3_snapshot["tool_calling"] is True
        assert m3_snapshot["streaming"] is True
        assert m3_snapshot["source_confidence"] == "live_minimax_model_list"
        assert m3_snapshot["last_verified_at"]
        assert models.is_cloud_model("MiniMax-M2.7") is True
        assert models.get_cloud_provider("MiniMax-M2.7") == "minimax"
        assert models.get_cloud_model_context("MiniMax-M2.7") == 204_800
        assert models.get_provider_emoji("MiniMax-M2.7") == "M"
        assert models._cloud_model_cache["MiniMax-M2.7"]["provider"] == "minimax"
        m2_snapshot = models._cloud_model_cache["MiniMax-M2.7"]["capabilities_snapshot"]
        assert m2_snapshot["input_modalities"] == ["text"]
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(old_cache)


def test_minimax_live_catalog_failure_preserves_existing_cache(monkeypatch):
    import httpx
    import row_bot.models as models
    import row_bot.providers.auth_store as auth_store

    old_cache = dict(models._cloud_model_cache)
    monkeypatch.setattr(auth_store, "get_provider_secret", lambda provider_id: "test-minimax-key" if provider_id == "minimax" else "")
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: (_ for _ in ()).throw(httpx.TimeoutException("boom")))
    try:
        models._cloud_model_cache.clear()
        models._cloud_model_cache["MiniMax-M2.7"] = {
            "provider": "minimax",
            "label": "MiniMax-M2.7",
            "ctx": 204_800,
        }

        count = models.fetch_cloud_models("minimax")

        assert count == 0
        assert "MiniMax-M2.7" in models._cloud_model_cache
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(old_cache)


def test_minimax_live_catalog_does_not_rewrite_current_default(monkeypatch):
    import httpx
    import row_bot.models as models
    import row_bot.providers.auth_store as auth_store

    old_cache = dict(models._cloud_model_cache)
    old_current = models._current_model
    monkeypatch.setattr(auth_store, "get_provider_secret", lambda provider_id: "test-minimax-key" if provider_id == "minimax" else "")

    class _Response:
        status_code = 200
        text = "{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [{"id": "MiniMax-M3", "display_name": "MiniMax-M3"}],
                "has_more": False,
                "last_id": "MiniMax-M3",
            }

    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: _Response())
    try:
        models._cloud_model_cache.clear()
        models._cloud_model_cache["MiniMax-M2.7"] = {
            "provider": "minimax",
            "label": "MiniMax-M2.7",
            "ctx": 204_800,
        }
        models._current_model = "model:minimax:MiniMax-M2.7"

        count = models.fetch_cloud_models("minimax")

        assert count == 1
        assert "MiniMax-M2.7" not in models._cloud_model_cache
        assert models._current_model == "model:minimax:MiniMax-M2.7"
    finally:
        models._current_model = old_current
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(old_cache)


def test_ollama_cloud_model_facade_fetches_direct_catalog(monkeypatch):
    import httpx
    import row_bot.models as models
    import row_bot.providers.auth_store as auth_store

    old_cache = dict(models._cloud_model_cache)

    class _Response:
        status_code = 200
        text = "{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {"models": [{"name": "gpt-oss:120b-cloud"}]}

    monkeypatch.setattr(auth_store, "get_provider_secret", lambda provider_id: "test-ollama-key" if provider_id == "ollama_cloud" else "")
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: _Response())
    try:
        models._cloud_model_cache.clear()
        count = models.fetch_cloud_models("ollama_cloud")

        assert count == 1
        assert models.is_cloud_model("model:ollama_cloud:gpt-oss:120b-cloud") is True
        assert models.get_cloud_provider("model:ollama_cloud:gpt-oss:120b-cloud") == "ollama_cloud"
        assert models._cloud_model_cache["gpt-oss:120b-cloud"]["provider"] == "ollama_cloud"
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(old_cache)


def test_ollama_cloud_offload_model_facade_routes_to_local_ollama_provider():
    import row_bot.models as models

    assert models.is_cloud_model("model:ollama:gpt-oss:120b-cloud") is True
    assert models.get_cloud_provider("model:ollama:gpt-oss:120b-cloud") == "ollama"


def test_atlascloud_model_facade_fetches_openai_compatible_catalog(monkeypatch):
    import httpx
    import row_bot.models as models
    import row_bot.providers.auth_store as auth_store

    old_cache = dict(models._cloud_model_cache)
    captured = {}

    class _Response:
        status_code = 200
        text = "{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {"id": "deepseek-ai/DeepSeek-V3-0324", "context_length": 163_840},
                    {"id": "qwen/qwen3-32b"},
                ]
            }

    def _fake_get(url, **kwargs):
        captured["url"] = url
        captured["headers"] = dict(kwargs.get("headers") or {})
        return _Response()

    monkeypatch.setattr(auth_store, "get_provider_secret", lambda provider_id: "test-atlas-key" if provider_id == "atlascloud" else "")
    monkeypatch.setattr(httpx, "get", _fake_get)
    try:
        models._cloud_model_cache.clear()
        count = models.fetch_cloud_models("atlascloud")

        assert count == 2
        assert captured["url"] == "https://api.atlascloud.ai/v1/models"
        assert captured["headers"]["Authorization"] == "Bearer test-atlas-key"
        assert models.is_cloud_model("model:atlascloud:deepseek-ai/DeepSeek-V3-0324") is True
        assert models.get_cloud_provider("model:atlascloud:deepseek-ai/DeepSeek-V3-0324") == "atlascloud"
        assert models.get_cloud_model_context("model:atlascloud:deepseek-ai/DeepSeek-V3-0324") == 163_840
        assert "deepseek-ai/DeepSeek-V3-0324" not in models._cloud_model_cache
        assert models._cloud_model_cache["model:atlascloud:deepseek-ai/DeepSeek-V3-0324"]["provider"] == "atlascloud"
        assert models._cloud_model_cache["model:atlascloud:qwen/qwen3-32b"]["provider"] == "atlascloud"
        assert models.get_provider_emoji("model:atlascloud:deepseek-ai/DeepSeek-V3-0324") == "AC"
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(old_cache)


def test_atlascloud_live_catalog_failure_preserves_existing_cache(monkeypatch):
    import httpx
    import row_bot.models as models
    import row_bot.providers.auth_store as auth_store

    old_cache = dict(models._cloud_model_cache)
    monkeypatch.setattr(auth_store, "get_provider_secret", lambda provider_id: "test-atlas-key" if provider_id == "atlascloud" else "")
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: (_ for _ in ()).throw(httpx.TimeoutException("boom")))
    try:
        models._cloud_model_cache.clear()
        models._cloud_model_cache["model:atlascloud:deepseek-ai/DeepSeek-V3-0324"] = {
            "provider": "atlascloud",
            "label": "DeepSeek-V3-0324",
            "ctx": 163_840,
        }

        count = models.fetch_cloud_models("atlascloud")

        assert count == 0
        assert "model:atlascloud:deepseek-ai/DeepSeek-V3-0324" in models._cloud_model_cache
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(old_cache)


def test_minimax_validation_treats_insufficient_balance_as_accepted_key(monkeypatch):
    import httpx
    import row_bot.models as models

    captured = {}

    class _Response:
        status_code = 500
        text = '{"type":"error","error":{"type":"api_error","message":"insufficient balance (1008)"}}'

    def _fake_get(url, **kwargs):
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        return _Response()

    monkeypatch.setattr(httpx, "get", _fake_get)

    assert models.validate_minimax_key("test-minimax-key") is True
    assert captured["url"] == "https://api.minimax.io/anthropic/v1/models"
    assert captured["params"] == {"limit": 1}


def test_minimax_validation_rejects_auth_failure(monkeypatch):
    import httpx
    import row_bot.models as models

    class _Response:
        status_code = 401
        text = '{"type":"error","error":{"message":"invalid api key"}}'

    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: _Response())

    assert models.validate_minimax_key("bad-key") is False


def test_minimax_pre_model_trim_uses_anthropic_message_consolidation(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.agent as agent
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    monkeypatch.setattr(agent, "get_context_size", lambda: 200_000)
    monkeypatch.setattr(agent, "get_current_model", lambda: "MiniMax-M2.7")
    monkeypatch.setattr(agent, "is_cloud_model", lambda model: True)
    monkeypatch.setattr(agent, "get_cloud_provider", lambda model: "minimax")
    monkeypatch.setattr(agent, "is_background_workflow", lambda: False)

    agent.set_active_model_override("MiniMax-M2.7")
    try:
        result = agent._pre_model_trim({
            "execution_budget": new_execution_budget("minimax-trim"),
            "messages": [
                SystemMessage(content="Root system"),
                HumanMessage(content="Hello"),
                AIMessage(content="Hi"),
                SystemMessage(content="Late recall"),
                HumanMessage(content="Continue"),
            ]
        })["llm_input_messages"]
    finally:
        agent.set_active_model_override("")

    first_non_system = next(i for i, msg in enumerate(result) if not isinstance(msg, SystemMessage))
    assert all(isinstance(msg, SystemMessage) for msg in result[:first_non_system])
    assert not any(isinstance(msg, SystemMessage) for msg in result[first_non_system:])
    assert result[first_non_system].content == "Hello"
    assert not any(
        isinstance(msg.content, list)
        and any(isinstance(block, dict) and "cache_control" in block for block in msg.content)
        for msg in result
    )


@pytest.mark.parametrize(
    ("provider_id", "model_id"),
    [
        ("openrouter", "anthropic/claude-opus-5"),
        ("requesty", "anthropic/claude-opus-5"),
        ("requesty", "bedrock/claude-opus-5"),
        ("requesty", "vertex/claude-opus-5"),
    ],
)
def test_routed_claude_pre_model_trim_consolidates_long_history_without_mutating_checkpoint(
    tmp_path,
    monkeypatch,
    provider_id,
    model_id,
):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.agent as agent
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    model_ref = f"model:{provider_id}:{model_id}"
    tool_call = {
        "name": "row_bot_status",
        "args": {"category": "overview"},
        "id": "call_routed_claude",
        "type": "tool_call",
    }
    checkpoint_messages = [
        SystemMessage(content="Root system"),
        HumanMessage(content="Inspect status"),
        AIMessage(content="", tool_calls=[tool_call]),
        ToolMessage(content="ok", name="row_bot_status", tool_call_id="call_routed_claude"),
        AIMessage(content="Status inspected"),
        SystemMessage(content="Late recall and wind-down"),
        HumanMessage(content="Continue"),
    ]
    checkpoint_types = [type(message) for message in checkpoint_messages]
    checkpoint_contents = [message.content for message in checkpoint_messages]

    monkeypatch.setattr(agent, "get_context_size", lambda: 200_000)
    monkeypatch.setattr(agent, "get_current_model", lambda: model_ref)
    monkeypatch.setattr(agent, "is_cloud_model", lambda model: True)
    monkeypatch.setattr(agent, "get_cloud_provider", lambda model: provider_id)
    monkeypatch.setattr(agent, "is_background_workflow", lambda: False)

    agent.set_active_model_override(model_ref)
    try:
        provider_messages = agent._pre_model_trim({
            "execution_budget": new_execution_budget(f"routed-claude-{provider_id}"),
            "messages": checkpoint_messages,
        })["llm_input_messages"]
    finally:
        agent.set_active_model_override("")

    assert isinstance(provider_messages[0], SystemMessage)
    assert sum(isinstance(message, SystemMessage) for message in provider_messages) == 1
    assert "Root system" in provider_messages[0].content
    assert "Late recall and wind-down" in provider_messages[0].content
    assert not any(isinstance(message, SystemMessage) for message in provider_messages[1:])
    tool_call_index = next(
        index for index, message in enumerate(provider_messages) if getattr(message, "tool_calls", None)
    )
    assert isinstance(provider_messages[tool_call_index + 1], ToolMessage)
    assert provider_messages[tool_call_index + 1].tool_call_id == provider_messages[
        tool_call_index
    ].tool_calls[0]["id"]
    assert [type(message) for message in checkpoint_messages] == checkpoint_types
    assert [message.content for message in checkpoint_messages] == checkpoint_contents
    assert isinstance(checkpoint_messages[5], SystemMessage)


def test_custom_openai_pre_model_trim_compacts_32k_agent_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.agent as agent
    from langchain_core.messages import HumanMessage, SystemMessage

    monkeypatch.setattr(agent, "get_context_size", lambda: 32_768)
    monkeypatch.setattr(agent, "get_current_model", lambda: "model:custom_openai_lab:local")
    monkeypatch.setattr(agent, "is_cloud_model", lambda model: True)
    monkeypatch.setattr(agent, "get_cloud_provider", lambda model: "custom_openai_lab")
    monkeypatch.setattr(agent, "is_background_workflow", lambda: False)
    import row_bot.self_knowledge as self_knowledge
    import row_bot.skills as skills
    import row_bot.plugins.registry as plugin_registry

    monkeypatch.setattr(self_knowledge, "build_static_self_knowledge_block", lambda: "SELF_SENTINEL " * 1000)
    monkeypatch.setattr(self_knowledge, "build_dynamic_self_knowledge_block", lambda: "SELF_DYNAMIC_SENTINEL " * 1000)
    monkeypatch.setattr(skills, "get_skills_prompt", lambda *args, **kwargs: "SKILL_SENTINEL " * 1000)
    monkeypatch.setattr(plugin_registry, "get_skills_prompt", lambda *args, **kwargs: "PLUGIN_SENTINEL " * 1000)

    tool_token = agent._current_enabled_tool_names_var.set(tuple(f"tool_{i}" for i in range(30)))
    agent.set_active_model_override("model:custom_openai_lab:local")
    try:
        result = agent._pre_model_trim({
            "execution_budget": new_execution_budget("custom-openai-32k-trim"),
            "messages": [
                SystemMessage(content="Root system"),
                HumanMessage(content="hi"),
            ]
        })["llm_input_messages"]
    finally:
        agent._current_enabled_tool_names_var.reset(tool_token)
        agent.set_active_model_override("")

    system_text = "\n".join(str(msg.content) for msg in result if msg.type == "system")
    assert "SELF_SENTINEL" not in system_text
    assert "SKILL_SENTINEL" not in system_text
    assert "PLUGIN_SENTINEL" not in system_text
    assert sum(1 for msg in result if msg.type == "system") == 1


def test_custom_openai_pre_model_trim_consolidates_64k_system_envelope(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.agent as agent
    from langchain_core.messages import HumanMessage, SystemMessage

    monkeypatch.setattr(agent, "get_context_size", lambda: 65_536)
    monkeypatch.setattr(agent, "get_current_model", lambda: "model:custom_openai_lab:local")
    monkeypatch.setattr(agent, "is_cloud_model", lambda model: True)
    monkeypatch.setattr(agent, "get_cloud_provider", lambda model: "custom_openai_lab")
    monkeypatch.setattr(agent, "is_background_workflow", lambda: False)

    agent.set_active_model_override("model:custom_openai_lab:local")
    try:
        result = agent._pre_model_trim({
            "execution_budget": new_execution_budget("custom-openai-64k-trim"),
            "messages": [
                SystemMessage(content="Root system"),
                HumanMessage(content="hi"),
                SystemMessage(content="Late memory recall"),
                HumanMessage(content="continue"),
            ]
        })["llm_input_messages"]
    finally:
        agent.set_active_model_override("")

    assert result[0].type == "system"
    assert sum(1 for msg in result if msg.type == "system") == 1
    assert not any(msg.type == "system" for msg in result[1:])
    assert "Root system" in result[0].content
    assert "Late memory recall" in result[0].content


def test_pre_model_trim_drops_reasoning_only_assistant_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.agent as agent
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    monkeypatch.setattr(agent, "get_context_size", lambda: 200_000)
    monkeypatch.setattr(agent, "get_current_model", lambda: "model:openrouter:qwen/qwen3.7-max")
    monkeypatch.setattr(agent, "is_cloud_model", lambda model: True)
    monkeypatch.setattr(agent, "get_cloud_provider", lambda model: "openrouter")
    monkeypatch.setattr(agent, "is_background_workflow", lambda: False)

    result = agent._pre_model_trim({
        "execution_budget": new_execution_budget("reasoning-only-trim"),
        "messages": [
            SystemMessage(content="Root system"),
            HumanMessage(content="Use the status tool"),
            AIMessage(content="", additional_kwargs={"reasoning_content": "I should answer now."}),
            HumanMessage(content="try again"),
        ]
    })["llm_input_messages"]

    assert all(
        not (
            isinstance(msg, AIMessage)
            and not str(msg.content or "").strip()
            and not getattr(msg, "tool_calls", None)
        )
        for msg in result
    )
    assert [msg.type for msg in result if msg.type != "system"] == ["human", "human"]


def test_pre_model_trim_preserves_empty_assistant_tool_call_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.agent as agent
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    monkeypatch.setattr(agent, "get_context_size", lambda: 200_000)
    monkeypatch.setattr(agent, "get_current_model", lambda: "model:openrouter:qwen/qwen3.7-max")
    monkeypatch.setattr(agent, "is_cloud_model", lambda model: True)
    monkeypatch.setattr(agent, "get_cloud_provider", lambda model: "openrouter")
    monkeypatch.setattr(agent, "is_background_workflow", lambda: False)

    tool_call = {
        "name": "row_bot_status",
        "args": {"category": "tools"},
        "id": "call_1",
        "type": "tool_call",
    }
    result = agent._pre_model_trim({
        "execution_budget": new_execution_budget("tool-call-trim"),
        "messages": [
            SystemMessage(content="Root system"),
            HumanMessage(content="Use the status tool"),
            AIMessage(content="", tool_calls=[tool_call], additional_kwargs={"reasoning_content": "Use a tool."}),
            ToolMessage(content="ok", name="row_bot_status", tool_call_id="call_1"),
            HumanMessage(content="continue"),
        ]
    })["llm_input_messages"]

    preserved = [msg for msg in result if isinstance(msg, AIMessage)]
    assert len(preserved) == 1
    assert preserved[0].tool_calls == [tool_call]


def test_provider_transcript_normalizer_strips_invalid_tool_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.agent as agent
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    messages = [
        HumanMessage(content="use the tool"),
        AIMessage(
            content="",
            tool_calls=[{"name": "row_bot_status", "args": {}, "id": "call_1", "type": "tool_call"}],
            invalid_tool_calls=[{"name": "", "args": "bad", "id": "bad_1", "error": None}],
        ),
        ToolMessage(content="ok", name="row_bot_status", tool_call_id="call_1"),
    ]

    result = agent._normalize_provider_facing_messages(messages, provider_id="openrouter")

    ai = next(msg for msg in result if isinstance(msg, AIMessage))
    assert ai.tool_calls == [{"name": "row_bot_status", "args": {}, "id": "call_1", "type": "tool_call"}]
    assert getattr(ai, "invalid_tool_calls", []) == []
    assert result[2].tool_call_id == "call_1"


def test_provider_transcript_normalizer_rewrites_duplicate_tool_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.agent as agent
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    first_call = {"name": "row_bot_status", "args": {"category": "overview"}, "id": "text_call_0", "type": "tool_call"}
    second_call = {"name": "row_bot_status", "args": {"category": "tools"}, "id": "text_call_0", "type": "tool_call"}
    messages = [
        HumanMessage(content="check tools"),
        AIMessage(content="", tool_calls=[first_call]),
        ToolMessage(content="overview", name="row_bot_status", tool_call_id="text_call_0"),
        AIMessage(content="", tool_calls=[second_call]),
        ToolMessage(content="tools", name="row_bot_status", tool_call_id="text_call_0"),
    ]

    result = agent._normalize_provider_facing_messages(messages, provider_id="openrouter")
    ai_messages = [msg for msg in result if isinstance(msg, AIMessage)]
    tool_messages = [msg for msg in result if isinstance(msg, ToolMessage)]

    assert ai_messages[0].tool_calls[0]["id"] == "text_call_0"
    assert ai_messages[1].tool_calls[0]["id"] == "text_call_0_2"
    assert tool_messages[0].tool_call_id == "text_call_0"
    assert tool_messages[1].tool_call_id == "text_call_0_2"


def test_provider_transcript_normalizer_preserves_healthy_native_reasoning(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.agent as agent
    from langchain_core.messages import AIMessage, HumanMessage

    messages = [
        HumanMessage(content="hi"),
        AIMessage(content="hello", additional_kwargs={"reasoning_content": "native reasoning"}),
    ]

    result = agent._normalize_provider_facing_messages(messages, provider_id="openrouter")

    assert result[1].additional_kwargs == {"reasoning_content": "native reasoning"}


def test_provider_transcript_normalizer_repairs_google_reasoning_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.agent as agent
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_google_genai.chat_models import _parse_chat_history

    messages = [
        HumanMessage(content="hi"),
        AIMessage(
            content=[
                {"type": "reasoning", "summary": [{"type": "summary_text", "text": "private"}]},
                {"type": "reasoning", "text": "legacy reasoning"},
                {"type": "text", "text": "hello"},
            ]
        ),
    ]

    result = agent._normalize_provider_facing_messages(
        messages,
        provider_id="opencode_zen",
        google_genai=True,
    )

    assert result[1].content == [
        {"type": "reasoning", "reasoning": "legacy reasoning"},
        {"type": "text", "text": "hello"},
    ]
    _system, history = _parse_chat_history(result, model="gemini-3-flash")
    assert history[1].parts[0].text == "legacy reasoning"
    assert history[1].parts[0].thought is True
    assert history[1].parts[1].text == "hello"


def test_provider_transcript_normalizer_strips_reasoning_for_custom_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.agent as agent
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    messages = [
        HumanMessage(content="check tools"),
        AIMessage(
            content="",
            tool_calls=[{"name": "row_bot_status", "args": {"category": "tools"}, "id": "text_call_0", "type": "tool_call"}],
            additional_kwargs={"reasoning_content": "<tool_call><function=row_bot_status></function></tool_call>"},
        ),
        ToolMessage(content="tools", name="row_bot_status", tool_call_id="text_call_0"),
        AIMessage(content="Done", additional_kwargs={"reasoning_content": "I should now answer."}),
    ]

    result = agent._normalize_provider_facing_messages(messages, provider_id="openrouter")
    ai_messages = [msg for msg in result if isinstance(msg, AIMessage)]

    assert "reasoning_content" not in ai_messages[0].additional_kwargs
    assert ai_messages[-1].additional_kwargs == {"reasoning_content": "I should now answer."}
    assert ai_messages[-1].content == "Done"


def test_provider_transcript_normalizer_strips_reasoning_for_unsupported_custom_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.agent as agent
    import row_bot.providers.config as provider_config
    from row_bot.providers.custom import save_custom_endpoint
    from langchain_core.messages import AIMessage, HumanMessage

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    save_custom_endpoint({
        "id": "generic",
        "base_url": "http://127.0.0.1:8000/v1",
        "auth_required": False,
        "supports_reasoning_replay": False,
    })

    messages = [
        HumanMessage(content="hi"),
        AIMessage(content="hello", additional_kwargs={"reasoning_content": "native reasoning"}),
    ]

    result = agent._normalize_provider_facing_messages(messages, provider_id="custom_openai_generic")

    assert result[1].additional_kwargs == {}


def test_provider_transcript_normalizer_preserves_reasoning_for_supported_custom_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.agent as agent
    import row_bot.providers.config as provider_config
    from row_bot.providers.custom import save_custom_endpoint
    from langchain_core.messages import AIMessage, HumanMessage

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    save_custom_endpoint({
        "id": "qwen",
        "base_url": "http://127.0.0.1:8000/v1",
        "auth_required": False,
        "supports_reasoning_replay": True,
    })

    messages = [
        HumanMessage(content="hi"),
        AIMessage(content="", additional_kwargs={"reasoning_content": "native reasoning"}),
        HumanMessage(content="continue"),
    ]

    result = agent._normalize_provider_facing_messages(messages, provider_id="custom_openai_qwen")

    assert result[1].additional_kwargs == {"reasoning_content": "native reasoning"}
    assert result[1].content == ""


@pytest.mark.parametrize(
    ("provider_id", "extra_kwargs"),
    [
        ("anthropic", {}),
        ("minimax", {}),
        ("opencode_zen", {"anthropic_messages": True}),
    ],
)
def test_provider_transcript_normalizer_repairs_anthropic_signature_only_thinking_blocks(
    tmp_path,
    monkeypatch,
    provider_id,
    extra_kwargs,
):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.agent as agent
    from langchain_core.messages import AIMessage, HumanMessage

    chat_models = pytest.importorskip("langchain_anthropic.chat_models")

    messages = [
        HumanMessage(content="hi"),
        AIMessage(content=[
            {"type": "thinking", "signature": "sig_123", "index": 0},
            {"type": "text", "text": "hello"},
        ]),
    ]

    result = agent._normalize_provider_facing_messages(
        messages,
        provider_id=provider_id,
        **extra_kwargs,
    )

    ai = result[1]
    assert ai.content[0] == {"type": "thinking", "thinking": "", "signature": "sig_123"}
    assert ai.content[1] == {"type": "text", "text": "hello"}

    _system, payloads = chat_models._format_messages(result)
    thinking_block = payloads[1]["content"][0]
    assert thinking_block == {"type": "thinking", "thinking": "", "signature": "sig_123"}


def test_provider_transcript_normalizer_drops_incomplete_anthropic_thinking_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.agent as agent
    from langchain_core.messages import AIMessage, HumanMessage

    chat_models = pytest.importorskip("langchain_anthropic.chat_models")

    messages = [
        HumanMessage(content="hi"),
        AIMessage(content=[
            {"type": "thinking", "thinking": "partial without signature"},
            {"type": "redacted_thinking"},
            {"type": "redacted_thinking", "data": "encrypted"},
            {"type": "text", "text": "hello"},
        ]),
    ]

    result = agent._normalize_provider_facing_messages(messages, provider_id="anthropic")

    ai = result[1]
    assert ai.content == [
        {"type": "redacted_thinking", "data": "encrypted"},
        {"type": "text", "text": "hello"},
    ]

    _system, payloads = chat_models._format_messages(result)
    assert payloads[1]["content"] == ai.content


def test_provider_transcript_normalizer_serializes_cleanly_for_openrouter(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.agent as agent
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    chat_models = pytest.importorskip("langchain_openrouter.chat_models")
    _convert_message_to_dict = chat_models._convert_message_to_dict

    messages = [
        HumanMessage(content="check tools"),
        AIMessage(
            content="",
            tool_calls=[{"name": "row_bot_status", "args": {}, "id": "call_1", "type": "tool_call"}],
            invalid_tool_calls=[{"name": "", "args": '"category":"tools"}', "id": "openai_call_0", "error": None}],
            additional_kwargs={"reasoning_content": "use a tool"},
        ),
        ToolMessage(content="repair", name="row_bot_status", tool_call_id="call_1"),
        AIMessage(
            content="",
            tool_calls=[{"name": "row_bot_status", "args": {"category": "overview"}, "id": "text_call_0", "type": "tool_call"}],
            additional_kwargs={"reasoning_content": "<tool_call><function=row_bot_status></function></tool_call>"},
        ),
        ToolMessage(content="overview", name="row_bot_status", tool_call_id="text_call_0"),
        AIMessage(
            content="",
            tool_calls=[{"name": "row_bot_status", "args": {"category": "tools"}, "id": "text_call_0", "type": "tool_call"}],
            additional_kwargs={"reasoning_content": "<tool_call><function=row_bot_status></function></tool_call>"},
        ),
        ToolMessage(content="tools", name="row_bot_status", tool_call_id="text_call_0"),
        AIMessage(content="Done", additional_kwargs={"reasoning_content": "answer now"}),
    ]

    normalized = agent._normalize_provider_facing_messages(messages, provider_id="openrouter")
    payloads = [_convert_message_to_dict(message) for message in normalized]

    assistant_payloads = [payload for payload in payloads if payload["role"] == "assistant"]
    tool_call_ids = [
        call["id"]
        for payload in assistant_payloads
        for call in payload.get("tool_calls") or []
    ]
    tool_result_ids = [payload["tool_call_id"] for payload in payloads if payload["role"] == "tool"]

    assert len(tool_call_ids) == len(set(tool_call_ids))
    assert set(tool_result_ids).issubset(set(tool_call_ids))
    assert not any(
        "<tool_call>" in str(payload.get("reasoning") or payload.get("reasoning_content") or "")
        for payload in assistant_payloads
    )
    assert all("reasoning_details" not in payload for payload in assistant_payloads)
    assert all(
        call["id"] != "openai_call_0"
        for payload in assistant_payloads
        for call in payload.get("tool_calls") or []
    )


def test_custom_tool_validation_repair_handles_missing_query(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.agent as agent
    from langchain_core.tools import StructuredTool

    def _search(query: str) -> str:
        return f"ok:{query}"

    tool = StructuredTool.from_function(
        func=_search,
        name="duckduckgo",
        description="Search the web.",
    )

    agent._install_custom_tool_validation_repair([tool], "custom_openai_lab")

    repaired = tool.invoke({})

    assert "Invalid tool call for duckduckgo" in repaired
    assert "ROW_BOT_TOOL_VALIDATION_RETRY_REQUIRED" in repaired
    assert "query" in repaired
    assert "valid JSON arguments" in repaired
    assert "properties" not in repaired
    assert len(repaired) < 280
    assert tool.invoke({"query": "latest AI news"}) == "ok:latest AI news"


def test_custom_tool_validation_repair_handles_explicit_schema_generically(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.agent as agent
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel, Field

    class _StatusInput(BaseModel):
        category: str = Field(description="Status category.")

    tool = StructuredTool.from_function(
        func=lambda category: f"status:{category}",
        name="row_bot_status",
        description="Query status.",
        args_schema=_StatusInput,
    )

    agent._install_custom_tool_validation_repair([tool], "custom_openai_lab")

    repaired = tool.invoke({})

    assert "Invalid tool call for row_bot_status" in repaired
    assert "ROW_BOT_TOOL_VALIDATION_RETRY_REQUIRED" in repaired
    assert "category" in repaired
    assert "duckduckgo" not in repaired
    assert len(repaired) < 280


def test_tool_validation_repair_is_not_installed_for_hosted_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.agent as agent
    from langchain_core.tools import StructuredTool

    def _search(query: str) -> str:
        return f"ok:{query}"

    tool = StructuredTool.from_function(
        func=_search,
        name="duckduckgo",
        description="Search the web.",
    )

    agent._install_custom_tool_validation_repair([tool], "openai")

    assert tool.handle_validation_error is False
    with pytest.raises(Exception):
        tool.invoke({})


def test_agent_graph_installs_custom_tool_validation_repair(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.agent as agent
    from langchain_core.tools import StructuredTool

    def _search(query: str) -> str:
        return f"ok:{query}"

    tool = StructuredTool.from_function(
        func=_search,
        name="duckduckgo",
        description="Search the web.",
    )
    fake_tool_obj = SimpleNamespace(
        as_langchain_tools=lambda: [tool],
        destructive_tool_names=set(),
    )

    agent.clear_agent_cache()
    monkeypatch.setattr(agent.tool_registry, "get_tool", lambda name: fake_tool_obj)
    monkeypatch.setattr(agent, "get_current_model", lambda: "model:custom_openai_lab:local")
    monkeypatch.setattr(agent, "get_llm", lambda: object())
    monkeypatch.setattr(agent, "get_context_size", lambda model_name=None: 32_768)
    monkeypatch.setattr(agent, "get_agent_system_prompt", lambda: "system")
    monkeypatch.setattr(agent, "_ensure_agent_mode_ready", lambda model_name: SimpleNamespace(
        provider_id="custom_openai_lab",
        runtime_model="local",
        capability_source="probe",
        confidence="high",
    ))
    import row_bot.plugins.registry as plugin_registry

    monkeypatch.setattr(plugin_registry, "get_langchain_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr(plugin_registry, "get_destructive_names", lambda *args, **kwargs: set())
    from row_bot.channels import registry as channel_registry

    monkeypatch.setattr(channel_registry, "running_channels", lambda: [])
    monkeypatch.setattr(agent, "_build_runtime_skill_snapshot", lambda: ((), (), False, "none"))
    monkeypatch.setattr(agent, "create_react_agent", lambda **kwargs: SimpleNamespace(**kwargs))
    agent._current_external_discovery_active_var.set(False)

    graph = agent.get_agent_graph(["duckduckgo"])

    assert graph.tools == [tool]
    assert "Invalid tool call for duckduckgo" in graph.tools[0].invoke({})
    assert "ROW_BOT_TOOL_VALIDATION_RETRY_REQUIRED" in graph.tools[0].invoke({})

    agent.clear_agent_cache()


def test_openai_pre_model_trim_keeps_standard_skill_injections(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.agent as agent
    from langchain_core.messages import HumanMessage, SystemMessage

    monkeypatch.setattr(agent, "get_context_size", lambda: 32_768)
    monkeypatch.setattr(agent, "get_current_model", lambda: "gpt-4o")
    monkeypatch.setattr(agent, "is_cloud_model", lambda model: True)
    monkeypatch.setattr(agent, "get_cloud_provider", lambda model: "openai")
    monkeypatch.setattr(agent, "is_background_workflow", lambda: False)
    import row_bot.self_knowledge as self_knowledge
    import row_bot.skills as skills
    import row_bot.plugins.registry as plugin_registry

    monkeypatch.setattr(self_knowledge, "build_static_self_knowledge_block", lambda: "SELF_SENTINEL")
    monkeypatch.setattr(self_knowledge, "build_dynamic_self_knowledge_block", lambda: "")
    monkeypatch.setattr(skills, "get_skills_prompt", lambda *args, **kwargs: "SKILL_SENTINEL")
    monkeypatch.setattr(plugin_registry, "get_skills_prompt", lambda *args, **kwargs: "PLUGIN_SENTINEL")

    agent.set_active_model_override("gpt-4o")
    try:
        result = agent._pre_model_trim({
            "execution_budget": new_execution_budget("openai-skill-trim"),
            "messages": [
                SystemMessage(content="Root system"),
                HumanMessage(content="hi"),
            ]
        })["llm_input_messages"]
    finally:
        agent.set_active_model_override("")

    system_text = "\n".join(str(msg.content) for msg in result if msg.type == "system")
    assert "SELF_SENTINEL" in system_text
    assert "SKILL_SENTINEL" in system_text
    assert "PLUGIN_SENTINEL" not in system_text


def test_custom_endpoint_history_reserves_actual_bound_schema_tokens(monkeypatch):
    import row_bot.agent as agent

    monkeypatch.setattr(agent, "_active_custom_openai_provider", lambda: True)
    agent._current_bound_tool_schema_tokens_var.set(9_000)

    assert agent._agent_history_budget_tokens(32_768) == 17_720

    monkeypatch.setattr(agent, "_active_custom_openai_provider", lambda: False)
    assert agent._agent_history_budget_tokens(32_768) == int(32_768 * 0.85)


def test_minimax_provider_raises_when_api_key_missing(monkeypatch):
    fake_langchain_anthropic = ModuleType("langchain_anthropic")

    class _FakeChatAnthropic:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake_langchain_anthropic.ChatAnthropic = _FakeChatAnthropic
    monkeypatch.setitem(sys.modules, "langchain_anthropic", fake_langchain_anthropic)
    monkeypatch.setattr(runtime, "get_provider_secret", lambda provider_id: "")

    try:
        runtime.create_chat_model("MiniMax-M2.7", provider_id="minimax")
    except ValueError as exc:
        assert "MiniMax" in str(exc)
    else:
        raise AssertionError("Expected ValueError when MiniMax API key is missing")


def test_ollama_runtime_does_not_force_reasoning(monkeypatch):
    fake_langchain_ollama = ModuleType("langchain_ollama")
    captured = {}

    class _FakeChatOllama:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake_langchain_ollama.ChatOllama = _FakeChatOllama
    monkeypatch.setitem(sys.modules, "langchain_ollama", fake_langchain_ollama)
    monkeypatch.setattr("row_bot.models._ollama_base_url", lambda: "http://127.0.0.1:11434")

    model = runtime.create_chat_model("vendor/non-tool-chat:14b", provider_id="ollama")

    assert model
    assert captured["model"] == "vendor/non-tool-chat:14b"
    assert "reasoning" not in captured


def test_ollama_runtime_enables_reasoning_for_thinking_models(monkeypatch):
    fake_langchain_ollama = ModuleType("langchain_ollama")
    captured = {}

    class _FakeChatOllama:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake_langchain_ollama.ChatOllama = _FakeChatOllama
    monkeypatch.setitem(sys.modules, "langchain_ollama", fake_langchain_ollama)
    monkeypatch.setattr("row_bot.models._ollama_base_url", lambda: "http://127.0.0.1:11434")

    model = runtime.create_chat_model("qwen3.5:30b", provider_id="ollama")

    assert model
    assert captured["model"] == "qwen3.5:30b"
    assert captured["reasoning"] is True
