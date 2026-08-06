"""Secure storage adapters for Row-Bot secrets.

The platform keyring remains the default. An explicitly keyed server deployment
can use encrypted records in its persistent data directory when the platform
backend is unavailable. This module never falls back to plaintext files.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pathlib
import re
import secrets
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
PERSISTENT_SERVER_SECRET_KEY_NAME = "ROW_BOT_SECRET_STORE_KEY"
PERSISTENT_SERVER_SECRET_DIR_NAME = "secure-secrets"
_PERSISTENT_SERVER_SECRET_KEY = re.compile(r"^[0-9a-fA-F]{64}$")
_PERSISTENT_SERVER_SECRET_MAGIC = b"ROWBOT-SECRET-V1\x00"
_PERSISTENT_SERVER_SECRET_NONCE_BYTES = 12
MAX_PERSISTENT_SERVER_SECRET_BYTES = MAX_SERVER_SECRET_BYTES + 1024

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


def _persistent_server_secret_key() -> bytes | None:
    value = read_server_secret(
        PERSISTENT_SERVER_SECRET_KEY_NAME,
        allowed_names={PERSISTENT_SERVER_SECRET_KEY_NAME},
    )
    if value is None:
        return None
    if not _PERSISTENT_SERVER_SECRET_KEY.fullmatch(value):
        raise SecretStoreError(
            f"{PERSISTENT_SERVER_SECRET_KEY_NAME} must contain exactly 64 hexadecimal characters"
        )
    return bytes.fromhex(value)


def persistent_server_store_configured() -> bool:
    """Return whether an explicit encrypted server secret store is configured."""
    return _persistent_server_secret_key() is not None


def _persistent_server_secret_directory() -> pathlib.Path:
    configured = str(os.environ.get(APP_DATA_DIR_ENV) or "").strip()
    data_dir = pathlib.Path(configured) if configured else DATA_DIR
    return data_dir / PERSISTENT_SERVER_SECRET_DIR_NAME


def _persistent_server_secret_identity(service: str, account: str) -> tuple[pathlib.Path, bytes]:
    identity = f"{service}\x00{account}".encode("utf-8")
    filename = f"{hashlib.sha256(identity).hexdigest()}.secret"
    return _persistent_server_secret_directory() / filename, identity


def _prepare_persistent_server_secret_directory() -> pathlib.Path:
    directory = _persistent_server_secret_directory()
    if directory.is_symlink():
        raise SecretStoreError("persistent server secret directory must not be a symlink")
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not directory.is_dir():
            raise SecretStoreError("persistent server secret path must be a directory")
        os.chmod(directory, 0o700)
    except SecretStoreError:
        raise
    except OSError as exc:
        raise SecretStoreError("persistent server secret directory is unavailable") from exc
    return directory


def _read_persistent_server_secret(
    service: str,
    account: str,
) -> tuple[bool, str | None]:
    key = _persistent_server_secret_key()
    if key is None:
        return False, None
    path, identity = _persistent_server_secret_identity(service, account)
    try:
        if path.is_symlink():
            raise SecretStoreError("persistent server secret record must not be a symlink")
        info = path.stat()
    except FileNotFoundError:
        return True, None
    except SecretStoreError:
        raise
    except OSError as exc:
        raise SecretStoreError("persistent server secret record cannot be inspected") from exc
    if not stat.S_ISREG(info.st_mode):
        raise SecretStoreError("persistent server secret record must be regular")
    if info.st_size > MAX_PERSISTENT_SERVER_SECRET_BYTES:
        raise SecretStoreError("persistent server secret record is too large")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise SecretStoreError("persistent server secret record cannot be read") from exc
    prefix_size = len(_PERSISTENT_SERVER_SECRET_MAGIC)
    nonce_end = prefix_size + _PERSISTENT_SERVER_SECRET_NONCE_BYTES
    if not payload.startswith(_PERSISTENT_SERVER_SECRET_MAGIC) or len(payload) <= nonce_end:
        raise SecretStoreError("persistent server secret record has an unsupported format")
    nonce = payload[prefix_size:nonce_end]
    ciphertext = payload[nonce_end:]
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        plaintext = AESGCM(key).decrypt(nonce, ciphertext, identity)
        value = plaintext.decode("utf-8")
    except Exception as exc:
        raise SecretStoreError("persistent server secret record could not be decrypted") from exc
    return True, value or None


def _write_persistent_server_secret(service: str, account: str, value: str) -> bool:
    key = _persistent_server_secret_key()
    if key is None:
        return False
    directory = _prepare_persistent_server_secret_directory()
    path, identity = _persistent_server_secret_identity(service, account)
    if path.exists() or path.is_symlink():
        _read_persistent_server_secret(service, account)
    nonce = secrets.token_bytes(_PERSISTENT_SERVER_SECRET_NONCE_BYTES)
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        ciphertext = AESGCM(key).encrypt(nonce, str(value).encode("utf-8"), identity)
    except Exception as exc:
        raise SecretStoreError("persistent server secret could not be encrypted") from exc
    payload = _PERSISTENT_SERVER_SECRET_MAGIC + nonce + ciphertext
    temporary = directory / f".{path.name}.{secrets.token_hex(8)}.tmp"
    file_descriptor: int | None = None
    try:
        file_descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(file_descriptor, "wb") as handle:
            file_descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        if file_descriptor is not None:
            os.close(file_descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise SecretStoreError("persistent server secret record could not be written") from exc
    return True


def _delete_persistent_server_secret(service: str, account: str) -> bool:
    key = _persistent_server_secret_key()
    if key is None:
        return False
    path, _identity = _persistent_server_secret_identity(service, account)
    if path.is_symlink():
        raise SecretStoreError("persistent server secret record must not be a symlink")
    if path.exists():
        _read_persistent_server_secret(service, account)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise SecretStoreError("persistent server secret record could not be deleted") from exc
    return True


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
    resolved_service = service or SERVICE_NAME
    account = _account(name, namespace=namespace)
    try:
        value = _backend().get_password(resolved_service, account)
    except Exception as exc:
        if _is_unavailable_error(exc):
            configured, stored = _read_persistent_server_secret(resolved_service, account)
            if configured:
                return stored
        _raise_secret_error("read", name, exc)
    if isinstance(value, str) and value:
        return value
    configured, stored = _read_persistent_server_secret(resolved_service, account)
    return stored if configured else None


def set_secret(name: str, value: str, *, namespace: str = "api_keys", service: str | None = None) -> str:
    """Persist a secret and return the secure storage backend used."""
    if value is None:
        delete_secret(name, namespace=namespace, service=service)
        return ""
    resolved_service = service or SERVICE_NAME
    account = _account(name, namespace=namespace)
    try:
        _backend().set_password(resolved_service, account, str(value))
    except Exception as exc:
        if _is_unavailable_error(exc) and _write_persistent_server_secret(
            resolved_service,
            account,
            str(value),
        ):
            return "encrypted_file"
        _raise_secret_error("write", name, exc)
    return "keyring"


def delete_secret(name: str, *, namespace: str = "api_keys", service: str | None = None) -> None:
    """Remove a secret from the OS keyring if it exists."""
    resolved_service = service or SERVICE_NAME
    account = _account(name, namespace=namespace)
    try:
        _backend().delete_password(resolved_service, account)
    except Exception as exc:
        message = str(exc).lower()
        if "not found" in message or "not exist" in message or "no such" in message:
            _delete_persistent_server_secret(resolved_service, account)
            return
        if _is_unavailable_error(exc) and _delete_persistent_server_secret(
            resolved_service,
            account,
        ):
            return
        _raise_secret_error("delete", name, exc)
    _delete_persistent_server_secret(resolved_service, account)


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
