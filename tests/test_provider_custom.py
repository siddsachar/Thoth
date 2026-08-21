import row_bot.api_keys as api_keys
import base64
import io
import pytest
import row_bot.models as models
import row_bot.providers.auth_store as auth_store
import row_bot.providers.config as provider_config
from row_bot.providers.capabilities import model_supports_surface
from row_bot.providers.custom import (
    CUSTOM_ENDPOINT_PROFILES,
    custom_model_cache_entries,
    custom_probe_for_model,
    custom_provider_id,
    delete_custom_endpoint,
    get_custom_endpoint,
    list_custom_provider_definitions,
    model_infos_from_openai_compatible_catalog,
    probe_custom_endpoint,
    refresh_custom_endpoint_models,
    save_custom_endpoint,
)
from row_bot.providers.selection import add_quick_choice_for_model, list_quick_choices, model_choice_value
from row_bot.secret_store import _set_backend_for_tests


class _FailingKeyring:
    def get_password(self, service, account):
        raise RuntimeError("No recommended backend was available")

    def set_password(self, service, account, value):
        raise RuntimeError("No recommended backend was available")

    def delete_password(self, service, account):
        raise RuntimeError("No recommended backend was available")


def test_custom_endpoint_catalog_reads_models_payload_and_common_capability_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    endpoint = {
        "id": "generic",
        "name": "Generic",
        "base_url": "http://127.0.0.1:8000/v1",
        "auth_required": False,
    }
    payload = {
        "models": [{
            "id": "custom-vision-tool-model",
            "supports_vision": True,
            "supports_function_calling": True,
            "max_input_tokens": 131072,
        }]
    }

    infos = model_infos_from_openai_compatible_catalog(endpoint, payload)

    assert [info.model_id for info in infos] == ["custom-vision-tool-model"]
    assert infos[0].context_window == 131072
    assert model_supports_surface(infos[0], "vision") is True
    assert infos[0].tool_calling is True


