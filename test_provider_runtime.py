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

    model = runtime.create_chat_model("qwen3:14b", provider_id="ollama")

    assert model.kwargs == {"model": "qwen3:14b", "reasoning": True}


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
        (None, ("127.0.0.1", 11434)),
        ("127.0.0.1", ("127.0.0.1", 11434)),
        ("127.0.0.1:11435", ("127.0.0.1", 11435)),
        ("http://127.0.0.1:11436", ("127.0.0.1", 11436)),
        ("localhost:notaport", ("localhost", 11434)),
        ("[::1]:11437", ("::1", 11437)),
    ]
    for value, expected_address in cases:
        if value is None:
            monkeypatch.delenv("OLLAMA_HOST", raising=False)
        else:
            monkeypatch.setenv("OLLAMA_HOST", value)
        calls.clear()
        assert models._ollama_reachable(timeout=0.25) is True
        assert calls == [(expected_address, 0.25)]


def test_provider_errors_normalize_unsupported_capability():
    normalized = normalize_provider_error(ValueError("This model does not support tools"))

    assert normalized.kind == ProviderErrorKind.UNSUPPORTED_CAPABILITY
    assert normalized.next_action == "Choose a model whose capability badges match this surface."