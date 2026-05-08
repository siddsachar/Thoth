from __future__ import annotations

import importlib
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_api_key_storage import FakeKeyring


@pytest.fixture
def data_dir():
    root = Path(".tmp") / "pytest-channel-auth-fixtures"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"case-{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    yield path


def _reload_auth_modules(monkeypatch, data_dir):
    monkeypatch.setenv("THOTH_DATA_DIR", str(data_dir))
    import secret_store
    import api_keys
    import channels.auth_store as channel_auth

    secret_store = importlib.reload(secret_store)
    api_keys = importlib.reload(api_keys)
    channel_auth = importlib.reload(channel_auth)
    backend = FakeKeyring()
    secret_store._set_backend_for_tests(backend)
    return secret_store, api_keys, channel_auth, backend


def test_channel_secret_uses_channel_namespace_not_legacy_api_keys(
    data_dir,
    monkeypatch,
):
    secret_store, api_keys, channel_auth, backend = _reload_auth_modules(
        monkeypatch, data_dir,
    )

    channel_auth.set_channel_secret(
        "telegram",
        "TELEGRAM_BOT_TOKEN",
        "tg-secret-1234",
    )

    service = secret_store.SERVICE_NAME
    assert backend.values[
        (service, "channels:telegram:TELEGRAM_BOT_TOKEN")
    ] == "tg-secret-1234"
    assert (service, "api_keys:TELEGRAM_BOT_TOKEN") not in backend.values
    assert api_keys._load_keys() == {}
    assert channel_auth.get_channel_secret(
        "telegram",
        "TELEGRAM_BOT_TOKEN",
    ) == "tg-secret-1234"


def test_channel_secret_falls_back_to_legacy_api_keys(data_dir, monkeypatch):
    _secret_store, api_keys, channel_auth, _backend = _reload_auth_modules(
        monkeypatch, data_dir,
    )

    api_keys.set_key("SLACK_BOT_TOKEN", "legacy-slack-token")
    os.environ.pop("SLACK_BOT_TOKEN", None)

    assert channel_auth.get_channel_secret(
        "slack",
        "SLACK_BOT_TOKEN",
    ) == "legacy-slack-token"
    assert channel_auth.channel_secret_status(
        "slack",
        "SLACK_BOT_TOKEN",
    )["source"] == "legacy api_keys"


def test_channel_status_detects_legacy_keyring_without_metadata(
    data_dir,
    monkeypatch,
):
    secret_store, api_keys, channel_auth, _backend = _reload_auth_modules(
        monkeypatch, data_dir,
    )
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    secret_store.set_secret("TELEGRAM_BOT_TOKEN", "orphan-legacy-9999")
    assert api_keys.key_status("TELEGRAM_BOT_TOKEN")["configured"] is False

    status = channel_auth.channel_secret_status(
        "telegram",
        "TELEGRAM_BOT_TOKEN",
    )

    assert status["configured"] is True
    assert status["source"] == "legacy api_keys"
    assert status["fingerprint"] == "****9999"


def test_channel_keyring_takes_precedence_over_environment(data_dir, monkeypatch):
    _secret_store, _api_keys, channel_auth, _backend = _reload_auth_modules(
        monkeypatch, data_dir,
    )
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "env-discord-token")
    channel_auth.set_channel_secret(
        "discord",
        "DISCORD_BOT_TOKEN",
        "saved-discord-token",
    )
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "changed-env-token")

    assert channel_auth.get_channel_secret(
        "discord",
        "DISCORD_BOT_TOKEN",
    ) == "saved-discord-token"
    assert channel_auth.channel_secret_status(
        "discord",
        "DISCORD_BOT_TOKEN",
    )["source"] == "channel keyring"


def test_channel_secret_status_reports_environment_fallback(data_dir, monkeypatch):
    _secret_store, _api_keys, channel_auth, _backend = _reload_auth_modules(
        monkeypatch, data_dir,
    )
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-telegram-1234")

    status = channel_auth.channel_secret_status("telegram", "TELEGRAM_BOT_TOKEN")

    assert status["configured"] is True
    assert status["source"] == "environment"
    assert status["fingerprint"] == "****1234"


def test_channel_secret_status_reports_env_when_keyring_fails(data_dir, monkeypatch):
    secret_store, _api_keys, channel_auth, _backend = _reload_auth_modules(
        monkeypatch, data_dir,
    )
    secret_store._set_backend_for_tests(FakeKeyring(fail=True))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-telegram-5678")

    status = channel_auth.channel_secret_status("telegram", "TELEGRAM_BOT_TOKEN")

    assert status["configured"] is True
    assert status["source"] == "environment"
    assert status["fingerprint"] == "****5678"


def test_import_channel_secret_from_environment_fallback(data_dir, monkeypatch):
    secret_store, _api_keys, channel_auth, backend = _reload_auth_modules(
        monkeypatch, data_dir,
    )
    monkeypatch.setenv("SLACK_APP_TOKEN", "env-slack-4567")

    assert channel_auth.import_channel_secret_from_fallback(
        "slack",
        "SLACK_APP_TOKEN",
    ) is True

    service = secret_store.SERVICE_NAME
    assert backend.values[
        (service, "channels:slack:SLACK_APP_TOKEN")
    ] == "env-slack-4567"
    assert channel_auth.channel_secret_status(
        "slack",
        "SLACK_APP_TOKEN",
    )["source"] == "channel keyring"


