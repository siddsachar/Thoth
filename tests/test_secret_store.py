from __future__ import annotations

import pytest


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
