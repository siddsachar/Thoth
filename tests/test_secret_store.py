from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


class _UnavailableKeyring:
    def get_password(self, service, account):
        raise RuntimeError("No recommended backend was available")

    def set_password(self, service, account, value):
        raise RuntimeError("No recommended backend was available")

    def delete_password(self, service, account):
        raise RuntimeError("No recommended backend was available")


def test_persistent_server_secret_initializer_creates_once_and_reuses_key(
    tmp_path,
) -> None:
    from row_bot import secret_store

    secrets_dir = tmp_path / "mounted-secrets"
    key_path = secret_store.initialize_persistent_server_secret_store(secrets_dir)
    original = key_path.read_text(encoding="ascii")

    assert key_path == secrets_dir / "ROW_BOT_SECRET_STORE_KEY"
    assert len(original) == 64
    assert all(character in "0123456789abcdef" for character in original)
    assert (
        secret_store.initialize_persistent_server_secret_store(secrets_dir) == key_path
    )
    assert key_path.read_text(encoding="ascii") == original
    if os.name != "nt":
        assert key_path.stat().st_mode & 0o777 == 0o400
        assert secrets_dir.stat().st_mode & 0o777 == 0o700


def test_persistent_server_secret_initializer_rejects_invalid_existing_key(
    tmp_path,
) -> None:
    from row_bot import secret_store

    secrets_dir = tmp_path / "mounted-secrets"
    secrets_dir.mkdir()
    key_path = secrets_dir / "ROW_BOT_SECRET_STORE_KEY"
    key_path.write_text("invalid", encoding="ascii")

    with pytest.raises(secret_store.SecretStoreError, match="64 hexadecimal"):
        secret_store.initialize_persistent_server_secret_store(secrets_dir)

    assert key_path.read_text(encoding="ascii") == "invalid"


def test_server_secret_file_is_allowlisted_bounded_and_value_free(
    tmp_path,
    monkeypatch,
) -> None:
    from row_bot import secret_store

    secret = tmp_path / "OPENAI_API_KEY"
    secret.write_text("server-value\n", encoding="utf-8")
    monkeypatch.setenv("ROW_BOT_SECRETS_DIR", str(tmp_path))

    assert secret_store.read_server_secret(
        "OPENAI_API_KEY",
        allowed_names={"OPENAI_API_KEY"},
    ) == "server-value"
    status = secret_store.server_secret_status(
        "OPENAI_API_KEY",
        allowed_names={"OPENAI_API_KEY"},
    )
    assert status == {
        "configured": True,
        "source": "secret_file",
        "externally_managed": True,
        "fingerprint": "****alue",
    }
    assert "server-value" not in repr(status)


@pytest.mark.parametrize("name", ["../OPENAI_API_KEY", "not-allowlisted", ""])
def test_server_secret_rejects_unsafe_or_unlisted_names(
    name,
    tmp_path,
    monkeypatch,
) -> None:
    from row_bot import secret_store

    monkeypatch.setenv("ROW_BOT_SECRETS_DIR", str(tmp_path))
    with pytest.raises(secret_store.SecretStoreError):
        secret_store.read_server_secret(
            name,
            allowed_names={"OPENAI_API_KEY"},
        )


def test_server_secret_rejects_non_regular_and_oversize_files(
    tmp_path,
    monkeypatch,
) -> None:
    from row_bot import secret_store

    monkeypatch.setenv("ROW_BOT_SECRETS_DIR", str(tmp_path))
    (tmp_path / "OPENAI_API_KEY").mkdir()
    with pytest.raises(secret_store.SecretStoreError, match="regular"):
        secret_store.read_server_secret(
            "OPENAI_API_KEY",
            allowed_names={"OPENAI_API_KEY"},
        )

    (tmp_path / "OPENAI_API_KEY").rmdir()
    (tmp_path / "OPENAI_API_KEY").write_bytes(
        b"x" * (secret_store.MAX_SERVER_SECRET_BYTES + 1)
    )
    with pytest.raises(secret_store.SecretStoreError, match="large"):
        secret_store.read_server_secret(
            "OPENAI_API_KEY",
            allowed_names={"OPENAI_API_KEY"},
        )


