import json
import os

import row_bot.api_keys as api_keys
import row_bot.providers.config as provider_config
import row_bot.providers.auth_store as auth_store
from row_bot.providers.auth_store import get_provider_secret, provider_secret_status, set_provider_secret
from row_bot.secret_store import _set_backend_for_tests


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


class _FailingKeyring:
    def get_password(self, service, account):
        raise RuntimeError("No recommended backend was available")

    def set_password(self, service, account, value):
        raise RuntimeError("No recommended backend was available")

    def delete_password(self, service, account):
        raise RuntimeError("No recommended backend was available")


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


def test_ollama_cloud_provider_auth_store_uses_environment(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-cloud-env-secret")

    assert get_provider_secret("ollama_cloud") == "ollama-cloud-env-secret"
    assert provider_secret_status("ollama_cloud")["source"] == "environment"

    os.environ.pop("OLLAMA_API_KEY", None)


def test_atlascloud_provider_auth_store_uses_environment(monkeypatch):
    monkeypatch.setenv("ATLASCLOUD_API_KEY", "atlascloud-env-secret")

    assert get_provider_secret("atlascloud") == "atlascloud-env-secret"
    assert provider_secret_status("atlascloud")["source"] == "environment"

    os.environ.pop("ATLASCLOUD_API_KEY", None)


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


def test_provider_auth_store_uses_session_when_keyring_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    backend = _FailingKeyring()
    auth_store._clear_session_secrets_for_tests()
    _set_backend_for_tests(backend)
    try:
        set_provider_secret("custom_openai_llama", "api_key", "sk-session-secret")

        assert get_provider_secret("custom_openai_llama") == "sk-session-secret"
        assert provider_secret_status("custom_openai_llama") == {
            "configured": True,
            "source": "session",
            "fingerprint": "****cret",
        }
        assert "session only" in auth_store.get_storage_warning()

        raw_config = (tmp_path / "providers.json").read_text(encoding="utf-8")
        assert "sk-session-secret" not in raw_config
        provider_entry = json.loads(raw_config)["providers"]["custom_openai_llama"]
        assert provider_entry["source"] == "session"
        assert provider_entry["secret_storage"] == "session"
        assert provider_entry["fingerprint"] == "****cret"

        auth_store._clear_session_secrets_for_tests()
        assert get_provider_secret("custom_openai_llama") == ""
        assert provider_secret_status("custom_openai_llama")["configured"] is False
    finally:
        auth_store._clear_session_secrets_for_tests()
        _set_backend_for_tests(None)


def test_provider_auth_store_uses_encrypted_server_store_across_process_state(
    tmp_path,
    monkeypatch,
):
    secrets_dir = tmp_path / "mounted-secrets"
    data_dir = tmp_path / "data"
    secrets_dir.mkdir()
    data_dir.mkdir()
    (secrets_dir / "ROW_BOT_SECRET_STORE_KEY").write_text("78" * 32, encoding="ascii")
    monkeypatch.setenv("ROW_BOT_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(data_dir))
    monkeypatch.setattr(provider_config, "CONFIG_PATH", data_dir / "providers.json")
    auth_store._clear_session_secrets_for_tests()
    _set_backend_for_tests(_FailingKeyring())
    try:
        set_provider_secret(
            "codex",
            "refresh_token",
            "fake-rotating-refresh-token",
            source="oauth_device",
        )

        assert get_provider_secret("codex", "refresh_token") == "fake-rotating-refresh-token"
        assert auth_store.get_storage_warning() == ""
        raw_config = json.loads((data_dir / "providers.json").read_text(encoding="utf-8"))
        assert raw_config["providers"]["codex"]["secret_storage"] == "encrypted_file"
        assert "fake-rotating-refresh-token" not in repr(raw_config)

        auth_store._clear_session_secrets_for_tests()
        _set_backend_for_tests(_FailingKeyring())
        assert get_provider_secret("codex", "refresh_token") == "fake-rotating-refresh-token"
        assert provider_secret_status("codex", "refresh_token") == {
            "configured": True,
            "source": "encrypted_file",
            "fingerprint": "****oken",
        }
    finally:
        auth_store._clear_session_secrets_for_tests()
        _set_backend_for_tests(None)


def test_provider_auth_store_changed_server_key_cannot_replace_existing_secret(
    tmp_path,
    monkeypatch,
):
    secrets_dir = tmp_path / "mounted-secrets"
    data_dir = tmp_path / "data"
    secrets_dir.mkdir()
    data_dir.mkdir()
    key_file = secrets_dir / "ROW_BOT_SECRET_STORE_KEY"
    key_file.write_text("ab" * 32, encoding="ascii")
    monkeypatch.setenv("ROW_BOT_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(data_dir))
    monkeypatch.setattr(provider_config, "CONFIG_PATH", data_dir / "providers.json")
    auth_store._clear_session_secrets_for_tests()
    _set_backend_for_tests(_FailingKeyring())
    try:
        set_provider_secret("codex", "refresh_token", "fake-original-refresh")
        records_before = {
            path.name: path.read_bytes()
            for path in (data_dir / "secure-secrets").glob("*.secret")
        }

        key_file.write_text("cd" * 32, encoding="ascii")
        set_provider_secret("codex", "refresh_token", "fake-replacement-refresh")

        records_after = {
            path.name: path.read_bytes()
            for path in (data_dir / "secure-secrets").glob("*.secret")
        }
        assert records_after == records_before
        assert provider_secret_status("codex", "refresh_token")["source"] == "session"
        assert "session only" in auth_store.get_storage_warning()
    finally:
        auth_store._clear_session_secrets_for_tests()
        _set_backend_for_tests(None)


def test_provider_auth_store_keyring_success_clears_session_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    auth_store._clear_session_secrets_for_tests()
    _set_backend_for_tests(_FailingKeyring())
    try:
        set_provider_secret("custom_openai_llama", "api_key", "sk-session-secret")
        assert provider_secret_status("custom_openai_llama")["source"] == "session"
    finally:
        _set_backend_for_tests(None)

    backend = _MemoryKeyring()
    _set_backend_for_tests(backend)
    try:
        set_provider_secret("custom_openai_llama", "api_key", "sk-keyring-secret")

        assert get_provider_secret("custom_openai_llama") == "sk-keyring-secret"
        assert provider_secret_status("custom_openai_llama")["source"] == "keyring"
        raw_config = json.loads((tmp_path / "providers.json").read_text(encoding="utf-8"))
        assert raw_config["providers"]["custom_openai_llama"]["source"] == "keyring"
        assert raw_config["providers"]["custom_openai_llama"]["secret_storage"] == "keyring"
    finally:
        auth_store._clear_session_secrets_for_tests()
        _set_backend_for_tests(None)
