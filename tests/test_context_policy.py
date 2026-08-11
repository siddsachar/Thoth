import row_bot.providers.config as provider_config
import pytest
from row_bot.providers.custom import custom_provider_id, save_custom_endpoint


def test_context_policy_uses_local_cap_for_ollama_ref(monkeypatch):
    import row_bot.models as models

    monkeypatch.setattr(models, "_num_ctx", 65_536)
    monkeypatch.setattr(models, "_local_context_mode", "fixed")
    monkeypatch.setattr(models, "_cloud_num_ctx", 1_048_576)
    monkeypatch.setattr(models, "get_model_max_context", lambda model_name=None: 32_768)

    policy = models.get_context_policy("model:ollama:qwen3:14b")

    assert policy.provider_id == "ollama"
    assert policy.policy_kind == "local"
    assert policy.user_cap == 65_536
    assert policy.effective_context == 32_768
    assert policy.request_application == "ollama_num_ctx"


def test_context_policy_uses_provider_cap_for_cloud_ref(monkeypatch):
    import row_bot.models as models

    monkeypatch.setattr(models, "_num_ctx", 65_536)
    monkeypatch.setattr(models, "_cloud_num_ctx", 131_072)
    monkeypatch.setattr(models, "_cloud_context_override", 131_072)
    monkeypatch.setattr(models, "get_model_max_context", lambda model_name=None: 1_048_576)

    policy = models.get_context_policy("model:openai:gpt-5.5")

    assert policy.provider_id == "openai"
    assert policy.policy_kind == "provider"
    assert policy.user_cap == 131_072
    assert policy.effective_context == 131_072
    assert policy.request_application == "trim_only"


def test_context_policy_coerces_string_context_caps(monkeypatch):
    import row_bot.models as models

    monkeypatch.setattr(models, "_num_ctx", "65536")
    monkeypatch.setattr(models, "_local_context_mode", "fixed")
    monkeypatch.setattr(models, "_cloud_num_ctx", "262144")
    monkeypatch.setattr(models, "_cloud_context_override", "262144")
    monkeypatch.setattr(models, "get_model_max_context", lambda model_name=None: "131072")

    local_policy = models.get_context_policy("model:ollama:qwen3:14b")
    cloud_policy = models.get_context_policy("model:openai:gpt-5.5")

    assert local_policy.user_cap == 65_536
    assert local_policy.native_max == 131_072
    assert local_policy.effective_context == 65_536
    assert cloud_policy.user_cap == 262_144
    assert cloud_policy.native_max == 131_072
    assert cloud_policy.effective_context == 131_072


def test_model_info_coerces_string_context_window():
    from row_bot.providers.models import ModelInfo, TransportMode

    info = ModelInfo(
        provider_id="openai",
        model_id="gpt-test",
        display_name="GPT Test",
        context_window="131072",
        transport=TransportMode.OPENAI_CHAT,
    )

    assert info.context_window == 131_072
    assert isinstance(info.context_window, int)


def test_context_setters_coerce_ui_string_values(monkeypatch):
    import row_bot.models as models

    monkeypatch.setattr(models, "_llm_instance", object())
    monkeypatch.setattr(models, "_current_model", "model:openai:gpt-5.5")
    monkeypatch.setattr(models, "is_cloud_model", lambda model_name: True)
    monkeypatch.setattr(
        models,
        "_get_cloud_llm",
        lambda model_name: (_ for _ in ()).throw(
            AssertionError("context settings must not create a provider transport")
        ),
    )
    saved = {}
    monkeypatch.setattr(models, "_save_settings", lambda payload: saved.update(payload))

    models.set_cloud_context_size("262144")
    models.set_context_size("65536")

    assert models.get_cloud_context_size() == 262_144
    assert models.get_user_context_size() == 65_536
    assert saved["cloud_context_size"] == 262_144
    assert saved["cloud_context_override"] == 262_144
    assert saved["context_size"] == 65_536
    assert models._llm_instance is None