def test_provider_secret_file_survives_process_state_and_conflicts_fail_closed(
    tmp_path,
    monkeypatch,
) -> None:
    from row_bot.providers import auth_store
    from row_bot import secret_store

    (tmp_path / "OPENAI_API_KEY").write_text("mounted-provider\n", encoding="utf-8")
    monkeypatch.setenv("ROW_BOT_SECRETS_DIR", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert auth_store.get_provider_secret("openai") == "mounted-provider"
    assert auth_store.provider_secret_status("openai") == {
        "configured": True,
        "source": "secret_file",
        "fingerprint": "****ider",
        "externally_managed": True,
    }
    with pytest.raises(secret_store.SecretStoreError, match="read-only"):
        auth_store.set_provider_secret("openai", "api_key", "replacement")

    monkeypatch.setenv("OPENAI_API_KEY", "different")
    with pytest.raises(secret_store.SecretStoreError, match="conflicts"):
        auth_store.get_provider_secret("openai")


def test_channel_secret_file_is_read_only_and_conflict_safe(
    tmp_path,
    monkeypatch,
) -> None:
    from row_bot.channels import auth_store
    from row_bot import secret_store

    (tmp_path / "TELEGRAM_BOT_TOKEN").write_text("mounted-channel\n", encoding="utf-8")
    monkeypatch.setenv("ROW_BOT_SECRETS_DIR", str(tmp_path))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr(
        auth_store,
        "_server_secret_names",
        lambda: frozenset({"TELEGRAM_BOT_TOKEN"}),
    )

    assert (
        auth_store.get_channel_secret("telegram", "TELEGRAM_BOT_TOKEN")
        == "mounted-channel"
    )
    with pytest.raises(secret_store.SecretStoreError, match="read-only"):
        auth_store.set_channel_secret(
            "telegram",
            "TELEGRAM_BOT_TOKEN",
            "replacement",
        )

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "different")
    status = auth_store.channel_secret_status(
        "telegram",
        "TELEGRAM_BOT_TOKEN",
    )
    assert status["source"] == "conflict"
    assert status["configured"] is False
    assert "mounted-channel" not in repr(status)


