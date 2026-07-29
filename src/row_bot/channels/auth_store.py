"""Channel credential storage.

Channel credentials live in channel-specific keyring namespaces so they are
not affected by legacy bulk ``api_keys`` rewrites.  Reads retain the old env
and api_keys fallbacks for compatibility with existing installs.
"""

from __future__ import annotations

import os
import logging
from typing import Any

import row_bot.api_keys as api_keys
import row_bot.secret_store as secret_store

log = logging.getLogger("row_bot.channels.auth_store")


def _server_secret_names() -> frozenset[str]:
    from row_bot.channels.registry import all_channels

    return frozenset(
        str(getattr(field, "env_key", "") or "").strip()
        for channel in all_channels()
        for field in getattr(channel, "config_fields", []) or []
        if getattr(field, "storage", "") == "env"
        and str(getattr(field, "env_key", "") or "").strip()
    )


def _namespace(channel_name: str) -> str:
    return f"channels:{str(channel_name).strip()}"


def _credential_name(env_key: str) -> str:
    return str(env_key or "").strip()


def get_channel_secret(channel_name: str, env_key: str) -> str:
    """Return a channel secret, falling back to env and legacy storage."""
    name = _credential_name(env_key)
    if not name:
        return ""
    file_value = secret_store.read_server_secret(
        name,
        allowed_names=_server_secret_names(),
    )
    env_value = os.environ.get(name, "")
    if file_value and env_value and file_value != env_value:
        raise secret_store.SecretStoreError(
            "channel secret file conflicts with environment"
        )
    if file_value:
        return file_value
    try:
        value = secret_store.get_secret(name, namespace=_namespace(channel_name))
        if value:
            return value
    except secret_store.SecretStoreError:
        pass
    if os.environ.get(name):
        return os.environ[name]
    return api_keys.get_key(name)


def set_channel_secret(channel_name: str, env_key: str, value: str) -> None:
    """Persist a channel secret to a channel-specific keyring namespace."""
    name = _credential_name(env_key)
    text = str(value or "").strip()
    if not name:
        return
    if secret_store.read_server_secret(
        name,
        allowed_names=_server_secret_names(),
    ):
        raise secret_store.SecretStoreError(
            "externally managed channel secret is read-only"
        )
    if not text:
        delete_channel_secret(channel_name, name)
        return
    secret_store.set_secret(name, text, namespace=_namespace(channel_name))
    os.environ[name] = text


def delete_channel_secret(channel_name: str, env_key: str) -> None:
    """Delete a channel secret from the channel namespace and process env."""
    name = _credential_name(env_key)
    if not name:
        return
    if secret_store.read_server_secret(
        name,
        allowed_names=_server_secret_names(),
    ):
        raise secret_store.SecretStoreError(
            "externally managed channel secret is read-only"
        )
    try:
        secret_store.delete_secret(name, namespace=_namespace(channel_name))
    except secret_store.SecretStoreError:
        pass
    os.environ.pop(name, None)


def import_channel_secret_from_fallback(channel_name: str, env_key: str) -> bool:
    """Persist the current env/legacy fallback value into channel keyring.

    Returns True when a fallback value was found and saved. Existing channel
    keyring values are left untouched.
    """
    name = _credential_name(env_key)
    if not name:
        return False
    try:
        if secret_store.get_secret(name, namespace=_namespace(channel_name)):
            return True
    except secret_store.SecretStoreError:
        return False
    value = os.environ.get(name) or api_keys.get_key(name)
    if not value:
        return False
    set_channel_secret(channel_name, name, value)
    return True


def migrate_legacy_channel_secrets(channels: list[Any]) -> dict[str, int]:
    """Copy legacy keyring channel credentials into channel namespaces.

    This is intentionally conservative:
    - copy only from the legacy ``api_keys`` keyring namespace
    - never delete legacy secrets
    - never persist arbitrary environment variables
    - continue after per-field failures
    """
    stats = {"migrated": 0, "skipped": 0, "failed": 0}
    for channel in channels:
        channel_name = str(getattr(channel, "name", "") or "").strip()
        if not channel_name:
            continue
        for field in getattr(channel, "config_fields", []) or []:
            if getattr(field, "storage", "") != "env":
                continue
            env_key = _credential_name(getattr(field, "env_key", ""))
            if not env_key:
                continue
            namespace = _namespace(channel_name)
            try:
                legacy_value = secret_store.get_secret(env_key)
            except secret_store.SecretStoreError:
                # A fresh server may not have a keyring backend at all. Without
                # legacy evidence there is nothing to migrate and nothing worth
                # warning about.
                stats["skipped"] += 1
                continue
            if not legacy_value:
                stats["skipped"] += 1
                continue
            try:
                if secret_store.get_secret(env_key, namespace=namespace):
                    stats["skipped"] += 1
                    continue
                secret_store.set_secret(env_key, legacy_value, namespace=namespace)
                stats["migrated"] += 1
            except secret_store.SecretStoreError as exc:
                stats["failed"] += 1
                log.warning(
                    "Failed to migrate channel credential %s for %s: %s",
                    env_key,
                    channel_name,
                    exc,
                )
    return stats


def channel_secret_status(channel_name: str, env_key: str) -> dict[str, Any]:
    """Return display-safe status for a channel secret."""
    name = _credential_name(env_key)
    if not name:
        return {"configured": False, "source": "", "fingerprint": ""}
    try:
        file_value = secret_store.read_server_secret(
            name,
            allowed_names=_server_secret_names(),
        )
    except secret_store.SecretStoreError as exc:
        return {
            "configured": False,
            "source": "secret_file",
            "fingerprint": "",
            "externally_managed": True,
            "error": str(exc),
        }
    env_value = os.environ.get(name, "")
    if file_value:
        if env_value and env_value != file_value:
            return {
                "configured": False,
                "source": "conflict",
                "fingerprint": "",
                "externally_managed": True,
                "error": "secret file conflicts with environment",
            }
        return {
            "configured": True,
            "source": "secret_file",
            "fingerprint": secret_store.fingerprint(file_value),
            "externally_managed": True,
        }
    keyring_error = ""
    try:
        value = secret_store.get_secret(name, namespace=_namespace(channel_name))
    except secret_store.SecretStoreError:
        value = None
        keyring_error = "keyring unavailable"
    if value:
        return {
            "configured": True,
            "source": "channel keyring",
            "fingerprint": secret_store.fingerprint(value),
        }
    if os.environ.get(name):
        return {
            "configured": True,
            "source": "environment",
            "fingerprint": secret_store.fingerprint(os.environ[name]),
        }
    legacy = api_keys.key_status(name)
    if legacy.get("configured"):
        return {
            "configured": True,
            "source": "legacy api_keys",
            "fingerprint": legacy.get("fingerprint", ""),
        }
    legacy_value = api_keys.get_key(name)
    if legacy_value:
        return {
            "configured": True,
            "source": "legacy api_keys",
            "fingerprint": secret_store.fingerprint(legacy_value),
        }
    result = {"configured": False, "source": "", "fingerprint": ""}
    if keyring_error:
        result["error"] = keyring_error
    return result
