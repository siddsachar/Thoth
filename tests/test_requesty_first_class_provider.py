from __future__ import annotations

from pathlib import Path


def test_requesty_is_registered_in_provider_catalog():
    from row_bot.providers.catalog import PROVIDER_DEFINITIONS, get_provider_definition

    assert "requesty" in PROVIDER_DEFINITIONS
    definition = get_provider_definition("requesty")
    assert definition is not None
    assert definition.id == "requesty"
    assert definition.display_name == "Requesty"
    assert definition.base_url == "https://router.requesty.ai/v1"


def test_requesty_api_key_env_mapping():
    from row_bot.providers.auth_store import PROVIDER_API_KEY_ENV

    assert PROVIDER_API_KEY_ENV.get("requesty") == "REQUESTY_API_KEY"


def test_requesty_listed_among_configured_provider_ids(monkeypatch):
    import row_bot.providers.runtime as runtime

    # list_configured_provider_ids() only returns providers whose key is set,
    # so configure a Requesty key and assert it surfaces.
    monkeypatch.setattr(
        runtime,
        "is_provider_available",
        lambda provider_id: provider_id == "requesty",
    )

    assert "requesty" in runtime.list_configured_provider_ids()


def test_requesty_runtime_builds_openai_compatible_client(monkeypatch):
    import row_bot.providers.runtime as runtime

    monkeypatch.setattr(runtime, "get_provider_secret", lambda provider_id: "requesty-key")

    model = runtime.create_chat_model("openai/gpt-4o-mini", provider_id="requesty")

    assert type(model).__name__ == "ChatOpenAICompatible"
    assert model.model_name == "openai/gpt-4o-mini"
    assert model.base_url == "https://router.requesty.ai/v1"
    assert model.endpoint["provider_id"] == "requesty"
    assert model.endpoint["profile"] == "requesty"


def test_requesty_runtime_accepts_provider_ref(monkeypatch):
    import row_bot.providers.runtime as runtime

    monkeypatch.setattr(runtime, "get_provider_secret", lambda provider_id: "requesty-key")

    model = runtime.create_chat_model("model:requesty:openai/gpt-4o-mini")

    assert type(model).__name__ == "ChatOpenAICompatible"
    assert model.model_name == "openai/gpt-4o-mini"
    assert model.base_url == "https://router.requesty.ai/v1"
    assert model.endpoint["provider_id"] == "requesty"


def test_requesty_runtime_requires_a_key(monkeypatch):
    import pytest

    import row_bot.providers.runtime as runtime

    monkeypatch.setattr(runtime, "get_provider_secret", lambda provider_id: "")

    with pytest.raises(ValueError):
        runtime.create_chat_model("openai/gpt-4o-mini", provider_id="requesty")


def test_requesty_capabilities_use_boolean_fields():
    from row_bot.providers.catalog import classify_model_capabilities

    tool_and_vision = classify_model_capabilities(
        "requesty",
        "openai/gpt-4o",
        {"supports_tool_calling": True, "supports_vision": True},
    )
    no_tools = classify_model_capabilities(
        "requesty",
        "deepseek/deepseek-chat",
        {"supports_tool_calling": False, "supports_vision": False},
    )

    assert tool_and_vision["tool_calling"] is True
    assert "image" in tool_and_vision["input_modalities"]
    assert no_tools["tool_calling"] is False


def test_requesty_explicit_false_vision_overrides_loose_metadata():
    from row_bot.providers.requesty import requesty_model_info_from_metadata

    info = requesty_model_info_from_metadata(
        "openai/gpt-4o-mini",
        {
            "context_window": 128_000,
            "supports_tool_calling": True,
            "supports_vision": False,
            "input_modalities": ["text", "image"],
        },
    )

    assert info is not None
    assert "image" not in info.input_modalities
    assert "vision" not in info.capabilities


def test_validate_requesty_key_lists_models(monkeypatch):
    import httpx
    import row_bot.models as models

    captured = {}

    class _Response:
        status_code = 200
        text = "{}"

        def json(self):
            return {"data": []}

    def _fake_get(url, **kwargs):
        captured["url"] = url
        captured["headers"] = dict(kwargs.get("headers") or {})
        return _Response()

    monkeypatch.setattr(httpx, "get", _fake_get)

    assert models.validate_requesty_key("requesty-key") is True
    assert captured["url"] == "https://router.requesty.ai/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer requesty-key"