def test_custom_endpoint_catalog_parses_vllm_models(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    endpoint = {
        "id": "local-vllm",
        "name": "Local vLLM",
        "base_url": "http://127.0.0.1:8000/v1",
        "auth_required": False,
        "execution_location": "local",
    }
    payload = {
        "data": [
            {"id": "meta-llama/Llama-3.1-8B-Instruct", "context_length": 131072},
            {"id": "text-embedding-3-large"},
        ]
    }

    infos = model_infos_from_openai_compatible_catalog(endpoint, payload)

    assert [info.model_id for info in infos] == ["meta-llama/Llama-3.1-8B-Instruct", "text-embedding-3-large"]
    assert infos[0].provider_id == custom_provider_id("local-vllm")
    assert model_supports_surface(infos[0], "chat") is True
    assert model_supports_surface(infos[1], "chat") is False
    assert model_supports_surface(infos[1], "embeddings") is True


def test_custom_endpoint_catalog_infers_lmstudio_vision_models(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    endpoint = {
        "id": "lmstudio",
        "name": "LM Studio",
        "base_url": "http://127.0.0.1:1234/v1",
        "auth_required": False,
        "execution_location": "local",
    }
    payload = {"data": [{"id": "llava-phi3:3.8b"}, {"id": "google/gemma-3-4b-it"}, {"id": "qwen2.5-vl-7b-instruct"}]}

    infos = model_infos_from_openai_compatible_catalog(endpoint, payload)

    assert [info.model_id for info in infos] == ["llava-phi3:3.8b", "google/gemma-3-4b-it", "qwen2.5-vl-7b-instruct"]
    assert all(model_supports_surface(info, "vision") for info in infos)


def test_custom_endpoint_catalog_applies_manual_vision_capabilities(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    endpoint = {
        "id": "manual-vision",
        "name": "Manual Vision",
        "base_url": "http://127.0.0.1:1234/v1",
        "auth_required": False,
        "manual_capabilities": {"vision": True},
    }
    payload = {"data": [{"id": "local-model-with-sparse-metadata"}]}

    infos = model_infos_from_openai_compatible_catalog(endpoint, payload)

    assert model_supports_surface(infos[0], "vision") is True


def test_custom_endpoint_catalog_manual_vision_false_overrides_name_inference(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    endpoint = {
        "id": "manual-text",
        "name": "Manual Text",
        "base_url": "http://127.0.0.1:1234/v1",
        "auth_required": False,
        "manual_capabilities": {"vision": False},
    }
    payload = {"data": [{"id": "qwen2.5-vl-7b-instruct"}]}

    infos = model_infos_from_openai_compatible_catalog(endpoint, payload)

    assert model_supports_surface(infos[0], "vision") is False


def test_custom_endpoint_catalog_applies_manual_context_window(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    endpoint = {
        "id": "manual-context",
        "name": "Manual Context",
        "base_url": "http://127.0.0.1:1234/v1",
        "auth_required": False,
        "manual_capabilities": {"context_window": 32768},
    }
    payload = {"data": [{"id": "local-model-with-sparse-metadata"}]}

    infos = model_infos_from_openai_compatible_catalog(endpoint, payload)

    assert infos[0].context_window == 32768


def test_custom_endpoint_catalog_reads_llamacpp_nested_context(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    endpoint = {
        "id": "llamacpp",
        "name": "llama.cpp",
        "profile": "llama_cpp",
        "base_url": "http://127.0.0.1:8080/v1",
        "auth_required": False,
        "execution_location": "local",
    }
    payload = {
        "data": [{
            "id": "qwen3.5-9b",
            "owned_by": "llamacpp",
            "meta": {
                "n_ctx": 32768,
                "n_ctx_train": 262144,
            },
        }]
    }

    infos = model_infos_from_openai_compatible_catalog(endpoint, payload)

    assert infos[0].context_window == 32768


def test_custom_endpoint_catalog_keeps_unknown_context_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    endpoint = {
        "id": "generic",
        "name": "Generic",
        "base_url": "http://127.0.0.1:8000/v1",
        "auth_required": False,
        "unknown_context_fallback": 4096,
    }
    payload = {"data": [{"id": "sparse-local-chat"}]}

    infos = model_infos_from_openai_compatible_catalog(endpoint, payload)

    assert infos[0].context_window == 0


def test_custom_endpoint_catalog_merges_lmstudio_native_context_and_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    endpoint = {
        "id": "lm-studio",
        "name": "LM Studio",
        "profile": "lmstudio",
        "base_url": "http://127.0.0.1:1234/v1",
        "auth_required": False,
    }
    payload = {"data": [{"id": "qwen/qwen3.5-9b"}]}
    native = {
        "qwen/qwen3.5-9b": {
            "max_context_length": 262144,
            "capabilities": {"trained_for_tool_use": True, "vision": True},
        },
    }

    infos = model_infos_from_openai_compatible_catalog(endpoint, payload, native_metadata_by_model=native)

    assert infos[0].context_window == 262144
    assert infos[0].tool_calling is True
    assert "tool_calling" in infos[0].capabilities


def test_custom_endpoint_definitions_include_saved_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")

    save_custom_endpoint({
        "id": "llama-cpp",
        "name": "llama.cpp",
        "base_url": "http://127.0.0.1:8080/v1/",
        "auth_required": False,
        "execution_location": "local",
    })

    definitions = list_custom_provider_definitions()

    assert definitions[0].id == custom_provider_id("llama-cpp")
    assert definitions[0].base_url == "http://127.0.0.1:8080/v1"
    assert definitions[0].risk_label == "local_private"


def test_custom_endpoint_profile_defaults_are_persisted(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")

    save_custom_endpoint({
        "id": "omlx",
        "name": "oMLX",
        "base_url": "http://127.0.0.1:8000/v1",
        "profile": "omlx",
        "auth_required": False,
        "execution_location": "local",
    })

    endpoint = get_custom_endpoint("omlx")

    assert endpoint["profile"] == "omlx"
    assert endpoint["message_content_mode"] == CUSTOM_ENDPOINT_PROFILES["omlx"]["message_content_mode"]
    assert endpoint["tool_history_mode"] == "native_required"
    assert endpoint["drop_unsupported_params"] is True
    assert endpoint["supports_reasoning_replay"] is False
    assert endpoint["reasoning_mode"] == "auto"


def test_custom_endpoint_reasoning_capability_config_is_persisted(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")

    save_custom_endpoint({
        "id": "qwen",
        "name": "Qwen Endpoint",
        "base_url": "http://127.0.0.1:8000/v1",
        "profile": "llama_cpp",
        "auth_required": False,
        "supports_reasoning_replay": True,
        "reasoning_mode": "off",
        "thinking_budget": "1024",
        "preserve_thinking": False,
    })

    endpoint = get_custom_endpoint("qwen")

    assert endpoint["supports_reasoning_content"] is True
    assert endpoint["supports_reasoning_replay"] is True
    assert endpoint["reasoning_mode"] == "off"
    assert endpoint["thinking_budget"] == 1024
    assert endpoint["preserve_thinking"] is False


def test_custom_endpoint_save_no_auth_removes_stored_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    import row_bot.providers.custom as custom

    deleted = []
    monkeypatch.setattr(custom, "delete_provider_secret", lambda provider_id, key: deleted.append((provider_id, key)))

    save_custom_endpoint({
        "id": "dummy",
        "name": "Dummy",
        "base_url": "http://127.0.0.1:8000/v1",
        "auth_required": False,
    })

    assert deleted == [("custom_openai_dummy", "api_key")]


def test_server_managed_profiles_do_not_send_context_as_request_param(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")

    save_custom_endpoint({
        "id": "vllm",
        "name": "vLLM",
        "base_url": "http://127.0.0.1:8000/v1",
        "profile": "vllm",
        "auth_required": False,
        "execution_location": "local",
    })
    provider_config.save_provider_config({
        "custom_endpoints": [
            *provider_config.load_provider_config().get("custom_endpoints", []),
            {
                "id": "sglang",
                "name": "SGLang",
                "base_url": "http://127.0.0.1:30000/v1",
                "profile": "sglang",
                "supports_runtime_context_override": True,
                "context_param_name": "context_length",
                "auth_required": False,
                "execution_location": "local",
            },
            {
                "id": "llama-cpp",
                "name": "llama.cpp",
                "base_url": "http://127.0.0.1:8080/v1",
                "profile": "llama_cpp",
                "supports_runtime_context_override": True,
                "context_param_name": "n_ctx",
                "auth_required": False,
                "execution_location": "local",
            },
        ]
    })

    vllm = get_custom_endpoint("vllm")
    sglang = get_custom_endpoint("sglang")
    llama_cpp = get_custom_endpoint("llama-cpp")

    assert vllm["supports_runtime_context_override"] is False
    assert vllm["context_param_name"] == ""
    assert sglang["supports_runtime_context_override"] is False
    assert sglang["context_param_name"] == ""
    assert llama_cpp["supports_runtime_context_override"] is False
    assert llama_cpp["context_param_name"] == ""


def test_litellm_profile_uses_system_first_for_local_proxy_templates(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")

    save_custom_endpoint({
        "id": "litellm-local",
        "name": "LiteLLM Local",
        "base_url": "http://127.0.0.1:4000/v1",
        "profile": "litellm",
        "auth_required": False,
        "execution_location": "local",
    })

    endpoint = get_custom_endpoint("litellm-local")

    assert endpoint["system_message_mode"] == "system_first"


def test_existing_litellm_provider_default_system_mode_is_upgraded(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    provider_config.save_provider_config({
        "custom_endpoints": [{
            "id": "litellm-local",
            "name": "LiteLLM Local",
            "base_url": "http://127.0.0.1:4000/v1",
            "profile": "litellm",
            "system_message_mode": "provider_default",
            "auth_required": False,
            "execution_location": "local",
        }]
    })

    endpoint = get_custom_endpoint("litellm-local")

    assert endpoint["system_message_mode"] == "system_first"


def test_custom_endpoint_delete_removes_config_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    save_custom_endpoint({"id": "localai", "base_url": "http://127.0.0.1:8080/v1", "auth_required": False})

    delete_custom_endpoint("localai")

    assert get_custom_endpoint("localai") is None


def test_custom_endpoint_delete_removes_only_matching_provider_quick_choices(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})
    save_custom_endpoint({
        "id": "old",
        "base_url": "http://127.0.0.1:8000/v1",
        "auth_required": False,
        "models": [{"id": "shared-model", "model_id": "shared-model"}],
    })
    save_custom_endpoint({
        "id": "new",
        "base_url": "http://127.0.0.1:9000/v1",
        "auth_required": False,
        "models": [{"id": "shared-model", "model_id": "shared-model"}],
    })
    add_quick_choice_for_model("shared-model", provider_id=custom_provider_id("old"), display_name="Old Shared")
    add_quick_choice_for_model("shared-model", provider_id=custom_provider_id("new"), display_name="New Shared")

    removed = delete_custom_endpoint("old")

    assert removed == 1
    assert get_custom_endpoint("old") is None
    assert [choice["id"] for choice in list_quick_choices("")] == [
        f"model:{custom_provider_id('new')}:shared-model"
    ]


def test_custom_endpoint_delete_resets_stale_current_model(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})
    monkeypatch.setattr(models, "_SETTINGS_PATH", tmp_path / "model_settings.json")
    monkeypatch.setattr(models, "_current_model", f"model:{custom_provider_id('old')}:old-model")
    save_custom_endpoint({
        "id": "old",
        "base_url": "http://127.0.0.1:8000/v1",
        "auth_required": False,
        "models": [{"id": "old-model", "model_id": "old-model"}],
    })

    delete_custom_endpoint("old")

    assert models.get_current_model() == model_choice_value(models.DEFAULT_MODEL, provider_id="ollama")


def test_get_current_model_resets_previously_deleted_custom_default(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})
    monkeypatch.setattr(models, "_SETTINGS_PATH", tmp_path / "model_settings.json")
    monkeypatch.setattr(models, "_current_model", "model:custom_openai_deleted:ghost-model")

    assert models.get_current_model() == model_choice_value(models.DEFAULT_MODEL, provider_id="ollama")


def test_custom_endpoint_refresh_persists_model_cache_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    save_custom_endpoint({"id": "dummy", "base_url": "http://127.0.0.1:8000/v1", "auth_required": False})

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "row-bot-dummy-chat", "context_length": 8192}]}

    import httpx
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: _Response())

    infos = refresh_custom_endpoint_models("dummy")
    entries = custom_model_cache_entries()

    assert [info.model_id for info in infos] == ["row-bot-dummy-chat"]
    assert entries["row-bot-dummy-chat"]["provider"] == custom_provider_id("dummy")
    assert entries["row-bot-dummy-chat"]["capabilities_snapshot"]["tasks"] == ["chat"]


def test_custom_endpoint_refresh_uses_session_secret_when_keyring_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    auth_store._clear_session_secrets_for_tests()
    _set_backend_for_tests(_FailingKeyring())
    try:
        save_custom_endpoint({
            "id": "headless",
            "base_url": "http://127.0.0.1:8000/v1",
            "auth_required": True,
            "api_key": "sk-local-session",
        })

        class _Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"data": [{"id": "row-bot-headless-chat", "context_length": 8192}]}

        seen_headers = []

        import httpx

        def _get(url, *args, **kwargs):
            seen_headers.append(dict(kwargs.get("headers") or {}))
            return _Response()

        monkeypatch.setattr(httpx, "get", _get)

        infos = refresh_custom_endpoint_models("headless")

        assert [info.model_id for info in infos] == ["row-bot-headless-chat"]
        assert seen_headers[0]["Authorization"] == "Bearer sk-local-session"
        assert auth_store.provider_secret_status(custom_provider_id("headless"))["source"] == "session"
        assert "sk-local-session" not in (tmp_path / "providers.json").read_text(encoding="utf-8")
    finally:
        auth_store._clear_session_secrets_for_tests()
        _set_backend_for_tests(None)


def test_auth_required_custom_endpoint_refresh_fails_before_http_without_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    auth_store._clear_session_secrets_for_tests()
    save_custom_endpoint({
        "id": "missing-key",
        "base_url": "http://127.0.0.1:8000/v1",
        "auth_required": True,
    })

    import httpx

    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("HTTP should not be called")))

    with pytest.raises(ValueError, match="requires an API key"):
        refresh_custom_endpoint_models("missing-key")


def test_custom_endpoint_refresh_prunes_removed_model_quick_choices(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})
    save_custom_endpoint({
        "id": "dummy",
        "base_url": "http://127.0.0.1:8000/v1",
        "auth_required": False,
        "models": [
            {"id": "old-chat", "model_id": "old-chat"},
            {"id": "new-chat", "model_id": "new-chat"},
        ],
    })
    provider_id = custom_provider_id("dummy")
    add_quick_choice_for_model("old-chat", provider_id=provider_id)
    add_quick_choice_for_model("new-chat", provider_id=provider_id)

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "new-chat", "context_length": 8192}]}

    import httpx
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: _Response())

    infos = refresh_custom_endpoint_models("dummy")

    assert [info.model_id for info in infos] == ["new-chat"]
    assert getattr(infos, "stale_pin_count") == 1
    assert [choice["id"] for choice in list_quick_choices("")] == [f"model:{provider_id}:new-chat"]


