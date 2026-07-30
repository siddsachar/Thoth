"""Small OS keyring wrapper for Row-Bot secrets.

This module intentionally stays tiny: it delegates persistence to the
platform keyring when available and reports failures to callers instead of
falling back to plaintext files.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pathlib
import re
import stat
from typing import Any

from row_bot.brand import APP_DATA_DIR_ENV, KEYRING_SERVICE_PREFIX, default_data_dir

logger = logging.getLogger(__name__)

DATA_DIR = pathlib.Path(os.environ.get(APP_DATA_DIR_ENV) or default_data_dir())


def service_name_for(data_dir: pathlib.Path | str) -> str:
    """Return the keyring service name for a Row-Bot data directory."""
    path = pathlib.Path(data_dir).resolve()
    return f"{KEYRING_SERVICE_PREFIX}:{hashlib.sha256(str(path).encode('utf-8')).hexdigest()[:12]}"


SERVICE_NAME = service_name_for(DATA_DIR)
SERVER_SECRETS_DIR_ENV = "ROW_BOT_SECRETS_DIR"
DEFAULT_SERVER_SECRETS_DIR = pathlib.Path("/run/secrets")
MAX_SERVER_SECRET_BYTES = 64 * 1024
_SERVER_SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")

_backend_override: Any | None = None


def _docs_capture_active() -> bool:
    return str(os.environ.get("ROW_BOT_DOCS_CAPTURE") or "").strip().lower() in {"1", "true", "yes", "on"}


class SecretStoreError(RuntimeError):
    """Raised when the platform secret store cannot complete an operation."""


def server_secrets_dir() -> pathlib.Path | None:
    """Return the explicitly configured server secret directory, if active."""
    configured = str(os.environ.get(SERVER_SECRETS_DIR_ENV) or "").strip()
    if configured:
        return pathlib.Path(configured)
    deployment = str(os.environ.get("ROW_BOT_DEPLOYMENT_MODE") or "").lower()
    if deployment == "server" and DEFAULT_SERVER_SECRETS_DIR.exists():
        return DEFAULT_SERVER_SECRETS_DIR
    return None


def read_server_secret(
    name: str,
    *,
    allowed_names: set[str] | frozenset[str],
) -> str | None:
    """Read an allowlisted regular secret file without copying it to data."""
    secret_name = str(name or "").strip()
    directory = server_secrets_dir()
    if directory is None:
        return None
    allowed = frozenset(str(value) for value in allowed_names)
    if not _SERVER_SECRET_NAME.fullmatch(secret_name) or secret_name not in allowed:
        raise SecretStoreError("server secret name is not allowlisted")
    try:
        base = directory.resolve(strict=True)
    except FileNotFoundError:
        return None
    candidate = base / secret_name
    try:
        if candidate.is_symlink():
            raise SecretStoreError("server secret file must not be a symlink")
        info = candidate.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SecretStoreError("server secret file cannot be inspected") from exc
    if not stat.S_ISREG(info.st_mode):
        raise SecretStoreError("server secret file must be regular")
    if info.st_size > MAX_SERVER_SECRET_BYTES:
        raise SecretStoreError("server secret file is too large")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(base)
        raw = resolved.read_bytes()
    except (OSError, ValueError) as exc:
        raise SecretStoreError("server secret file is outside the configured directory") from exc
    if len(raw) > MAX_SERVER_SECRET_BYTES:
        raise SecretStoreError("server secret file is too large")
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SecretStoreError("server secret file must be UTF-8") from exc
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    return value or None


def server_secret_status(
    name: str,
    *,
    allowed_names: set[str] | frozenset[str],
) -> dict[str, object]:
    """Return value-free externally-managed secret status."""
    value = read_server_secret(name, allowed_names=allowed_names)
    return {
        "configured": bool(value),
        "source": "secret_file" if value else "",
        "externally_managed": bool(value),
        "fingerprint": fingerprint(value or ""),
    }


def _is_unavailable_error(exc: BaseException) -> bool:
    """Return True for expected missing/disabled keyring backend failures."""
    name = exc.__class__.__name__.lower()
    module = exc.__class__.__module__.lower()
    message = str(exc).lower()
    return (
        "nokeyring" in name
        or "keyring.errors" in module and "backend" in message and "available" in message
        or "no recommended backend" in message
        or "keyring is unavailable" in message
        or "keyring unavailable" in message
    )


def _raise_secret_error(action: str, name: str, exc: BaseException) -> None:
    if not _is_unavailable_error(exc):
        logger.warning("Failed to %s secret %s from keyring", action, name, exc_info=True)
    raise SecretStoreError(str(exc)) from exc


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
    if _docs_capture_active():
        return False
    probe = "__row_bot_keyring_probe__"
    try:
        set_secret(probe, "ok", namespace="health")
        ok = get_secret(probe, namespace="health") == "ok"
        delete_secret(probe, namespace="health")
        return ok
    except SecretStoreError:
        return False


def get_secret(name: str, *, namespace: str = "api_keys", service: str | None = None) -> str | None:
    """Return a stored secret, or None if it is unset/unavailable."""
    if _docs_capture_active():
        return None
    try:
        value = _backend().get_password(service or SERVICE_NAME, _account(name, namespace=namespace))
    except Exception as exc:
        _raise_secret_error("read", name, exc)
    return value if isinstance(value, str) and value else None


def set_secret(name: str, value: str, *, namespace: str = "api_keys", service: str | None = None) -> None:
    """Persist a secret in the OS keyring."""
    if value is None:
        delete_secret(name, namespace=namespace, service=service)
        return
    try:
        _backend().set_password(service or SERVICE_NAME, _account(name, namespace=namespace), str(value))
    except Exception as exc:
        _raise_secret_error("write", name, exc)


def delete_secret(name: str, *, namespace: str = "api_keys", service: str | None = None) -> None:
    """Remove a secret from the OS keyring if it exists."""
    try:
        _backend().delete_password(service or SERVICE_NAME, _account(name, namespace=namespace))
    except Exception as exc:
        message = str(exc).lower()
        if "not found" in message or "not exist" in message or "no such" in message:
            return
        _raise_secret_error("delete", name, exc)


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
