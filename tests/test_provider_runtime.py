import sys
import socket
from types import ModuleType

import providers.runtime as runtime
from providers.errors import ProviderErrorKind, normalize_provider_error


class _FakeChatOpenAI:
    calls = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls.append(kwargs)


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


def test_openai_legacy_chat_model_keeps_chat_completions_path(monkeypatch):
    fake_module = ModuleType("langchain_openai")
    fake_module.ChatOpenAI = _FakeChatOpenAI
    _FakeChatOpenAI.calls.clear()
    monkeypatch.setitem(sys.modules, "langchain_openai", fake_module)
    monkeypatch.setattr(runtime, "get_provider_secret", lambda provider_id: "sk-test")

    model = runtime.create_chat_model("gpt-4o", provider_id="openai")

    assert model.kwargs["model"] == "gpt-4o"
    assert "use_responses_api" not in model.kwargs


def test_custom_openai_endpoint_uses_configured_base_url(tmp_path, monkeypatch):
    import providers.config as provider_config
    from providers.custom import custom_provider_id, save_custom_endpoint

    fake_module = ModuleType("langchain_openai")
    fake_module.ChatOpenAI = _FakeChatOpenAI
    _FakeChatOpenAI.calls.clear()
    monkeypatch.setitem(sys.modules, "langchain_openai", fake_module)
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")

    save_custom_endpoint({
        "id": "local-vllm",
        "name": "Local vLLM",
        "base_url": "http://127.0.0.1:8000/v1/",
        "auth_required": False,
        "execution_location": "local",
    })

    model = runtime.create_chat_model("meta-llama/Llama-3.1-8B-Instruct", provider_id=custom_provider_id("local-vllm"))

    assert model.kwargs["model"] == "meta-llama/Llama-3.1-8B-Instruct"
    assert model.kwargs["base_url"] == "http://127.0.0.1:8000/v1"
    assert model.kwargs["api_key"] == "not-needed"


def test_custom_endpoint_model_syncs_into_model_facade(tmp_path, monkeypatch):
    import models
    import providers.config as provider_config
    from providers.custom import custom_provider_id, save_custom_endpoint

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    models._cloud_model_cache.pop("thoth-dummy-chat", None)
    save_custom_endpoint({
        "id": "dummy",
        "base_url": "http://127.0.0.1:8000/v1",
        "auth_required": False,
        "models": [{
            "id": "thoth-dummy-chat",
            "model_id": "thoth-dummy-chat",
            "label": "thoth-dummy-chat",
            "ctx": 8192,
            "provider": custom_provider_id("dummy"),
            "capabilities_snapshot": {"tasks": ["chat"], "input_modalities": ["text"], "output_modalities": ["text"]},
        }],
    })

    assert models.is_cloud_model("thoth-dummy-chat") is True
    assert models.get_cloud_provider("thoth-dummy-chat") == custom_provider_id("dummy")


def test_model_facade_preserves_provider_refs_for_duplicate_ids(monkeypatch):
    import models

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


def test_runtime_rejects_custom_endpoint_non_chat_model(tmp_path, monkeypatch):
    import providers.config as provider_config
    from providers.custom import custom_provider_id, save_custom_endpoint

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    save_custom_endpoint({
        "id": "dummy",
        "base_url": "http://127.0.0.1:8000/v1",
        "auth_required": False,
        "models": [{
            "id": "thoth-dummy-embedding",
            "model_id": "thoth-dummy-embedding",
            "capabilities_snapshot": {"tasks": ["embedding"], "input_modalities": ["text"], "output_modalities": ["text"]},
        }],
    })

    try:
        runtime.create_chat_model("thoth-dummy-embedding", provider_id=custom_provider_id("dummy"))
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

    model = runtime.create_chat_model("qwen3:14b", provider_id="ollama")

    assert model.kwargs == {
        "model": "qwen3:14b",
        "base_url": "http://127.0.0.1:11435",
        "reasoning": True,
    }


def test_ollama_cloud_provider_runtime_constructs_native_client(monkeypatch):
    monkeypatch.setattr(runtime, "get_provider_secret", lambda provider_id: "ollama-cloud-key")

    model = runtime.create_chat_model("gpt-oss:120b-cloud", provider_id="ollama_cloud")

    assert model.model_name == "gpt-oss:120b-cloud"
    assert model.api_key == "ollama-cloud-key"
    assert model.base_url == "https://ollama.com"


def test_clear_llm_cache_drops_cached_provider_credentials(monkeypatch):
    import models

    models.clear_llm_cache()
    keys = iter(["old-key", "new-key"])
    monkeypatch.setattr("providers.runtime.get_provider_secret", lambda provider_id: next(keys))

    first = models._get_cloud_llm("model:ollama_cloud:gpt-oss:20b")
    models.clear_llm_cache()
    second = models._get_cloud_llm("model:ollama_cloud:gpt-oss:20b")

    assert first.api_key == "old-key"
    assert second.api_key == "new-key"
    models.clear_llm_cache()