def test_custom_endpoint_refresh_resets_default_for_removed_model(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})
    monkeypatch.setattr(models, "_SETTINGS_PATH", tmp_path / "model_settings.json")
    provider_id = custom_provider_id("dummy")
    monkeypatch.setattr(models, "_current_model", f"model:{provider_id}:old-chat")
    save_custom_endpoint({
        "id": "dummy",
        "base_url": "http://127.0.0.1:8000/v1",
        "auth_required": False,
        "models": [
            {"id": "old-chat", "model_id": "old-chat"},
            {"id": "new-chat", "model_id": "new-chat"},
        ],
    })
    add_quick_choice_for_model("new-chat", provider_id=provider_id)

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "new-chat", "context_length": 8192}]}

    import httpx
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: _Response())

    infos = refresh_custom_endpoint_models("dummy")

    assert getattr(infos, "default_reset") is True
    assert models.get_current_model() == f"model:{provider_id}:new-chat"


def test_custom_endpoint_refresh_resets_default_even_without_previous_model_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})
    monkeypatch.setattr(models, "_SETTINGS_PATH", tmp_path / "model_settings.json")
    provider_id = custom_provider_id("dummy")
    monkeypatch.setattr(models, "_current_model", f"model:{provider_id}:old-chat")
    save_custom_endpoint({
        "id": "dummy",
        "base_url": "http://127.0.0.1:8000/v1",
        "auth_required": False,
    })
    add_quick_choice_for_model("new-chat", provider_id=provider_id)

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "new-chat", "context_length": 8192}]}

    import httpx
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: _Response())

    infos = refresh_custom_endpoint_models("dummy")

    assert getattr(infos, "default_reset") is True
    assert models.get_current_model() == f"model:{provider_id}:new-chat"


def test_custom_model_cache_sync_prunes_removed_custom_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(models, "_cloud_model_cache", {
        "old-chat": {"provider": custom_provider_id("old"), "label": "Old Chat"},
        "openai-chat": {"provider": "openai", "label": "OpenAI Chat"},
    })

    models._sync_custom_model_cache()

    assert models._cloud_model_cache == {
        "openai-chat": {"provider": "openai", "label": "OpenAI Chat"},
    }


