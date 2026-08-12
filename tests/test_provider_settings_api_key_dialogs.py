import json
import os
from pathlib import Path

import pytest

import row_bot.api_keys as api_keys
import row_bot.providers.auth_store as auth_store
import row_bot.providers.config as provider_config
import row_bot.providers.status as provider_status
import row_bot.secret_store as secret_store
from row_bot.providers.auth_store import get_provider_secret, provider_secret_status
from row_bot.providers.catalog import PROVIDER_DEFINITIONS, list_provider_definitions
from row_bot.providers.models import AuthMethod
from row_bot.secret_store import _set_backend_for_tests
from row_bot.ui import provider_settings


class _MemoryKeyring:
    def __init__(self):
        self.values = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        self.values[(service, account)] = value

    def delete_password(self, service, account):
        self.values.pop((service, account), None)


def test_api_key_provider_actions_are_catalog_driven():
    expected = {
        definition.id
        for definition in PROVIDER_DEFINITIONS.values()
        if AuthMethod.API_KEY in definition.auth_methods
    }

    assert set(provider_settings._api_key_provider_ids()) == expected
    for provider_id in expected:
        assert provider_settings._api_key_provider_action_state({"provider_id": provider_id}) == {
            "can_manage_api_key": True,
        }
    for provider_id in {"ollama", "codex", "claude_subscription", "xai_oauth"}:
        assert provider_settings._api_key_provider_action_state({"provider_id": provider_id}) == {
            "can_manage_api_key": False,
        }


def test_provider_status_cards_keep_unconfigured_api_providers(monkeypatch):
    monkeypatch.setattr(
        provider_status,
        "provider_status",
        lambda provider_id, refresh_tokens=False: {"configured": False},
    )
    monkeypatch.setattr(
        provider_status,
        "_cached_model_stats",
        lambda provider_id: {"model_count": 0, "chat_count": 0, "media_count": 0},
    )
    monkeypatch.setattr(
        provider_status,
        "_provider_catalog_stats",
        lambda provider_id: {
            "model_count": None,
            "chat_count": 0,
            "media_count": 0,
            "model_count_source": "",
            "model_count_status": "unknown",
        },
    )

    cards = provider_status.provider_status_cards()
    cards_by_id = {card["provider_id"]: card for card in cards}
    expected = set(provider_settings._api_key_provider_ids())

    assert expected <= set(cards_by_id)
    for provider_id in expected:
        card = cards_by_id[provider_id]
        assert card["configured"] is False
        assert card["group"] == "API Providers"
        assert AuthMethod.API_KEY.value in card["auth_methods"]


def test_provider_api_key_validation_policy_and_normalization():
    assert provider_settings._api_key_provider_ui("requesty").validator_name == "validate_requesty_key"
    assert provider_settings._api_key_provider_ui("atlascloud").validator_name == "validate_atlascloud_key"
    assert provider_settings._api_key_provider_ui("xai").validation_failure == "warn"
    assert provider_settings._api_key_provider_ui("minimax").validation_failure == "warn"
    assert provider_settings._api_key_provider_ui("ollama_cloud").normalizer_name == "normalize_ollama_cloud_api_key"
    assert provider_settings._normalize_provider_api_key("ollama_cloud", " Bearer ollama-secret ") == "ollama-secret"


def test_provider_api_key_save_replaces_legacy_local_key_and_clear_removes_provider_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "KEYS_PATH", tmp_path / "api_keys.json")
    monkeypatch.delenv("REQUESTY_API_KEY", raising=False)
    backend = _MemoryKeyring()
    _set_backend_for_tests(backend)
    cache_clears = []
    monkeypatch.setattr(provider_settings, "_clear_provider_runtime_cache", lambda: cache_clears.append("cleared"))
    try:
        api_keys.set_key("REQUESTY_API_KEY", "legacy-requesty-key")
        assert get_provider_secret("requesty") == "legacy-requesty-key"

        status = provider_settings._save_provider_api_key_value("requesty", "provider-requesty-key")

        assert status["configured"] is True
        assert status["source"] == "keyring"
        assert get_provider_secret("requesty") == "provider-requesty-key"
        assert cache_clears == ["cleared"]
        raw_config = (tmp_path / "providers.json").read_text(encoding="utf-8")
        assert "provider-requesty-key" not in raw_config
        provider_entry = json.loads(raw_config)["providers"]["requesty"]
        assert provider_entry["auth_method"] == AuthMethod.API_KEY.value
        assert provider_entry["configured"] is True

        cleared = provider_settings._clear_provider_api_key_value("requesty")

        assert cleared["configured"] is False
        assert get_provider_secret("requesty") == ""
        assert cache_clears == ["cleared", "cleared"]
    finally:
        auth_store._clear_session_secrets_for_tests()
        api_keys.delete_key("REQUESTY_API_KEY")
        _set_backend_for_tests(None)