def test_ollama_cloud_runtime_posts_native_chat_request():
    from langchain_core.messages import HumanMessage
    from providers.transports.ollama_cloud import ChatOllamaCloud

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
    from providers.transports.ollama_cloud import normalize_ollama_cloud_model_name

    assert normalize_ollama_cloud_model_name("gpt-oss:120b-cloud") == "gpt-oss:120b"
    assert normalize_ollama_cloud_model_name("gemma4:31b-cloud") == "gemma4:31b"
    assert normalize_ollama_cloud_model_name("gpt-oss:cloud") == "gpt-oss"
    assert normalize_ollama_cloud_model_name("kimi-k2:thinking") == "kimi-k2:thinking"


def test_ollama_cloud_runtime_normalizes_bearer_prefix():
    from providers.transports.ollama_cloud import ChatOllamaCloud

    model = ChatOllamaCloud(model_name="gpt-oss:120b", api_key="Bearer test-key")

    assert model._headers()["Authorization"] == "Bearer test-key"


def test_ollama_cloud_401_has_actionable_message():
    from langchain_core.messages import HumanMessage
    from providers.transports.ollama_cloud import ChatOllamaCloud

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
    from providers.transports.ollama_cloud import ChatOllamaCloud

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
    from providers.transports.ollama_cloud import ChatOllamaCloud

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
    from providers.transports.ollama_cloud import ChatOllamaCloud

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
    from providers.transports.ollama_cloud import ChatOllamaCloud

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
    from providers.transports.ollama_cloud import ChatOllamaCloud

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
    from providers.transports.ollama_cloud import ChatOllamaCloud

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
    from providers.transports.ollama_cloud import ChatOllamaCloud

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
    import models

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
    from providers.transports.ollama_cloud import ChatOllamaCloud

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
    import models

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
    import models

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
    fake_langchain_anthropic = ModuleType("langchain_anthropic")

    class _FakeChatAnthropic:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake_langchain_anthropic.ChatAnthropic = _FakeChatAnthropic
    monkeypatch.setitem(sys.modules, "langchain_anthropic", fake_langchain_anthropic)
    monkeypatch.setattr(runtime, "get_provider_secret", lambda provider_id: "test-minimax-key")

    model = runtime.create_chat_model("MiniMax-M2.7", provider_id="minimax")

    assert model.kwargs["model"] == "MiniMax-M2.7"
    assert model.kwargs["api_key"] == "test-minimax-key"
    assert model.kwargs["base_url"] == "https://api.minimax.io/anthropic"


def test_minimax_model_facade_recognizes_static_catalog(monkeypatch):
    import api_keys
    import models

    old_cache = dict(models._cloud_model_cache)
    monkeypatch.setattr(api_keys, "get_key", lambda key: "test-minimax-key" if key == "MINIMAX_API_KEY" else "")
    try:
        models._cloud_model_cache.clear()
        count = models.fetch_cloud_models("minimax")

        assert count == 7
        assert models.is_cloud_model("MiniMax-M2.7") is True
        assert models.get_cloud_provider("MiniMax-M2.7") == "minimax"
        assert models.get_cloud_model_context("MiniMax-M2.7") == 204_800
        assert models.get_provider_emoji("MiniMax-M2.7") == "M"
        assert models._cloud_model_cache["MiniMax-M2.7"]["provider"] == "minimax"
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(old_cache)


def test_ollama_cloud_model_facade_fetches_direct_catalog(monkeypatch):
    import api_keys
    import httpx
    import models

    old_cache = dict(models._cloud_model_cache)

    class _Response:
        status_code = 200
        text = "{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {"models": [{"name": "gpt-oss:120b-cloud"}]}

    monkeypatch.setattr(api_keys, "get_key", lambda key: "test-ollama-key" if key == "OLLAMA_API_KEY" else "")
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
    import models

    assert models.is_cloud_model("model:ollama:gpt-oss:120b-cloud") is True
    assert models.get_cloud_provider("model:ollama:gpt-oss:120b-cloud") == "ollama"


def test_minimax_validation_treats_insufficient_balance_as_accepted_key(monkeypatch):
    import httpx
    import models

    captured = {}

    class _Response:
        status_code = 500
        text = '{"type":"error","error":{"type":"api_error","message":"insufficient balance (1008)"}}'

    def _fake_post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return _Response()

    monkeypatch.setattr(httpx, "post", _fake_post)

    assert models.validate_minimax_key("test-minimax-key") is True
    assert captured["url"] == "https://api.minimax.io/anthropic/v1/messages"
    assert captured["json"]["model"] == "MiniMax-M2.7"


def test_minimax_validation_rejects_auth_failure(monkeypatch):
    import httpx
    import models

    class _Response:
        status_code = 401
        text = '{"type":"error","error":{"message":"invalid api key"}}'

    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: _Response())

    assert models.validate_minimax_key("bad-key") is False


def test_minimax_pre_model_trim_uses_anthropic_message_consolidation(monkeypatch):
    import agent
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    monkeypatch.setattr(agent, "get_context_size", lambda: 200_000)
    monkeypatch.setattr(agent, "trim_messages", lambda messages, **kwargs: list(messages))
    monkeypatch.setattr(agent, "get_current_model", lambda: "MiniMax-M2.7")
    monkeypatch.setattr(agent, "is_cloud_model", lambda model: True)
    monkeypatch.setattr(agent, "get_cloud_provider", lambda model: "minimax")
    monkeypatch.setattr(agent, "is_background_workflow", lambda: False)

    agent.set_active_model_override("MiniMax-M2.7")
    try:
        result = agent._pre_model_trim({
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