def test_llamacpp_refresh_reads_props_context(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    save_custom_endpoint({
        "id": "llama-cpp",
        "base_url": "http://127.0.0.1:8080/v1",
        "profile": "llama_cpp",
        "auth_required": False,
    })

    class _Response:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    import httpx

    def _get(url, *args, **kwargs):
        if str(url).endswith("/props"):
            return _Response({
                "model_alias": "qwen3.5-9b",
                "default_generation_settings": {"n_ctx": 32768},
            })
        return _Response({"data": [{"id": "qwen3.5-9b"}]})

    monkeypatch.setattr(httpx, "get", _get)

    infos = refresh_custom_endpoint_models("llama-cpp")
    endpoint = get_custom_endpoint("llama-cpp")

    assert infos[0].context_window == 32768
    assert endpoint["models"][0]["context_window"] == 32768
    assert endpoint["models"][0]["ctx"] == 32768


def test_llamacpp_refresh_reads_props_vision_modalities(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    save_custom_endpoint({
        "id": "llama-cpp",
        "base_url": "http://127.0.0.1:8081/v1",
        "profile": "llama_cpp",
        "auth_required": False,
    })

    class _Response:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    import httpx

    def _get(url, *args, **kwargs):
        if str(url).endswith("/props"):
            return _Response({
                "model_alias": "qwen3.5-9b",
                "default_generation_settings": {"n_ctx": 65536},
                "modalities": {"vision": True, "audio": False},
            })
        return _Response({
            "data": [{
                "id": "qwen3.5-9b",
                "owned_by": "llamacpp",
                "meta": {"n_ctx": 65536},
            }]
        })

    monkeypatch.setattr(httpx, "get", _get)

    infos = refresh_custom_endpoint_models("llama-cpp")
    endpoint = get_custom_endpoint("llama-cpp")
    snapshot = endpoint["models"][0]["capabilities_snapshot"]

    assert model_supports_surface(infos[0], "vision") is True
    assert endpoint["models"][0]["vision"] is True
    assert "vision" in snapshot["capabilities"]
    assert "image" in snapshot["input_modalities"]


def test_litellm_refresh_reads_model_group_info_vision(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    save_custom_endpoint({
        "id": "litellm-local",
        "base_url": "http://127.0.0.1:4000/v1",
        "profile": "litellm",
        "auth_required": False,
    })

    class _Response:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    import httpx

    def _get(url, *args, **kwargs):
        if str(url).endswith("/model_group/info"):
            return _Response({
                "data": [{
                    "model_group": "llava-hf",
                    "supports_vision": True,
                    "supports_function_calling": True,
                    "max_input_tokens": 128000,
                }]
            })
        return _Response({"data": [{"id": "llava-hf"}]})

    monkeypatch.setattr(httpx, "get", _get)

    infos = refresh_custom_endpoint_models("litellm-local")
    endpoint = get_custom_endpoint("litellm-local")
    snapshot = endpoint["models"][0]["capabilities_snapshot"]

    assert infos[0].context_window == 128000
    assert model_supports_surface(infos[0], "vision") is True
    assert infos[0].tool_calling is True
    assert "image" in snapshot["input_modalities"]


def test_sglang_refresh_reads_native_image_understanding(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    save_custom_endpoint({
        "id": "sglang",
        "base_url": "http://127.0.0.1:30000/v1",
        "profile": "sglang",
        "auth_required": False,
    })

    class _Response:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    import httpx

    def _get(url, *args, **kwargs):
        if str(url).endswith("/get_model_info"):
            return _Response({
                "model_path": "Qwen/Qwen2.5-VL-7B-Instruct",
                "is_generation": True,
                "has_image_understanding": True,
                "has_audio_understanding": False,
            })
        return _Response({"data": [{"id": "Qwen/Qwen2.5-VL-7B-Instruct"}]})

    monkeypatch.setattr(httpx, "get", _get)

    infos = refresh_custom_endpoint_models("sglang")

    assert model_supports_surface(infos[0], "vision") is True


def test_localai_refresh_reads_config_json_context_and_backend_vision(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    save_custom_endpoint({
        "id": "localai",
        "base_url": "http://127.0.0.1:8080/v1",
        "profile": "localai",
        "auth_required": False,
    })

    class _Response:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    import httpx

    def _get(url, *args, **kwargs):
        if "/api/models/config-json/" in str(url):
            return _Response({
                "name": "my-vlm",
                "backend": "mlx-vlm",
                "context_size": 16384,
            })
        return _Response({"data": [{"id": "my-vlm"}]})

    monkeypatch.setattr(httpx, "get", _get)

    infos = refresh_custom_endpoint_models("localai")

    assert infos[0].context_window == 16384
    assert model_supports_surface(infos[0], "vision") is True


def test_custom_endpoint_probe_persists_models_and_probe_result(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    save_custom_endpoint({"id": "dummy", "base_url": "http://127.0.0.1:8000/v1", "auth_required": False})

    class _GetResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "row-bot-dummy-chat", "max_model_len": 16384}]}

    class _PostResponse:
        def __init__(self, payload=None):
            self._payload = payload or {"choices": [{"message": {"content": "pong"}}]}
            self._stream_tool = False

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def iter_lines(self):
            if self._stream_tool:
                yield b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_stream_1","type":"function","function":{"name":"row_bot_probe_echo"}}]}}]}'
                yield b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"value\\":"}}]}}]}'
                yield b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"ok\\"}"}}]}}]}'
                yield b"data: [DONE]"
                return
            yield b'data: {"choices":[{"delta":{"content":"p"}}]}'

    import httpx
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: _GetResponse())
    def _post(*args, **kwargs):
        body = kwargs.get("json") or {}
        if body.get("tools"):
            return _PostResponse({
                "choices": [{
                    "message": {
                        "content": "",
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "row_bot_probe_echo", "arguments": "{\"value\":\"ok\"}"},
                        }],
                    },
                }],
            })
        return _PostResponse()

    monkeypatch.setattr(httpx, "post", _post)
    def _stream(*args, **kwargs):
        response = _PostResponse()
        response._stream_tool = bool((kwargs.get("json") or {}).get("tools"))
        return response

    monkeypatch.setattr(httpx, "stream", _stream)

    probe = probe_custom_endpoint("dummy")
    endpoint = get_custom_endpoint("dummy")

    assert probe["ok"] is True
    assert probe["agent_ok"] is True
    assert probe["chat_only_ok"] is False
    assert probe["classification"] == "agent_ready"
    assert probe["models_ok"] is True
    assert probe["chat_ok"] is True
    assert probe["tool_calling"] is True
    assert probe["tool_round_trip"] is True
    assert probe["streaming_ok"] is True
    assert probe["streaming_tool_calling"] is True
    assert probe["model_id"] == "row-bot-dummy-chat"
    assert probe["context_window"] == 16384
    assert endpoint["last_probe"]["ok"] is True
    assert endpoint["last_probe"]["streaming_tool_calling"] is True
    assert endpoint["last_probe"]["classification"] == "agent_ready"
    assert endpoint["last_probe"]["model_id"] == "row-bot-dummy-chat"
    assert endpoint["models"][0]["context_window"] == 16384


