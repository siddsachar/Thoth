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