@pytest.mark.parametrize(
    ("provider_id", "env_var", "payload", "expected_url", "model_id"),
    [
        (
            "openrouter",
            "OPENROUTER_API_KEY",
            {
                "data": [{
                    "id": "deepseek/deepseek-chat",
                    "name": "DeepSeek Chat",
                    "context_length": 128_000,
                }],
            },
            "https://openrouter.ai/api/v1/models",
            "deepseek/deepseek-chat",
        ),
        (
            "google",
            "GOOGLE_API_KEY",
            {
                "models": [{
                    "name": "models/gemini-2.5-flash",
                    "displayName": "Gemini 2.5 Flash",
                    "inputTokenLimit": 1_048_576,
                    "supportedGenerationMethods": ["generateContent"],
                }],
            },
            "https://generativelanguage.googleapis.com/v1beta/models",
            "gemini-2.5-flash",
        ),
    ],
)
def test_provider_scoped_api_key_save_refreshes_live_catalog(
    tmp_path,
    monkeypatch,
    provider_id,
    env_var,
    payload,
    expected_url,
    model_id,
):
    import httpx
    import row_bot.models as models
    from row_bot.providers.model_catalog import build_model_catalog_rows

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "KEYS_PATH", tmp_path / "api_keys.json")
    monkeypatch.setattr(api_keys, "_session_keys", {})
    monkeypatch.setattr(auth_store, "_session_provider_secrets", {})
    monkeypatch.delenv(env_var, raising=False)
    monkeypatch.delenv("ROW_BOT_SECRETS_DIR", raising=False)
    monkeypatch.delenv("ROW_BOT_DEPLOYMENT_MODE", raising=False)
    monkeypatch.setattr(provider_settings, "_clear_provider_runtime_cache", lambda: None)
    monkeypatch.setattr(
        "row_bot.providers.model_catalog._provider_status_by_id",
        lambda: {provider_id: {"configured": True}},
    )
    original_backend = secret_store._backend_override
    old_cache = dict(models._cloud_model_cache)
    calls = []
    test_key = "provider-only-test-key"

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    def _fake_get(url, **kwargs):
        calls.append({
            "url": url,
            "headers": dict(kwargs.get("headers") or {}),
            "params": dict(kwargs.get("params") or {}),
        })
        return _Response()

    _set_backend_for_tests(_MemoryKeyring())
    monkeypatch.setattr(httpx, "get", _fake_get)
    try:
        models._cloud_model_cache.clear()

        status = provider_settings._save_provider_api_key_value(provider_id, test_key)

        assert status["configured"] is True
        assert api_keys.get_key(env_var) == ""

        result = models.refresh_cloud_provider_models(provider_id)

        assert result.status == "live"
        assert result.effective_count == 1
        assert calls[0]["url"] == expected_url
        if provider_id == "google":
            assert calls[0]["params"]["key"] == test_key
        else:
            assert calls[0]["headers"]["Authorization"] == f"Bearer {test_key}"
        assert models._cloud_model_cache[model_id]["provider"] == provider_id

        rows = build_model_catalog_rows(
            cloud_cache=dict(models._cloud_model_cache),
            ollama_rows=[],
            quick_choices=[],
        )
        row = next(row for row in rows if row.provider_id == provider_id and row.model_id == model_id)
        assert row.selection_ref == f"model:{provider_id}:{model_id}"
    finally:
        auth_store.delete_provider_secret(provider_id)
        auth_store._clear_session_secrets_for_tests()
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(old_cache)
        _set_backend_for_tests(original_backend)


def test_provider_api_key_clear_preserves_environment_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "KEYS_PATH", tmp_path / "api_keys.json")
    monkeypatch.setenv("REQUESTY_API_KEY", "env-requesty-key")
    _set_backend_for_tests(_MemoryKeyring())
    monkeypatch.setattr(provider_settings, "_clear_provider_runtime_cache", lambda: None)
    try:
        status = provider_settings._clear_provider_api_key_value("requesty")

        assert os.environ["REQUESTY_API_KEY"] == "env-requesty-key"
        assert get_provider_secret("requesty") == "env-requesty-key"
        assert provider_secret_status("requesty")["source"] == "environment"
        assert status["configured"] is True
        assert status["source"] == "environment"
    finally:
        auth_store._clear_session_secrets_for_tests()
        _set_backend_for_tests(None)


def test_provider_catalog_refresh_uses_provider_scoped_reason(monkeypatch):
    calls = []

    def _fake_start(**kwargs):
        calls.append(kwargs)
        return False

    import row_bot.providers.model_catalog_cache as cache

    monkeypatch.setattr(cache, "start_model_catalog_refresh_background", _fake_start)
    monkeypatch.setattr(provider_settings.ui, "notify", lambda *args, **kwargs: None)

    assert provider_settings._start_provider_catalog_refresh_ui(
        reason="provider_key_cleared",
        provider_id="requesty",
        force=True,
    ) is False
    assert calls == [{"reason": "provider_key_cleared", "provider_id": "requesty", "force": True}]


def test_settings_providers_tab_no_longer_contains_duplicate_api_key_section():
    source = Path("src/row_bot/ui/settings.py").read_text(encoding="utf-8")

    assert "Cloud API Providers" not in source
    assert 'ui.expansion("Requesty"' not in source
    assert 'ui.expansion("OpenCode Zen"' not in source
    assert 'ui.expansion("OpenCode Go"' not in source
    assert 'set_key("REQUESTY_API_KEY", val)' not in source