def test_custom_probe_lookup_is_model_scoped_and_supports_legacy_single_model():
    endpoint = {
        "id": "lab",
        "models": [
            {"model_id": "model-a"},
            {"model_id": "model-b"},
        ],
        "last_probe": {
            "model_id": "model-a",
            "chat_ok": True,
            "tool_calling": True,
            "tool_round_trip": True,
        },
    }

    assert custom_probe_for_model(endpoint, "model-a")["tool_round_trip"] is True
    assert custom_probe_for_model(endpoint, "model-b") == {}

    legacy = {
        "id": "legacy",
        "models": [{"model_id": "only-model"}],
        "last_probe": {"chat_ok": True, "tool_calling": False},
    }
    assert custom_probe_for_model(legacy, "only-model")["chat_ok"] is True
    assert custom_probe_for_model(legacy, "different-model") == {}


def test_custom_endpoint_probe_records_vision_success_for_vision_model(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    save_custom_endpoint({"id": "dummy", "base_url": "http://127.0.0.1:8000/v1", "auth_required": False})

    class _GetResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "qwen2.5-vl-7b-instruct", "max_model_len": 16384}]}

    class _PostResponse:
        def __init__(self, payload=None):
            self._payload = payload or {"choices": [{"message": {"content": "pong"}}]}

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def iter_lines(self):
            yield b'data: {"choices":[{"delta":{"content":"p"}}]}'

    vision_bodies = []

    import httpx
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: _GetResponse())

    def _post(*args, **kwargs):
        body = kwargs.get("json") or {}
        messages = body.get("messages") or []
        content = messages[0].get("content") if messages else None
        if isinstance(content, list):
            vision_bodies.append(body)
            return _PostResponse({"choices": [{"message": {"content": "The dominant color is red."}}]})
        if body.get("tools"):
            return _PostResponse({
                "choices": [{
                    "message": {
                        "content": "",
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "row_bot_probe_echo", "arguments": "{\"value\":\"ok\"}"},
                        }],
                    },
                }],
            })
        return _PostResponse()

    monkeypatch.setattr(httpx, "post", _post)
    monkeypatch.setattr(httpx, "stream", lambda *args, **kwargs: _PostResponse())

    probe = probe_custom_endpoint("dummy")
    endpoint = get_custom_endpoint("dummy")

    assert probe["ok"] is True
    assert probe["vision_ok"] is True
    assert probe["vision_error"] == ""
    assert probe["vision_probe_response"] == "the dominant color is red."
    assert probe["vision_model"] == "qwen2.5-vl-7b-instruct"
    assert probe["vision_content_format"] == "openai_image_url"
    assert endpoint["last_probe"]["vision_ok"] is True
    assert endpoint["last_probe"]["vision_probed"] is True
    assert vision_bodies
    assert vision_bodies[0]["messages"][0]["content"][1]["type"] == "image_url"


def test_custom_endpoint_vision_probe_image_is_red():
    from PIL import Image
    from row_bot.providers.custom import VISION_PROBE_IMAGE_DATA_URL

    encoded = VISION_PROBE_IMAGE_DATA_URL.split(",", 1)[1]
    image = Image.open(io.BytesIO(base64.b64decode(encoded))).convert("RGB")

    assert image.size == (1, 1)
    assert image.getpixel((0, 0)) == (255, 0, 0)


def test_custom_endpoint_vision_probe_budget_allows_thinking_models():
    from row_bot.providers.custom import VISION_PROBE_MAX_TOKENS

    assert VISION_PROBE_MAX_TOKENS >= 256


def test_custom_endpoint_probe_records_vision_failure_without_blocking_agent_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    save_custom_endpoint({
        "id": "manual-vision",
        "base_url": "http://127.0.0.1:8000/v1",
        "auth_required": False,
        "manual_capabilities": {"vision": True},
    })

    class _GetResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "plain-local-chat"}]}

    class _PostResponse:
        def __init__(self, payload=None):
            self._payload = payload or {"choices": [{"message": {"content": "pong"}}]}

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def iter_lines(self):
            yield b'data: {"choices":[{"delta":{"content":"p"}}]}'

    class _VisionFailure(_PostResponse):
        def raise_for_status(self):
            raise RuntimeError("image input unsupported")

    import httpx
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: _GetResponse())

    def _post(*args, **kwargs):
        body = kwargs.get("json") or {}
        messages = body.get("messages") or []
        content = messages[0].get("content") if messages else None
        if isinstance(content, list):
            return _VisionFailure()
        if body.get("tools"):
            return _PostResponse({
                "choices": [{
                    "message": {
                        "content": "",
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "row_bot_probe_echo", "arguments": "{\"value\":\"ok\"}"},
                        }],
                    },
                }],
            })
        return _PostResponse()

    monkeypatch.setattr(httpx, "post", _post)
    monkeypatch.setattr(httpx, "stream", lambda *args, **kwargs: _PostResponse())

    probe = probe_custom_endpoint("manual-vision")

    assert probe["ok"] is True
    assert probe["agent_ok"] is True
    assert probe["vision_ok"] is False
    assert "image input unsupported" in probe["vision_error"]
    assert "vision:" in "; ".join(probe["errors"])


