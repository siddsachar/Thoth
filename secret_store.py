"""Small OS keyring wrapper for Thoth secrets.

This module intentionally stays tiny: it delegates persistence to the
platform keyring when available and reports failures to callers instead of
falling back to plaintext files.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pathlib
from typing import Any

logger = logging.getLogger(__name__)

DATA_DIR = pathlib.Path(os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth"))


def service_name_for(data_dir: pathlib.Path | str) -> str:
    """Return the keyring service name for a Thoth data directory."""
    path = pathlib.Path(data_dir).resolve()
    return f"Thoth:{hashlib.sha256(str(path).encode('utf-8')).hexdigest()[:12]}"


SERVICE_NAME = service_name_for(DATA_DIR)

_backend_override: Any | None = None


class SecretStoreError(RuntimeError):
    """Raised when the platform secret store cannot complete an operation."""


def _backend() -> Any:
    if _backend_override is not None:
        return _backend_override
    try:
        import keyring  # type: ignore
    except Exception as exc:  # pragma: no cover - exercised through fake backends
        raise SecretStoreError(f"keyring is unavailable: {exc}") from exc
    return keyring


def _account(name: str, *, namespace: str = "api_keys") -> str:
    cleaned = str(name or "").strip()
    if not cleaned:
        raise SecretStoreError("secret name is required")
    return f"{namespace}:{cleaned}"


def is_available() -> bool:
    """Return True when the configured backend can round-trip a probe secret."""
    probe = "__thoth_keyring_probe__"
    try:
        set_secret(probe, "ok", namespace="health")
        ok = get_secret(probe, namespace="health") == "ok"
        delete_secret(probe, namespace="health")
        return ok
    except SecretStoreError:
        return False


def get_secret(name: str, *, namespace: str = "api_keys", service: str | None = None) -> str | None:
    """Return a stored secret, or None if it is unset/unavailable."""
    try:
        value = _backend().get_password(service or SERVICE_NAME, _account(name, namespace=namespace))
    except Exception as exc:
        logger.warning("Failed to read secret %s from keyring", name, exc_info=True)
        raise SecretStoreError(str(exc)) from exc
    return value if isinstance(value, str) and value else None


def set_secret(name: str, value: str, *, namespace: str = "api_keys", service: str | None = None) -> None:
    """Persist a secret in the OS keyring."""
    if value is None:
        delete_secret(name, namespace=namespace, service=service)
        return
    try:
        _backend().set_password(service or SERVICE_NAME, _account(name, namespace=namespace), str(value))
    except Exception as exc:
        logger.warning("Failed to write secret %s to keyring", name, exc_info=True)
        raise SecretStoreError(str(exc)) from exc


def delete_secret(name: str, *, namespace: str = "api_keys", service: str | None = None) -> None:
    """Remove a secret from the OS keyring if it exists."""
    try:
        _backend().delete_password(service or SERVICE_NAME, _account(name, namespace=namespace))
    except Exception as exc:
        message = str(exc).lower()
        if "not found" in message or "not exist" in message or "no such" in message:
            return
        logger.warning("Failed to delete secret %s from keyring", name, exc_info=True)
        raise SecretStoreError(str(exc)) from exc


def fingerprint(value: str) -> str:
    """Return a display-safe fingerprint for a secret value."""
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 4:
        return "****"
    return f"****{text[-4:]}"


def _set_backend_for_tests(backend: Any | None) -> None:
    """Install a fake backend for focused tests."""
    global _backend_override
    _backend_override = backend