def test_migrate_legacy_channel_secrets_copies_without_deleting_legacy(
    data_dir,
    monkeypatch,
):
    secret_store, _api_keys, channel_auth, backend = _reload_auth_modules(
        monkeypatch, data_dir,
    )
    channel = SimpleNamespace(
        name="telegram",
        config_fields=[
            SimpleNamespace(storage="env", env_key="TELEGRAM_BOT_TOKEN"),
            SimpleNamespace(storage="env", env_key="TELEGRAM_USER_ID"),
        ],
    )
    secret_store.set_secret("TELEGRAM_BOT_TOKEN", "legacy-token-1234")

    stats = channel_auth.migrate_legacy_channel_secrets([channel])

    service = secret_store.SERVICE_NAME
    assert stats == {"migrated": 1, "skipped": 1, "failed": 0}
    assert backend.values[
        (service, "api_keys:TELEGRAM_BOT_TOKEN")
    ] == "legacy-token-1234"
    assert backend.values[
        (service, "channels:telegram:TELEGRAM_BOT_TOKEN")
    ] == "legacy-token-1234"


def test_migrate_legacy_channel_secrets_does_not_overwrite_channel_keyring(
    data_dir,
    monkeypatch,
):
    secret_store, _api_keys, channel_auth, backend = _reload_auth_modules(
        monkeypatch, data_dir,
    )
    channel = SimpleNamespace(
        name="slack",
        config_fields=[
            SimpleNamespace(storage="env", env_key="SLACK_BOT_TOKEN"),
        ],
    )
    secret_store.set_secret("SLACK_BOT_TOKEN", "legacy-slack-token")
    channel_auth.set_channel_secret(
        "slack",
        "SLACK_BOT_TOKEN",
        "channel-slack-token",
    )

    stats = channel_auth.migrate_legacy_channel_secrets([channel])

    service = secret_store.SERVICE_NAME
    assert stats == {"migrated": 0, "skipped": 1, "failed": 0}
    assert backend.values[
        (service, "channels:slack:SLACK_BOT_TOKEN")
    ] == "channel-slack-token"


def test_migrate_legacy_channel_secrets_never_persists_environment(
    data_dir,
    monkeypatch,
):
    secret_store, _api_keys, channel_auth, backend = _reload_auth_modules(
        monkeypatch, data_dir,
    )
    channel = SimpleNamespace(
        name="discord",
        config_fields=[
            SimpleNamespace(storage="env", env_key="DISCORD_BOT_TOKEN"),
        ],
    )
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "env-discord-token")

    stats = channel_auth.migrate_legacy_channel_secrets([channel])

    service = secret_store.SERVICE_NAME
    assert stats == {"migrated": 0, "skipped": 1, "failed": 0}
    assert (
        service,
        "channels:discord:DISCORD_BOT_TOKEN",
    ) not in backend.values


def test_migrate_legacy_channel_secrets_is_failure_safe(data_dir, monkeypatch):
    secret_store, _api_keys, channel_auth, _backend = _reload_auth_modules(
        monkeypatch, data_dir,
    )
    channel = SimpleNamespace(
        name="telegram",
        config_fields=[
            SimpleNamespace(storage="env", env_key="TELEGRAM_BOT_TOKEN"),
        ],
    )
    secret_store._set_backend_for_tests(FakeKeyring(fail=True))

    stats = channel_auth.migrate_legacy_channel_secrets([channel])

    assert stats == {"migrated": 0, "skipped": 0, "failed": 1}


def test_channel_secret_delete_does_not_delete_legacy_key(data_dir, monkeypatch):
    _secret_store, api_keys, channel_auth, _backend = _reload_auth_modules(
        monkeypatch, data_dir,
    )
    api_keys.set_key("DISCORD_BOT_TOKEN", "legacy-discord-token")
    channel_auth.set_channel_secret(
        "discord",
        "DISCORD_BOT_TOKEN",
        "new-discord-token",
    )

    channel_auth.delete_channel_secret("discord", "DISCORD_BOT_TOKEN")

    assert os.environ.get("DISCORD_BOT_TOKEN") is None
    assert api_keys.get_key("DISCORD_BOT_TOKEN") == "legacy-discord-token"
    assert channel_auth.get_channel_secret(
        "discord",
        "DISCORD_BOT_TOKEN",
    ) == "legacy-discord-token"


def test_runtime_getter_reads_channel_namespace(data_dir, monkeypatch):
    _secret_store, _api_keys, channel_auth, _backend = _reload_auth_modules(
        monkeypatch, data_dir,
    )
    channel_auth.set_channel_secret("telegram", "TELEGRAM_BOT_TOKEN", "tg-runtime")

    import channels.telegram as telegram

    telegram = importlib.reload(telegram)
    assert telegram._get_bot_token() == "tg-runtime"


def test_channel_settings_uses_provider_style_secret_controls():
    source = open("ui/settings.py", encoding="utf-8").read()
    app_source = open("app.py", encoding="utf-8").read()

    assert "Paste a new value to replace the saved one" in source
    assert "Saved securely" in source
    assert "Set by environment" in source
    assert "Save Current" in source
    assert "import_channel_secret_from_fallback" in source
    assert "migrate_legacy_channel_secrets" in app_source
    assert "legacy fallback remains active" in app_source