def test_custom_endpoint_probe_treats_empty_vision_probe_response_as_inconclusive(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    save_custom_endpoint({"id": "dummy", "base_url": "http://127.0.0.1:8000/v1", "auth_required": False})

    class _GetResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "qwen2.5-vl-7b-instruct"}]}

    class _PostResponse:
        def __init__(self, payload=None):
            self._payload = payload or {"choices": [{"message": {"content": "pong"}}]}

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def iter_lines(self):
            yield b'data: {"choices":[{"delta":{"content":"p"}}]}'

    import httpx
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: _GetResponse())

    def _post(*args, **kwargs):
        body = kwargs.get("json") or {}
        messages = body.get("messages") or []
        content = messages[0].get("content") if messages else None
        if isinstance(content, list):
            return _PostResponse({"choices": [{"message": {"content": "", "reasoning_content": "thinking"}}]})
        if body.get("tools"):
            return _PostResponse({
                "choices": [{
                    "message": {
                        "content": "",
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "row_bot_probe_echo", "arguments": "{\"value\":\"ok\"}"},
                        }],
                    },
                }],
            })
        return _PostResponse()

    monkeypatch.setattr(httpx, "post", _post)
    monkeypatch.setattr(httpx, "stream", lambda *args, **kwargs: _PostResponse())

    probe = probe_custom_endpoint("dummy")

    assert probe["agent_ok"] is True
    assert probe["vision_ok"] is None
    assert "probe inconclusive" in probe["vision_error"]
    assert not any(str(error).startswith("vision:") for error in probe["errors"])


def test_custom_endpoint_probe_accepts_red_in_reasoning_when_content_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    save_custom_endpoint({"id": "dummy", "base_url": "http://127.0.0.1:8000/v1", "auth_required": False})

    class _GetResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "qwen2.5-vl-7b-instruct"}]}

    class _PostResponse:
        def __init__(self, payload=None):
            self._payload = payload or {"choices": [{"message": {"content": "pong"}}]}

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def iter_lines(self):
            yield b'data: {"choices":[{"delta":{"content":"p"}}]}'

    import httpx
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: _GetResponse())

    def _post(*args, **kwargs):
        body = kwargs.get("json") or {}
        messages = body.get("messages") or []
        content = messages[0].get("content") if messages else None
        if isinstance(content, list):
            return _PostResponse({"choices": [{"message": {
                "content": "",
                "reasoning_content": "The synthetic image is bright red, so answer red.",
            }}]})
        if body.get("tools"):
            return _PostResponse({
                "choices": [{
                    "message": {
                        "content": "",
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "row_bot_probe_echo", "arguments": "{\"value\":\"ok\"}"},
                        }],
                    },
                }],
            })
        return _PostResponse()

    monkeypatch.setattr(httpx, "post", _post)
    monkeypatch.setattr(httpx, "stream", lambda *args, **kwargs: _PostResponse())

    probe = probe_custom_endpoint("dummy")

    assert probe["agent_ok"] is True
    assert probe["vision_ok"] is True
    assert "bright red" in probe["vision_probe_response"]


def test_custom_endpoint_probe_skips_vision_for_text_only_model(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    save_custom_endpoint({"id": "dummy", "base_url": "http://127.0.0.1:8000/v1", "auth_required": False})

    class _GetResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "plain-local-chat"}]}

    class _PostResponse:
        def __init__(self, payload=None):
            self._payload = payload or {"choices": [{"message": {"content": "pong"}}]}

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def iter_lines(self):
            yield b'data: {"choices":[{"delta":{"content":"p"}}]}'

    post_bodies = []

    import httpx
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: _GetResponse())

    def _post(*args, **kwargs):
        body = kwargs.get("json") or {}
        post_bodies.append(body)
        if body.get("tools"):
            return _PostResponse({
                "choices": [{
                    "message": {
                        "content": "",
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "row_bot_probe_echo", "arguments": "{\"value\":\"ok\"}"},
                        }],
                    },
                }],
            })
        return _PostResponse()

    monkeypatch.setattr(httpx, "post", _post)
    monkeypatch.setattr(httpx, "stream", lambda *args, **kwargs: _PostResponse())

    probe = probe_custom_endpoint("dummy")

    assert probe["vision_ok"] is None
    assert probe["vision_probed"] is False
    assert "does not advertise vision" in probe["vision_probe_skip_reason"]
    assert probe["vision_error"] == ""
    assert probe["vision_model"] == "plain-local-chat"
    assert not any(isinstance((body.get("messages") or [{}])[0].get("content"), list) for body in post_bodies)


def test_custom_probe_summary_reports_distinct_vision_states():
    from row_bot.providers.custom import custom_probe_summary

    not_probed = custom_probe_summary({
        "classification": "agent_ready",
        "models_ok": True,
        "chat_ok": True,
        "streaming_ok": True,
        "tool_calling": True,
        "tool_round_trip": True,
        "streaming_tool_calling": True,
        "vision_ok": None,
        "vision_probed": False,
        "vision_probe_skip_reason": "model metadata does not advertise vision",
    })
    assert "vision: not probed" in not_probed["text"]

    inconclusive = custom_probe_summary({
        "classification": "agent_ready",
        "vision_ok": None,
        "vision_probed": True,
        "vision_error": "probe inconclusive: unexpected response: red",
    })
    assert "vision: inconclusive" in inconclusive["text"]
    assert inconclusive["components"][-1]["detail"].startswith("probe inconclusive")

    failed = custom_probe_summary({"classification": "agent_ready", "vision_ok": False, "vision_probed": True})
    assert "vision: failed" in failed["text"]

    ok = custom_probe_summary({"classification": "agent_ready", "vision_ok": True, "vision_probed": True})
    assert "vision: ok" in ok["text"]


def test_custom_probe_checks_summary_is_compact_and_status_colored():
    from row_bot.providers.custom import custom_probe_summary
    from row_bot.ui.provider_settings import _probe_checks_summary

    ok = _probe_checks_summary(custom_probe_summary({
        "classification": "agent_ready",
        "chat_ok": True,
        "tool_calling": True,
        "tool_round_trip": True,
        "streaming_ok": True,
        "streaming_tool_calling": True,
        "vision_ok": True,
        "vision_probed": True,
    }))
    assert ok == {"label": "6/6 checks", "color": "green"}

    skipped_vision = _probe_checks_summary(custom_probe_summary({
        "classification": "agent_ready",
        "chat_ok": True,
        "tool_calling": True,
        "tool_round_trip": True,
        "streaming_ok": True,
        "streaming_tool_calling": True,
        "vision_ok": None,
        "vision_probed": False,
    }))
    assert skipped_vision == {"label": "5/6 checks", "color": "blue-grey"}

    failed_tools = _probe_checks_summary(custom_probe_summary({
        "classification": "chat_only",
        "chat_ok": True,
        "tool_calling": False,
        "tool_round_trip": False,
        "streaming_ok": True,
        "streaming_tool_calling": False,
        "vision_ok": None,
        "vision_probed": True,
    }))
    assert failed_tools == {"label": "2/6 checks", "color": "orange"}


