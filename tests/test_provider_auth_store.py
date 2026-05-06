import os

import api_keys
import providers.config as provider_config
from providers.auth_store import get_provider_secret, provider_secret_status, set_provider_secret
from secret_store import _set_backend_for_tests


class _MemoryKeyring:
    def __init__(self):
        self.values = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        self.values[(service, account)] = value

    def delete_password(self, service, account):
        self.values.pop((service, account), None)


class _LimitedMemoryKeyring(_MemoryKeyring):
    def __init__(self, max_value_length):
        super().__init__()
        self.max_value_length = max_value_length

    def set_password(self, service, account, value):
        if len(str(value)) > self.max_value_length:
            raise RuntimeError("(1783, 'CredWrite', 'The stub received bad data')")
        super().set_password(service, account, value)


def test_provider_auth_store_uses_keyring_namespace(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    backend = _MemoryKeyring()
    _set_backend_for_tests(backend)
    try:
        set_provider_secret("openai", "api_key", "sk-provider-secret")

        assert get_provider_secret("openai") == "sk-provider-secret"
        assert any(account == "providers:openai:api_key" for _, account in backend.values)
        status = provider_secret_status("openai")
        assert status["configured"] is True
        assert status["fingerprint"] == "****cret"
    finally:
        _set_backend_for_tests(None)


def test_provider_auth_store_prefers_legacy_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-secret")

    assert get_provider_secret("openai") == "sk-env-secret"
    assert provider_secret_status("openai")["source"] == "environment"

    os.environ.pop("OPENAI_API_KEY", None)


def test_minimax_provider_auth_store_uses_environment(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "minimax-env-secret")

    assert get_provider_secret("minimax") == "minimax-env-secret"
    assert provider_secret_status("minimax")["source"] == "environment"

    os.environ.pop("MINIMAX_API_KEY", None)


def test_provider_auth_store_reports_keyring_when_saved_key_is_loaded_into_env(tmp_path, monkeypatch):
    monkeypatch.setattr(api_keys, "KEYS_PATH", tmp_path / "api_keys.json")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    backend = _MemoryKeyring()
    _set_backend_for_tests(backend)
    try:
        api_keys.set_key("OPENAI_API_KEY", "sk-saved-secret")

        assert os.environ.get("OPENAI_API_KEY") == "sk-saved-secret"
        assert provider_secret_status("openai")["source"] == "keyring"

        monkeypatch.setenv("OPENAI_API_KEY", "sk-env-secret")
        assert get_provider_secret("openai") == "sk-env-secret"
        assert provider_secret_status("openai")["source"] == "environment"
    finally:
        api_keys.delete_key("OPENAI_API_KEY")
        _set_backend_for_tests(None)


def test_provider_auth_store_chunks_large_provider_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    backend = _LimitedMemoryKeyring(max_value_length=512)
    _set_backend_for_tests(backend)
    try:
        token = "tok_" + ("x" * 1200)

        set_provider_secret("codex", "access_token", token)

        assert get_provider_secret("codex", "access_token") == token
        accounts = {account for _, account in backend.values}
        assert "providers:codex:access_token" not in accounts
        assert "providers:codex:access_token.__chunks" in accounts
        assert "providers:codex:access_token.__chunk.0000" in accounts
        status = provider_secret_status("codex", "access_token")
        assert status["configured"] is True
        assert status["fingerprint"] == "****xxxx"
    finally:
        _set_backend_for_tests(None)