def test_clearing_cloud_context_override_does_not_require_provider_credentials(monkeypatch):
    import row_bot.models as models

    monkeypatch.setattr(models, "_cloud_context_override", 262_144)
    monkeypatch.setattr(models, "_llm_instance", object())
    monkeypatch.setattr(models, "_current_model", "model:openai:gpt-5.5")
    monkeypatch.setattr(models, "is_cloud_model", lambda model_name: True)
    monkeypatch.setattr(
        models,
        "_get_cloud_llm",
        lambda model_name: (_ for _ in ()).throw(
            AssertionError("context settings must not create a provider transport")
        ),
    )
    saved = {}
    monkeypatch.setattr(models, "_save_settings", lambda payload: saved.update(payload))

    models.clear_cloud_context_override()

    assert models.get_cloud_context_override() is None
    assert saved["cloud_context_override"] is None
    assert models._llm_instance is None


def test_local_llm_construction_does_not_force_reasoning(monkeypatch):
    import row_bot.models as models

    captured = {}

    class _FakeChatOllama:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(models, "ChatOllama", _FakeChatOllama)
    monkeypatch.setattr(models, "_num_ctx", 65_536)
    monkeypatch.setattr(models, "_local_context_mode", "fixed")
    monkeypatch.setattr(models, "_ollama_base_url", lambda: "http://127.0.0.1:11434")
    monkeypatch.setattr(models, "is_cloud_model", lambda model_name: False)
    monkeypatch.setattr(models, "get_model_max_context", lambda model_name=None: 65_536)
    models.clear_llm_cache()

    model = models.get_llm_for("model:ollama:vendor/non-tool-chat:14b")

    assert model
    assert captured["model"] == "vendor/non-tool-chat:14b"
    assert captured["num_ctx"] == models.get_user_context_size()
    assert "reasoning" not in captured


def test_local_thinking_model_enables_reasoning(monkeypatch):
    import row_bot.models as models

    captured = {}

    class _FakeChatOllama:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(models, "ChatOllama", _FakeChatOllama)
    monkeypatch.setattr(models, "_num_ctx", 65_536)
    monkeypatch.setattr(models, "_local_context_mode", "fixed")
    monkeypatch.setattr(models, "_ollama_base_url", lambda: "http://127.0.0.1:11434")
    monkeypatch.setattr(models, "is_cloud_model", lambda model_name: False)
    monkeypatch.setattr(models, "get_model_max_context", lambda model_name=None: 65_536)
    models.clear_llm_cache()

    model = models.get_llm_for("model:ollama:qwen3.6:27b")

    assert model
    assert captured["model"] == "qwen3.6:27b"
    assert captured["reasoning"] is True


def test_context_policy_uses_local_cap_for_local_custom_endpoint(tmp_path, monkeypatch):
    import row_bot.models as models

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(models, "_num_ctx", 65_536)
    monkeypatch.setattr(models, "_local_context_mode", "fixed")
    monkeypatch.setattr(models, "_cloud_num_ctx", 131_072)
    monkeypatch.setattr(models, "_cloud_context_override", 131_072)
    save_custom_endpoint({
        "id": "omlx",
        "name": "oMLX",
        "base_url": "http://127.0.0.1:8000/v1",
        "execution_location": "local",
        "auth_required": False,
        "models": [{
            "id": "qwen-local",
            "model_id": "qwen-local",
            "ctx": 32_768,
            "provider": custom_provider_id("omlx"),
            "capabilities_snapshot": {"tasks": ["chat"]},
        }],
    })
    models._cloud_model_cache.pop("qwen-local", None)

    policy = models.get_context_policy(f"model:{custom_provider_id('omlx')}:qwen-local")

    assert policy.provider_id == custom_provider_id("omlx")
    assert policy.policy_kind == "local"
    assert policy.user_cap == 65_536
    assert policy.effective_context == 32_768
    assert policy.request_application == "trim_only"