def test_custom_endpoint_manual_capability_ui_helper_builds_sparse_overrides():
    from row_bot.ui.provider_settings import _manual_capabilities_from_ui

    assert _manual_capabilities_from_ui("auto", "auto", "") == {}
    assert _manual_capabilities_from_ui("on", "off", "65536") == {
        "vision": True,
        "tool_calling": False,
        "context_window": 65536,
    }
    assert _manual_capabilities_from_ui("off", "on", "not a number") == {
        "vision": False,
        "tool_calling": True,
    }


def test_custom_endpoint_edit_payload_preserves_fixed_fields_for_display_name_only():
    from row_bot.ui.provider_settings import _custom_endpoint_edit_payload

    endpoint = {
        "id": "llama-cpp",
        "provider_id": "custom_openai_llama-cpp",
        "name": "llama-cpp",
        "display_name": "llama-cpp",
        "base_url": "http://127.0.0.1:8081/v1",
        "profile": "llama_cpp",
        "transport": "openai_chat",
        "execution_location": "local",
        "auth_required": False,
        "models": [{"id": "qwen3.5-9b"}],
        "last_probe": {"ok": True},
    }

    payload, stale = _custom_endpoint_edit_payload(
        endpoint,
        display_name="Local llama.cpp",
        base_url="http://127.0.0.1:8081/v1",
        no_auth=True,
    )

    assert stale is False
    assert payload["id"] == "llama-cpp"
    assert payload["profile"] == "llama_cpp"
    assert payload["execution_location"] == "local"
    assert payload["display_name"] == "Local llama.cpp"
    assert payload["models"] == [{"id": "qwen3.5-9b"}]
    assert payload["last_probe"] == {"ok": True}


def test_custom_endpoint_edit_payload_stales_probe_for_connection_and_advanced_changes():
    from row_bot.ui.provider_settings import _custom_endpoint_edit_payload

    endpoint = {
        "id": "lm-studio",
        "name": "LM Studio",
        "base_url": "http://127.0.0.1:1234/v1",
        "profile": "lmstudio",
        "transport": "openai_chat",
        "execution_location": "local",
        "auth_required": False,
        "manual_capabilities": {"vision": True},
        "models": [{"id": "qwen/qwen3.5-9b"}],
        "last_probe": {"ok": True},
    }

    payload, stale = _custom_endpoint_edit_payload(
        endpoint,
        display_name="LM Studio",
        base_url="http://127.0.0.1:2234/v1/",
        no_auth=True,
        vision_mode="off",
        tool_mode="auto",
        context_window="65536",
    )

    assert stale is True
    assert payload["base_url"] == "http://127.0.0.1:2234/v1"
    assert payload["manual_capabilities"] == {"vision": False, "context_window": 65536}
    assert "models" not in payload
    assert "last_probe" not in payload
    assert payload["profile"] == "lmstudio"
    assert payload["execution_location"] == "local"


def test_custom_endpoint_edit_payload_validates_reasoning_and_extra_json():
    from row_bot.ui.provider_settings import _custom_endpoint_edit_payload

    endpoint = {
        "id": "vllm",
        "name": "vLLM",
        "base_url": "http://127.0.0.1:8000/v1",
        "profile": "vllm",
        "transport": "openai_chat",
        "execution_location": "local",
        "auth_required": False,
    }

    payload, stale = _custom_endpoint_edit_payload(
        endpoint,
        display_name="vLLM",
        base_url=endpoint["base_url"],
        no_auth=True,
        reasoning_mode="on",
        thinking_budget="4096",
        extra_body_json='{"top_k": 20, "chat_template_kwargs": {"custom": true}}',
        supports_reasoning_content=True,
        supports_reasoning_replay=True,
    )

    assert stale is False
    assert payload["reasoning_mode"] == "on"
    assert payload["thinking_budget"] == 4096
    assert payload["extra_body"] == {"top_k": 20, "chat_template_kwargs": {"custom": True}}
    assert payload["supports_reasoning_content"] is True
    assert payload["supports_reasoning_replay"] is True


@pytest.mark.parametrize(
    ("extra_json", "message"),
    [
        ("not-json", "invalid"),
        ("[]", "JSON object"),
        ('{"authorization": "Bearer secret"}', "credentials"),
        ('{"nested": {"api_key": "secret"}}', "credentials"),
    ],
)
def test_custom_endpoint_extra_request_json_rejects_invalid_or_secret_data(extra_json, message):
    from row_bot.ui.provider_settings import _parse_custom_extra_body

    with pytest.raises(ValueError, match=message):
        _parse_custom_extra_body(extra_json)


def test_custom_endpoint_probe_classifies_chat_only_when_tools_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    save_custom_endpoint({"id": "dummy", "base_url": "http://127.0.0.1:8000/v1", "auth_required": False})

    class _GetResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "row-bot-dummy-chat", "max_model_len": 32768}]}

    class _PostResponse:
        def __init__(self, payload=None):
            self._payload = payload or {"choices": [{"message": {"content": "pong"}}]}

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def iter_lines(self):
            yield b'data: {"choices":[{"delta":{"content":"p"}}]}'

    import httpx
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: _GetResponse())
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: _PostResponse())
    monkeypatch.setattr(httpx, "stream", lambda *args, **kwargs: _PostResponse())

    probe = probe_custom_endpoint("dummy")
    endpoint = get_custom_endpoint("dummy")

    assert probe["ok"] is False
    assert probe["agent_ok"] is False
    assert probe["chat_only_ok"] is True
    assert probe["classification"] == "chat_only"
    assert endpoint["last_probe"]["classification"] == "chat_only"


def test_custom_endpoint_probe_logs_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    save_custom_endpoint({"id": "dummy", "base_url": "http://127.0.0.1:8000/v1", "auth_required": False})

    import httpx
    import row_bot.providers.custom as custom

    def _raise(*args, **kwargs):
        raise OSError("connection refused")

    logged = []
    monkeypatch.setattr(httpx, "get", _raise)
    monkeypatch.setattr(httpx, "post", _raise)
    monkeypatch.setattr(httpx, "stream", _raise)
    monkeypatch.setattr(custom.logger, "warning", lambda *args, **kwargs: logged.append(args))

    probe = probe_custom_endpoint("dummy")

    assert probe["ok"] is False
    assert probe["classification"] == "unavailable"
    assert "connection refused" in "; ".join(probe["errors"])
    assert logged
    assert "Custom endpoint probe failed" in logged[0][0]