def test_requesty_model_facade_fetches_provider_qualified_catalog(monkeypatch):
    import httpx
    import row_bot.models as models
    import row_bot.providers.auth_store as auth_store
    from row_bot.providers.capabilities import snapshot_supports_surface

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
                    {
                        "id": "openai/gpt-4o-mini",
                        "name": "GPT-4o mini",
                        "context_window": 128_000,
                        "supports_tool_calling": True,
                        "supports_vision": False,
                    },
                    {
                        "id": "anthropic/claude-sonnet-4",
                        "context_window": "200000",
                        "supports_tool_calling": True,
                        "supports_vision": True,
                    },
                    {
                        "id": "openai/text-embedding-3-small",
                        "type": "embedding",
                        "context_window": 8192,
                    },
                ]
            }

    def _fake_get(url, **kwargs):
        captured["url"] = url
        captured["headers"] = dict(kwargs.get("headers") or {})
        return _Response()

    monkeypatch.setattr(auth_store, "get_provider_secret", lambda provider_id: "requesty-key" if provider_id == "requesty" else "")
    monkeypatch.setattr(httpx, "get", _fake_get)
    try:
        models._cloud_model_cache.clear()
        count = models.fetch_cloud_models("requesty")

        assert count == 2
        assert captured["url"] == "https://router.requesty.ai/v1/models"
        assert captured["headers"]["Authorization"] == "Bearer requesty-key"
        assert "openai/gpt-4o-mini" not in models._cloud_model_cache
        requesty_ref = "model:requesty:openai/gpt-4o-mini"
        vision_ref = "model:requesty:anthropic/claude-sonnet-4"
        assert models._cloud_model_cache[requesty_ref]["provider"] == "requesty"
        assert models.get_cloud_provider(requesty_ref) == "requesty"
        assert models.get_cloud_model_context(requesty_ref) == 128_000
        assert models.get_cloud_model_context(vision_ref) == 200_000
        assert "model:requesty:openai/text-embedding-3-small" not in models._cloud_model_cache
        snapshot = models._cloud_model_cache[vision_ref]["capabilities_snapshot"]
        assert snapshot["tool_calling"] is True
        assert snapshot_supports_surface(snapshot, "chat") is True
        assert snapshot_supports_surface(snapshot, "vision") is True
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(old_cache)


def test_requesty_live_catalog_failure_preserves_existing_cache(monkeypatch):
    import httpx
    import row_bot.models as models
    import row_bot.providers.auth_store as auth_store

    old_cache = dict(models._cloud_model_cache)
    monkeypatch.setattr(auth_store, "get_provider_secret", lambda provider_id: "requesty-key" if provider_id == "requesty" else "")
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: (_ for _ in ()).throw(httpx.TimeoutException("boom")))
    try:
        models._cloud_model_cache.clear()
        models._cloud_model_cache["model:requesty:openai/gpt-4o-mini"] = {
            "provider": "requesty",
            "label": "GPT-4o mini",
            "ctx": 128_000,
        }

        count = models.fetch_cloud_models("requesty")

        assert count == 0
        assert "model:requesty:openai/gpt-4o-mini" in models._cloud_model_cache
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(old_cache)


def test_refresh_cloud_models_preserves_requesty_rows_when_fetch_fails(monkeypatch):
    import row_bot.models as models

    old_cache = dict(models._cloud_model_cache)
    old_current = models._current_model
    calls: list[str] = []

    def _fake_fetch(provider: str) -> int:
        calls.append(provider)
        return 0

    monkeypatch.setattr(models, "fetch_context_catalog", lambda: 0)
    monkeypatch.setattr(models, "_fetch_cloud_models", _fake_fetch)
    monkeypatch.setattr(models, "_save_cloud_cache", lambda: None)
    try:
        models._cloud_model_cache.clear()
        models._cloud_model_cache["model:requesty:openai/gpt-4o-mini"] = {
            "provider": "requesty",
            "label": "GPT-4o mini",
            "ctx": 128_000,
        }
        models._current_model = "model:requesty:openai/gpt-4o-mini"

        models.refresh_cloud_models()

        assert "requesty" in calls
        assert models._cloud_model_cache["model:requesty:openai/gpt-4o-mini"]["provider"] == "requesty"
    finally:
        models._current_model = old_current
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(old_cache)


def test_requesty_targeted_model_catalog_refresh_preserves_rows_on_fetch_failure(tmp_path, monkeypatch):
    import row_bot.models as models
    import row_bot.providers.model_catalog_cache as catalog_cache

    old_cache = dict(models._cloud_model_cache)
    monkeypatch.setattr(catalog_cache, "CATALOG_CACHE_PATH", tmp_path / "model_catalog_cache.json")
    monkeypatch.setattr(models, "fetch_context_catalog", lambda: 0)
    monkeypatch.setattr(models, "_fetch_cloud_models", lambda provider_id: 0)
    monkeypatch.setattr(models, "_save_cloud_cache", lambda: None)
    try:
        models._cloud_model_cache.clear()
        models._cloud_model_cache["model:requesty:openai/gpt-4o-mini"] = {
            "provider": "requesty",
            "label": "GPT-4o mini",
            "ctx": 128_000,
        }

        snapshot = catalog_cache.refresh_model_catalog_cache(
            reason="test_requesty_preserve",
            force=True,
            provider_id="requesty",
        )

        assert "model:requesty:openai/gpt-4o-mini" in snapshot.cloud_cache
        assert snapshot.provider_status["requesty"]["status"] == "cached"
        assert snapshot.provider_status["requesty"]["count"] == 1
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(old_cache)