def test_context_policy_uses_profile_fallback_for_unknown_local_custom_endpoint(tmp_path, monkeypatch):
    import row_bot.models as models

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(models, "_num_ctx", 65_536)
    monkeypatch.setattr(models, "_local_context_mode", "fixed")
    monkeypatch.setattr(models, "_cloud_num_ctx", 131_072)
    save_custom_endpoint({
        "id": "lm-studio",
        "name": "LM Studio",
        "profile": "lmstudio",
        "base_url": "http://127.0.0.1:1234/v1",
        "execution_location": "local",
        "auth_required": False,
        "models": [{
            "id": "qwen-local",
            "model_id": "qwen-local",
            "provider": custom_provider_id("lm-studio"),
            "capabilities_snapshot": {"tasks": ["chat"]},
        }],
    })
    models._cloud_model_cache.pop("qwen-local", None)

    policy = models.get_context_policy(f"model:{custom_provider_id('lm-studio')}:qwen-local")

    assert policy.provider_id == custom_provider_id("lm-studio")
    assert policy.policy_kind == "local"
    assert policy.native_max == 32_768
    assert policy.cap_source == "profile_default"
    assert policy.effective_context == 32_768


def test_context_policy_uses_provider_cap_for_remote_custom_endpoint(tmp_path, monkeypatch):
    import row_bot.models as models

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(models, "_num_ctx", 65_536)
    monkeypatch.setattr(models, "_cloud_num_ctx", 131_072)
    monkeypatch.setattr(models, "_cloud_context_override", 131_072)
    save_custom_endpoint({
        "id": "proxy",
        "name": "Proxy",
        "base_url": "https://llm.example.test/v1",
        "execution_location": "remote",
        "auth_required": True,
        "models": [{
            "id": "qwen-remote",
            "model_id": "qwen-remote",
            "ctx": 262_144,
            "provider": custom_provider_id("proxy"),
            "capabilities_snapshot": {"tasks": ["chat"]},
        }],
    })
    models._cloud_model_cache.pop("qwen-remote", None)

    policy = models.get_context_policy(f"model:{custom_provider_id('proxy')}:qwen-remote")

    assert policy.provider_id == custom_provider_id("proxy")
    assert policy.policy_kind == "provider"
    assert policy.user_cap == 131_072
    assert policy.effective_context == 131_072


def test_context_policy_marks_custom_runtime_context_param(tmp_path, monkeypatch):
    import row_bot.models as models

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(models, "_num_ctx", 65_536)
    monkeypatch.setattr(models, "_cloud_num_ctx", 131_072)
    save_custom_endpoint({
        "id": "llamacpp",
        "name": "llama.cpp",
        "profile": "llama_cpp",
        "base_url": "http://127.0.0.1:8080/v1",
        "execution_location": "local",
        "auth_required": False,
        "models": [{
            "id": "qwen-local",
            "model_id": "qwen-local",
            "ctx": 32_768,
            "provider": custom_provider_id("llamacpp"),
            "capabilities_snapshot": {"tasks": ["chat"]},
        }],
    })
    models._cloud_model_cache.pop("qwen-local", None)

    policy = models.get_context_policy(f"model:{custom_provider_id('llamacpp')}:qwen-local")

    assert policy.provider_id == custom_provider_id("llamacpp")
    assert policy.policy_kind == "local"
    assert policy.effective_context == 32_768
    assert policy.request_application == "request_param:n_ctx"


def test_local_auto_requests_32k_and_caps_to_observed_allocation(monkeypatch):
    import row_bot.models as models

    monkeypatch.setattr(models, "_local_context_mode", "auto")
    monkeypatch.setattr(models, "_num_ctx", 131_072)
    monkeypatch.setattr(models, "get_model_max_context", lambda model_name=None: 65_536)
    monkeypatch.setattr(models, "_observed_local_context", {"qwen3:14b": 24_576})

    policy = models.get_context_policy("model:ollama:qwen3:14b")

    assert policy.requested_limit_tokens == 32_768
    assert policy.observed_limit_tokens == 24_576
    assert policy.effective_limit_tokens == 24_576
    assert policy.compact_at_tokens == int(24_576 * 0.75)
    assert policy.usable_input_tokens == int(24_576 * 0.85)


