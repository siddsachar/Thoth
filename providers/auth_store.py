from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import Any

import api_keys
import secret_store

from providers.config import update_provider_config
from providers.models import AuthMethod, ProviderHealth

PROVIDER_API_KEY_ENV: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "ollama_cloud": "OLLAMA_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "xai": "XAI_API_KEY",
    "minimax": "MINIMAX_API_KEY",
}
PROVIDER_SECRET_CHUNK_SIZE = 512
CHUNK_MARKER_SUFFIX = "__chunks"
CHUNK_VALUE_PREFIX = "v1:"


def _namespace(provider_id: str) -> str:
    return f"providers:{provider_id}"


def _credential_name(credential_name: str) -> str:
    return str(credential_name or "api_key").strip() or "api_key"


def _chunk_marker_name(name: str) -> str:
    return f"{name}.{CHUNK_MARKER_SUFFIX}"


def _chunk_name(name: str, index: int) -> str:
    return f"{name}.__chunk.{index:04d}"


def _chunk_count(provider_id: str, name: str) -> int:
    try:
        marker = secret_store.get_secret(_chunk_marker_name(name), namespace=_namespace(provider_id)) or ""
    except secret_store.SecretStoreError:
        return 0
    if marker.startswith(CHUNK_VALUE_PREFIX):
        marker = marker.removeprefix(CHUNK_VALUE_PREFIX)
    try:
        return max(0, int(marker))
    except ValueError:
        return 0


def _delete_chunked_provider_secret(provider_id: str, name: str) -> None:
    count = _chunk_count(provider_id, name)
    for index in range(count):
        try:
            secret_store.delete_secret(_chunk_name(name, index), namespace=_namespace(provider_id))
        except secret_store.SecretStoreError:
            pass
    try:
        secret_store.delete_secret(_chunk_marker_name(name), namespace=_namespace(provider_id))
    except secret_store.SecretStoreError:
        pass


def _set_provider_secret_value(provider_id: str, name: str, value: str) -> None:
    _delete_chunked_provider_secret(provider_id, name)
    try:
        secret_store.delete_secret(name, namespace=_namespace(provider_id))
    except secret_store.SecretStoreError:
        pass
    text = str(value)
    if len(text) <= PROVIDER_SECRET_CHUNK_SIZE:
        try:
            secret_store.set_secret(name, text, namespace=_namespace(provider_id))
            return
        except secret_store.SecretStoreError:
            pass
    chunks = [text[index:index + PROVIDER_SECRET_CHUNK_SIZE] for index in range(0, len(text), PROVIDER_SECRET_CHUNK_SIZE)]
    if not chunks:
        chunks = [""]
    for index, chunk in enumerate(chunks):
        secret_store.set_secret(_chunk_name(name, index), chunk, namespace=_namespace(provider_id))
    secret_store.set_secret(_chunk_marker_name(name), f"{CHUNK_VALUE_PREFIX}{len(chunks)}", namespace=_namespace(provider_id))


def _get_provider_secret_value(provider_id: str, name: str) -> str:
    count = _chunk_count(provider_id, name)
    if count:
        parts: list[str] = []
        for index in range(count):
            try:
                part = secret_store.get_secret(_chunk_name(name, index), namespace=_namespace(provider_id)) or ""
            except secret_store.SecretStoreError:
                return ""
            parts.append(part)
        return "".join(parts)
    try:
        return secret_store.get_secret(name, namespace=_namespace(provider_id)) or ""
    except secret_store.SecretStoreError:
        return ""


def set_provider_secret(
    provider_id: str,
    credential_name: str,
    value: str,
    *,
    source: str = "keyring",
    auth_method: AuthMethod | str | None = None,
) -> None:
    provider_id = str(provider_id).strip()
    name = _credential_name(credential_name)
    _set_provider_secret_value(provider_id, name, value)
    fingerprint = secret_store.fingerprint(value)
    now = datetime.now(timezone.utc).isoformat()
    method = auth_method or (AuthMethod.API_KEY if name == "api_key" else AuthMethod.CUSTOM)
    method_value = method.value if isinstance(method, AuthMethod) else str(method)

    def _update(cfg: dict[str, Any]) -> None:
        providers = cfg.setdefault("providers", {})
        entry = providers.setdefault(provider_id, {})
        entry.update({
            "provider_id": provider_id,
            "auth_method": method_value,
            "health": ProviderHealth.CONNECTED.value,
            "configured": True,
            "source": source,
            "fingerprint": fingerprint,
            "updated_at": now,
            "last_error": "",
        })

    update_provider_config(_update)


def get_provider_secret(provider_id: str, credential_name: str = "api_key") -> str:
    provider_id = str(provider_id).strip()
    name = _credential_name(credential_name)
    if name == "api_key":
        env_var = PROVIDER_API_KEY_ENV.get(provider_id)
        if env_var:
            legacy_value = api_keys.get_key(env_var)
            if legacy_value:
                return legacy_value
    try:
        return _get_provider_secret_value(provider_id, name)
    except secret_store.SecretStoreError:
        return ""


def delete_provider_secret(provider_id: str, credential_name: str = "api_key") -> None:
    provider_id = str(provider_id).strip()
    name = _credential_name(credential_name)
    _delete_chunked_provider_secret(provider_id, name)
    try:
        secret_store.delete_secret(name, namespace=_namespace(provider_id))
    except secret_store.SecretStoreError:
        pass

    def _update(cfg: dict[str, Any]) -> None:
        entry = cfg.setdefault("providers", {}).setdefault(provider_id, {})
        entry.update({
            "provider_id": provider_id,
            "configured": bool(get_provider_secret(provider_id, name)),
            "health": ProviderHealth.UNKNOWN.value,
            "fingerprint": "",
            "last_error": "",
        })

    update_provider_config(_update)


def provider_secret_status(provider_id: str, credential_name: str = "api_key") -> dict[str, Any]:
    provider_id = str(provider_id).strip()
    name = _credential_name(credential_name)
    value = ""
    source = ""
    if name == "api_key" and provider_id in PROVIDER_API_KEY_ENV:
        env_var = PROVIDER_API_KEY_ENV[provider_id]
        env_value = os.environ.get(env_var, "")
        status = api_keys.key_status(env_var)
        value = api_keys.get_key(env_var)
        source = str(status.get("source") or "")
        if value:
            fingerprint = str(status.get("fingerprint") or secret_store.fingerprint(value))
            if env_value and source == "keyring":
                try:
                    stored_value = secret_store.get_secret(env_var) or ""
                except secret_store.SecretStoreError:
                    stored_value = ""
                is_override = (
                    env_value != stored_value
                    if stored_value
                    else secret_store.fingerprint(env_value) != fingerprint
                )
                if is_override:
                    return {
                        "configured": True,
                        "source": "environment",
                        "fingerprint": secret_store.fingerprint(env_value),
                    }
            return {
                "configured": True,
                "source": source or "api_keys",
                "fingerprint": fingerprint,
            }
    try:
        value = _get_provider_secret_value(provider_id, name)
        source = "keyring" if value else ""
    except secret_store.SecretStoreError:
        return {"configured": False, "source": "", "fingerprint": "", "error": "keyring unavailable"}
    return {
        "configured": bool(value),
        "source": source,
        "fingerprint": secret_store.fingerprint(value),
    }