def test_server_secret_is_never_copied_to_data_dir(
    tmp_path,
    monkeypatch,
) -> None:
    from row_bot import secret_store

    secrets_dir = tmp_path / "secrets"
    data_dir = tmp_path / "data"
    secrets_dir.mkdir()
    data_dir.mkdir()
    (secrets_dir / "OPENAI_API_KEY").write_text("only-mounted", encoding="utf-8")
    monkeypatch.setenv("ROW_BOT_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(data_dir))

    assert secret_store.read_server_secret(
        "OPENAI_API_KEY",
        allowed_names={"OPENAI_API_KEY"},
    ) == "only-mounted"
    assert list(data_dir.rglob("*")) == []


def test_encrypted_server_store_persists_when_platform_keyring_is_unavailable(
    tmp_path,
    monkeypatch,
) -> None:
    from row_bot import secret_store

    secrets_dir = tmp_path / "mounted-secrets"
    data_dir = tmp_path / "data"
    secrets_dir.mkdir()
    data_dir.mkdir()
    key_file = secrets_dir / secret_store.PERSISTENT_SERVER_SECRET_KEY_NAME
    key_file.write_text("12" * 32, encoding="ascii")
    monkeypatch.setenv("ROW_BOT_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(data_dir))
    secret_store._set_backend_for_tests(_UnavailableKeyring())
    try:
        source = secret_store.set_secret(
            "access_token",
            "fake-oauth-token-for-testing",
            namespace="providers:codex",
        )

        assert source == "encrypted_file"
        records = list(
            (data_dir / secret_store.PERSISTENT_SERVER_SECRET_DIR_NAME).glob("*.secret")
        )
        assert len(records) == 1
        payload = records[0].read_bytes()
        assert payload.startswith(b"ROWBOT-SECRET-V1\x00")
        assert b"fake-oauth-token-for-testing" not in payload
        assert "access_token" not in records[0].name

        secret_store._set_backend_for_tests(_UnavailableKeyring())
        assert secret_store.get_secret(
            "access_token",
            namespace="providers:codex",
        ) == "fake-oauth-token-for-testing"

        secret_store.delete_secret("access_token", namespace="providers:codex")
        assert not records[0].exists()
        assert secret_store.get_secret("access_token", namespace="providers:codex") is None
    finally:
        secret_store._set_backend_for_tests(None)


def test_encrypted_server_store_rejects_invalid_or_changed_key(
    tmp_path,
    monkeypatch,
) -> None:
    from row_bot import secret_store

    secrets_dir = tmp_path / "mounted-secrets"
    data_dir = tmp_path / "data"
    secrets_dir.mkdir()
    data_dir.mkdir()
    key_file = secrets_dir / secret_store.PERSISTENT_SERVER_SECRET_KEY_NAME
    monkeypatch.setenv("ROW_BOT_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(data_dir))
    secret_store._set_backend_for_tests(_UnavailableKeyring())
    try:
        key_file.write_text("not-a-key", encoding="ascii")
        with pytest.raises(secret_store.SecretStoreError, match="64 hexadecimal"):
            secret_store.set_secret("refresh_token", "fake-refresh")

        key_file.write_text("34" * 32, encoding="ascii")
        secret_store.set_secret("refresh_token", "fake-refresh")
        key_file.write_text("56" * 32, encoding="ascii")
        with pytest.raises(secret_store.SecretStoreError, match="could not be decrypted"):
            secret_store.get_secret("refresh_token")
        with pytest.raises(secret_store.SecretStoreError, match="could not be decrypted"):
            secret_store.set_secret("refresh_token", "replacement")
        with pytest.raises(secret_store.SecretStoreError, match="could not be decrypted"):
            secret_store.delete_secret("refresh_token")
    finally:
        secret_store._set_backend_for_tests(None)


def test_encrypted_server_store_is_not_an_implicit_plaintext_fallback(
    tmp_path,
    monkeypatch,
) -> None:
    from row_bot import secret_store

    secrets_dir = tmp_path / "mounted-secrets"
    data_dir = tmp_path / "data"
    secrets_dir.mkdir()
    data_dir.mkdir()
    monkeypatch.setenv("ROW_BOT_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(data_dir))
    secret_store._set_backend_for_tests(_UnavailableKeyring())
    try:
        with pytest.raises(secret_store.SecretStoreError, match="No recommended backend"):
            secret_store.set_secret("access_token", "fake-session-only-token")
        assert list(data_dir.rglob("*")) == []
    finally:
        secret_store._set_backend_for_tests(None)


def test_encrypted_server_store_survives_a_fresh_python_process(
    tmp_path,
) -> None:
    secrets_dir = tmp_path / "mounted-secrets"
    data_dir = tmp_path / "data"
    secrets_dir.mkdir()
    data_dir.mkdir()
    (secrets_dir / "ROW_BOT_SECRET_STORE_KEY").write_text("9a" * 32, encoding="ascii")
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
            "ROW_BOT_DATA_DIR": str(data_dir),
            "ROW_BOT_DEPLOYMENT_MODE": "server",
            "ROW_BOT_SECRETS_DIR": str(secrets_dir),
        }
    )
    write_result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from row_bot.secret_store import set_secret; "
                "assert set_secret('refresh_token', 'fake-process-token', "
                "namespace='providers:codex') == 'encrypted_file'"
            ),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert write_result.returncode == 0, write_result.stderr

    read_result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from row_bot.secret_store import get_secret; "
                "assert get_secret('refresh_token', namespace='providers:codex') "
                "== 'fake-process-token'"
            ),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert read_result.returncode == 0, read_result.stderr