def test_requesty_readiness_uses_metadata_for_agent_mode(monkeypatch):
    import row_bot.models as models
    import row_bot.providers.readiness as readiness
    from row_bot.providers.readiness import evaluate_runtime_readiness
    from row_bot.providers.resolution import resolve_provider_config

    monkeypatch.setattr(readiness, "provider_status", lambda provider_id: {"configured": True})
    monkeypatch.setattr(models, "get_context_policy", lambda value: models.ContextPolicy(
        model_ref="model:requesty:openai/gpt-4o-mini",
        provider_id="requesty",
        runtime_model="openai/gpt-4o-mini",
        native_max=128_000,
        user_cap=128_000,
        effective_context=128_000,
        policy_kind="provider",
        cap_source="provider_metadata",
        request_application="trim_only",
    ))

    result = evaluate_runtime_readiness(
        resolve_provider_config("model:requesty:openai/gpt-4o-mini"),
        capability_snapshot={
            "tasks": ["chat"],
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "tool_calling": True,
            "streaming": True,
            "transport": "openai_chat",
            "endpoint_compatibility": ["openai_chat"],
            "source_confidence": "live_requesty_model_list",
        },
    )

    assert result.selected_mode == "agent"
    assert result.agent.ready is True
    assert result.agent.tool_calling_source == "requesty_metadata"


def test_requesty_missing_tool_metadata_is_chat_only(monkeypatch):
    import row_bot.models as models
    import row_bot.providers.readiness as readiness
    from row_bot.providers.readiness import evaluate_runtime_readiness
    from row_bot.providers.resolution import resolve_provider_config

    monkeypatch.setattr(readiness, "provider_status", lambda provider_id: {"configured": True})
    monkeypatch.setattr(models, "get_context_policy", lambda value: models.ContextPolicy(
        model_ref="model:requesty:unknown/provider",
        provider_id="requesty",
        runtime_model="unknown/provider",
        native_max=128_000,
        user_cap=128_000,
        effective_context=128_000,
        policy_kind="provider",
        cap_source="provider_metadata",
        request_application="trim_only",
    ))

    result = evaluate_runtime_readiness(
        resolve_provider_config("model:requesty:unknown/provider"),
        capability_snapshot={
            "tasks": ["chat"],
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "tool_calling": None,
            "streaming": True,
            "transport": "openai_chat",
            "endpoint_compatibility": ["openai_chat"],
        },
    )

    assert result.selected_mode == "chat_only"
    assert result.chat.ready is True
    assert result.agent.ready is False
    assert any("Requesty tool metadata" in error for error in result.agent.errors)


def test_requesty_model_catalog_rows_are_chat_only_without_tool_metadata(monkeypatch):
    from row_bot.providers.model_catalog import build_model_catalog_rows

    monkeypatch.setattr("row_bot.providers.model_catalog._provider_status_by_id", lambda: {
        "requesty": {"configured": True},
    })

    rows = build_model_catalog_rows(
        cloud_cache={
            "model:requesty:openai/gpt-4o-mini": {
                "provider": "requesty",
                "label": "GPT-4o mini",
                "ctx": 128_000,
                "transport": "openai_chat",
                "capabilities_snapshot": {
                    "tasks": ["chat"],
                    "input_modalities": ["text"],
                    "output_modalities": ["text"],
                    "tool_calling": None,
                    "streaming": True,
                    "transport": "openai_chat",
                    "endpoint_compatibility": ["openai_chat"],
                },
            },
        },
        ollama_rows=[],
        quick_choices=[],
    )

    row = next(row for row in rows if row.selection_ref == "model:requesty:openai/gpt-4o-mini")
    assert row.provider_id == "requesty"
    assert row.runtime_mode == "chat_only"
    assert row.status_reason == "Chat Only: tools and actions are off."


def test_requesty_settings_and_setup_wizard_are_wired():
    provider_settings_source = Path("src/row_bot/ui/provider_settings.py").read_text(encoding="utf-8")
    setup_source = Path("src/row_bot/ui/setup_wizard.py").read_text(encoding="utf-8")
    from row_bot.ui.provider_settings import _api_key_provider_action_state, _api_key_provider_ids, _api_key_provider_ui

    assert "requesty" in _api_key_provider_ids()
    assert _api_key_provider_action_state({"provider_id": "requesty"})["can_manage_api_key"] is True
    assert _api_key_provider_ui("requesty").validator_name == "validate_requesty_key"
    assert "set_provider_secret(provider_id, \"api_key\", value" in provider_settings_source
    assert "provider_key_saved" in provider_settings_source
    assert "Requesty API Key (optional)" in setup_source
    assert '("requesty", requesty_val)' in setup_source
    assert 'set_key("REQUESTY_API_KEY", requesty_val)' in setup_source