def test_custom_endpoint_probe_success_log_includes_round_trip_and_vision(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    save_custom_endpoint({
        "id": "manual-vision",
        "base_url": "http://127.0.0.1:8000/v1",
        "auth_required": False,
        "manual_capabilities": {"vision": True},
    })

    class _GetResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "plain-local-chat"}]}

    class _PostResponse:
        def __init__(self, payload=None):
            self._payload = payload or {"choices": [{"message": {"content": "pong"}}]}

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def iter_lines(self):
            yield b'data: {"choices":[{"delta":{"content":"p"}}]}'

    import httpx
    import row_bot.providers.custom as custom

    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: _GetResponse())

    def _post(*args, **kwargs):
        body = kwargs.get("json") or {}
        messages = body.get("messages") or []
        content = messages[0].get("content") if messages else None
        if isinstance(content, list):
            return _PostResponse({"choices": [{"message": {"content": "vision-ok"}}]})
        if body.get("tools"):
            return _PostResponse({
                "choices": [{
                    "message": {
                        "content": "",
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "row_bot_probe_echo", "arguments": "{\"value\":\"ok\"}"},
                        }],
                    },
                }],
            })
        return _PostResponse()

    logged = []
    monkeypatch.setattr(httpx, "post", _post)
    monkeypatch.setattr(httpx, "stream", lambda *args, **kwargs: _PostResponse())
    monkeypatch.setattr(custom.logger, "info", lambda *args, **kwargs: logged.append(args))

    probe = probe_custom_endpoint("manual-vision")

    assert probe["ok"] is True
    assert probe["vision_ok"] is True
    assert logged
    assert "tool_round_trip" in logged[-1][0]
    assert "vision_ok" in logged[-1][0]
    assert True in logged[-1]


def test_custom_endpoint_probe_records_streaming_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    save_custom_endpoint({"id": "dummy", "base_url": "http://127.0.0.1:8000/v1", "auth_required": False})

    class _GetResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "row-bot-dummy-chat"}]}

    class _PostResponse:
        def __init__(self, payload=None):
            self._payload = payload or {"choices": [{"message": {"content": "pong"}}]}

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class _EmptyStreamResponse(_PostResponse):
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def iter_lines(self):
            return iter(())

    import httpx
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: _GetResponse())
    def _post(*args, **kwargs):
        body = kwargs.get("json") or {}
        if body.get("tools"):
            return _PostResponse({
                "choices": [{
                    "message": {
                        "content": "",
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "row_bot_probe_echo", "arguments": "{\"value\":\"ok\"}"},
                        }],
                    },
                }],
            })
        return _PostResponse()

    monkeypatch.setattr(httpx, "post", _post)
    monkeypatch.setattr(httpx, "stream", lambda *args, **kwargs: _EmptyStreamResponse())

    probe = probe_custom_endpoint("dummy")

    assert probe["ok"] is True
    assert probe["chat_ok"] is True
    assert probe["streaming_ok"] is False
    assert "streaming: no usable stream delta returned" in probe["errors"]
    assert probe["streaming_tool_calling"] is False


def test_custom_endpoint_probe_does_not_treat_done_only_sse_as_streaming(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    save_custom_endpoint({"id": "dummy", "base_url": "http://127.0.0.1:8000/v1", "auth_required": False})

    class _GetResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "row-bot-dummy-chat"}]}

    class _PostResponse:
        def __init__(self, payload=None):
            self._payload = payload or {"choices": [{"message": {"content": "pong"}}]}

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class _DoneOnlyStreamResponse(_PostResponse):
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def iter_lines(self):
            yield b"data: [DONE]"

    import httpx
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: _GetResponse())
    def _post(*args, **kwargs):
        body = kwargs.get("json") or {}
        if body.get("tools"):
            return _PostResponse({
                "choices": [{
                    "message": {
                        "content": "",
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "row_bot_probe_echo", "arguments": "{\"value\":\"ok\"}"},
                        }],
                    },
                }],
            })
        return _PostResponse()

    monkeypatch.setattr(httpx, "post", _post)
    monkeypatch.setattr(httpx, "stream", lambda *args, **kwargs: _DoneOnlyStreamResponse())

    probe = probe_custom_endpoint("dummy")

    assert probe["ok"] is True
    assert probe["chat_ok"] is True
    assert probe["streaming_ok"] is False


def test_custom_endpoint_probe_records_malformed_streamed_tool_call(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    save_custom_endpoint({"id": "dummy", "base_url": "http://127.0.0.1:8000/v1", "auth_required": False})

    class _GetResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "row-bot-dummy-chat", "max_model_len": 32768}]}

    class _PostResponse:
        def __init__(self, payload=None):
            self._payload = payload or {"choices": [{"message": {"content": "pong"}}]}

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class _MalformedToolStreamResponse(_PostResponse):
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def iter_lines(self):
            yield b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"value\\":\\"ok\\"}"}}]}}]}'
            yield b"data: [DONE]"

    class _PlainStreamResponse(_PostResponse):
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def iter_lines(self):
            yield b'data: {"choices":[{"delta":{"content":"p"}}]}'

    import httpx
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: _GetResponse())

    def _post(*args, **kwargs):
        body = kwargs.get("json") or {}
        if body.get("tools"):
            return _PostResponse({
                "choices": [{
                    "message": {
                        "content": "",
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "row_bot_probe_echo", "arguments": "{\"value\":\"ok\"}"},
                        }],
                    },
                }],
            })
        return _PostResponse()

    def _stream(*args, **kwargs):
        return _MalformedToolStreamResponse() if (kwargs.get("json") or {}).get("tools") else _PlainStreamResponse()

    monkeypatch.setattr(httpx, "post", _post)
    monkeypatch.setattr(httpx, "stream", _stream)

    probe = probe_custom_endpoint("dummy")
    endpoint = get_custom_endpoint("dummy")

    assert probe["ok"] is True
    assert probe["streaming_ok"] is True
    assert probe["streaming_tool_calling"] is False
    assert probe["streaming_tool_error"] == "no streamed structured tool call returned"
    assert endpoint["last_probe"]["streaming_tool_calling"] is False


def test_no_auth_custom_endpoint_refresh_skips_secret_lookup(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    save_custom_endpoint({"id": "dummy", "base_url": "http://127.0.0.1:8000/v1", "auth_required": False})

    import httpx
    import row_bot.providers.custom as custom

    class _GetResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "row-bot-dummy-chat"}]}

    monkeypatch.setattr(custom, "custom_endpoint_secret", lambda provider_id: (_ for _ in ()).throw(AssertionError("secret lookup should be skipped")))
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: _GetResponse())

    infos = refresh_custom_endpoint_models("dummy")

    assert [info.model_id for info in infos] == ["row-bot-dummy-chat"]