def test_unknown_remote_auto_capacity_is_unavailable(monkeypatch):
    import row_bot.models as models

    monkeypatch.setattr(models, "_cloud_context_override", None)
    monkeypatch.setattr(models, "get_model_max_context", lambda model_name=None: None)
    monkeypatch.setattr(models, "_cloud_model_cache", {})
    monkeypatch.setattr(models, "_context_catalog", {})

    policy = models.get_context_policy("model:openai:not-a-maintained-model")

    assert policy.effective_limit_tokens is None
    assert policy.usable_input_tokens is None
    assert policy.compact_at_tokens is None
    assert policy.capacity_state == "unavailable"


@pytest.mark.parametrize("reported", [None, 0, -1, "0", "invalid"])
def test_model_max_context_normalizes_non_positive_and_invalid_cloud_capacity(
    monkeypatch,
    reported,
):
    import row_bot.models as models

    monkeypatch.setattr(models, "get_cloud_model_context", lambda model_name: reported)

    assert models.get_model_max_context("model:openai:not-a-maintained-model") is None


def test_unknown_remote_explicit_override_is_usable(monkeypatch):
    import row_bot.models as models

    monkeypatch.setattr(models, "_cloud_context_override", 262_144)
    monkeypatch.setattr(models, "get_model_max_context", lambda model_name=None: None)
    monkeypatch.setattr(models, "_cloud_model_cache", {})
    monkeypatch.setattr(models, "_context_catalog", {})

    policy = models.get_context_policy("model:openai:not-a-maintained-model")

    assert policy.native_limit_tokens is None
    assert policy.requested_limit_tokens == 262_144
    assert policy.effective_limit_tokens == 262_144
    assert policy.compact_at_tokens == int(262_144 * 0.75)
    assert policy.capacity_state == "ready"
    assert policy.capacity_source == "advanced_override"


def test_fresh_context_settings_keep_cloud_auto_and_local_auto():
    import row_bot.models as models

    migrated, _changed = models._migrate_context_settings({})

    assert migrated["cloud_context_override"] is None
    assert migrated["local_context_mode"] == "auto"
    assert migrated["context_size"] == 32_768


def test_capacity_tables_and_catalogs_are_provider_qualified(monkeypatch):
    import row_bot.models as models

    monkeypatch.setattr(models, "_cloud_context_override", None)
    monkeypatch.setattr(models, "_cloud_model_cache", {})
    monkeypatch.setattr(
        models,
        "_context_catalog",
        {"openai/shared-model": 111_000, "anthropic/shared-model": 222_000},
    )

    assert models._catalog_or_heuristic("openai", "shared-model") == 111_000
    assert models._catalog_or_heuristic("anthropic", "shared-model") == 222_000
    assert models._catalog_or_heuristic("google", "gpt-4o") == 0
    assert models._catalog_or_heuristic("openai", "gpt-4o") == 128_000


@pytest.mark.parametrize(
    ("old_local", "old_cloud", "local_mode", "cloud_override"),
    [
        (32_768, 131_072, "auto", None),
        (65_536, 262_144, "fixed", 262_144),
    ],
)
def test_context_settings_v2_migration_preserves_unrelated_keys(
    old_local,
    old_cloud,
    local_mode,
    cloud_override,
):
    import row_bot.models as models

    migrated, changed = models._migrate_context_settings({
        "model": "model:openai:gpt-4o",
        "context_size": old_local,
        "cloud_context_size": old_cloud,
        "unrelated_feature": {"enabled": True},
    })

    assert changed
    assert migrated["context_policy_version"] == 2
    assert migrated["local_context_mode"] == local_mode
    assert migrated["cloud_context_override"] == cloud_override
    assert migrated["unrelated_feature"] == {"enabled": True}


def test_settings_writer_merges_instead_of_overwriting(tmp_path, monkeypatch):
    import json
    import row_bot.models as models

    settings_path = tmp_path / "model_settings.json"
    settings_path.write_text(json.dumps({"unrelated": "keep", "context_size": 32_768}))
    monkeypatch.setattr(models, "_SETTINGS_PATH", settings_path)
    monkeypatch.setattr(models, "_DATA_DIR", tmp_path)

    models._save_settings({"cloud_context_override": 65_536})

    saved = json.loads(settings_path.read_text())
    assert saved["unrelated"] == "keep"
    assert saved["cloud_context_override"] == 65_536
    assert saved["context_policy_version"] == 2
