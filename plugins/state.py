"""Plugin state persistence — enable/disable, config, and secrets.

Stores plugin state in ``~/.thoth/plugin_state.json``. Plugin secrets are
stored in the OS keyring when available, with metadata in
``~/.thoth/plugin_secrets.json``.

This module is the ONLY place plugin state is read/written.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import stat
from datetime import datetime, timezone
from typing import Any

import secret_store

logger = logging.getLogger(__name__)

DATA_DIR = pathlib.Path(os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

_STATE_PATH = DATA_DIR / "plugin_state.json"
_SECRETS_PATH = DATA_DIR / "plugin_secrets.json"
_SECRETS_VERSION = 2
_SECRETS_NAMESPACE = "plugin_secrets"

# ── In-memory caches ─────────────────────────────────────────────────────────
_state: dict[str, Any] = {}     # {plugin_id: {"enabled": bool, "config": {...}}}
_secrets: dict[str, Any] = {}
_session_secrets: dict[str, dict[str, str]] = {}
_loaded = False


# ── State I/O ────────────────────────────────────────────────────────────────
def reload():
    """Force re-read of state from disk. Call before load_plugins()."""
    global _loaded
    _loaded = False
    _ensure_loaded()


def _ensure_loaded():
    global _state, _secrets, _loaded
    if _loaded:
        return
    _state = _read_json(_STATE_PATH)
    _secrets = _read_json(_SECRETS_PATH)
    if _is_legacy_secrets_file(_secrets) and _migrate_legacy_secrets(_secrets):
        _secrets = _read_json(_SECRETS_PATH)
    _loaded = True


def _read_json(path: pathlib.Path) -> dict:
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            logger.warning("Failed to read %s", path, exc_info=True)
    return {}


def _save_state():
    _write_json(_STATE_PATH, _state)


def _save_secrets():
    _write_json(_SECRETS_PATH, _secrets, restricted=True)


def _write_json(path: pathlib.Path, data: dict, restricted: bool = False):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        if restricted and os.name != "nt":
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:
        logger.warning("Failed to write %s", path, exc_info=True)


# ── Enabled State ────────────────────────────────────────────────────────────
def is_plugin_enabled(plugin_id: str) -> bool:
    _ensure_loaded()
    return _state.get(plugin_id, {}).get("enabled", True)  # auto-enable by default


def set_plugin_enabled(plugin_id: str, enabled: bool) -> None:
    _ensure_loaded()
    _state.setdefault(plugin_id, {})["enabled"] = enabled
    _save_state()
    _invalidate_agent_cache()
    logger.info("Plugin '%s' %s", plugin_id, "enabled" if enabled else "disabled")


# ── Configuration ────────────────────────────────────────────────────────────
def get_plugin_config(plugin_id: str, key: str, default: Any = None) -> Any:
    _ensure_loaded()
    return _state.get(plugin_id, {}).get("config", {}).get(key, default)


def set_plugin_config(plugin_id: str, key: str, value: Any) -> None:
    _ensure_loaded()
    _state.setdefault(plugin_id, {}).setdefault("config", {})[key] = value
    _save_state()


def get_all_plugin_config(plugin_id: str) -> dict:
    _ensure_loaded()
    return dict(_state.get(plugin_id, {}).get("config", {}))


# ── Secrets ──────────────────────────────────────────────────────────────────
def get_plugin_secret(plugin_id: str, key: str) -> str | None:
    _ensure_loaded()
    plugin_id = str(plugin_id)
    key = str(key)
    session_value = _session_secrets.get(plugin_id, {}).get(key)
    if session_value:
        return session_value
    if _is_legacy_secrets_file(_secrets):
        value = _secrets.get(plugin_id, {}).get(key)
        return value if isinstance(value, str) and value else None
    entry = _secret_metadata_entry(plugin_id, key)
    if not entry:
        return None
    try:
        return secret_store.get_secret(_secret_name(plugin_id, key), namespace=_SECRETS_NAMESPACE)
    except secret_store.SecretStoreError:
        return None


def set_plugin_secret(plugin_id: str, key: str, value: str) -> None:
    _ensure_loaded()
    plugin_id = str(plugin_id)
    key = str(key)
    value = str(value or "").strip()
    if not value:
        delete_plugin_secret(plugin_id, key)
        return
    try:
        secret_store.set_secret(_secret_name(plugin_id, key), value, namespace=_SECRETS_NAMESPACE)
    except secret_store.SecretStoreError as exc:
        _session_secrets.setdefault(plugin_id, {})[key] = value
        logger.warning("Using session-only plugin secret storage for %s/%s: %s", plugin_id, key, exc)
        return
    _session_secrets.get(plugin_id, {}).pop(key, None)
    metadata = _ensure_secret_metadata()
    metadata.setdefault("plugins", {}).setdefault(plugin_id, {})[key] = {
        "configured": True,
        "fingerprint": secret_store.fingerprint(value),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_secrets()


def delete_plugin_secret(plugin_id: str, key: str) -> None:
    _ensure_loaded()
    plugin_id = str(plugin_id)
    key = str(key)
    try:
        secret_store.delete_secret(_secret_name(plugin_id, key), namespace=_SECRETS_NAMESPACE)
    except secret_store.SecretStoreError:
        pass
    _session_secrets.get(plugin_id, {}).pop(key, None)
    if _is_legacy_secrets_file(_secrets):
        plugin_secrets = _secrets.get(plugin_id, {})
        if isinstance(plugin_secrets, dict):
            plugin_secrets.pop(key, None)
        _save_secrets()
        return
    metadata = _ensure_secret_metadata()
    plugin_meta = metadata.setdefault("plugins", {}).get(plugin_id, {})
    if isinstance(plugin_meta, dict):
        plugin_meta.pop(key, None)
    _save_secrets()


def get_all_plugin_secrets(plugin_id: str) -> dict[str, str]:
    _ensure_loaded()
    plugin_id = str(plugin_id)
    if _is_legacy_secrets_file(_secrets):
        values = {
            str(k): str(v)
            for k, v in _secrets.get(plugin_id, {}).items()
            if isinstance(v, str) and v
        }
    else:
        values = {}
        for key in _metadata_secret_keys(plugin_id):
            value = get_plugin_secret(plugin_id, key)
            if value:
                values[key] = value
    values.update(_session_secrets.get(plugin_id, {}))
    return values


# ── Cleanup ──────────────────────────────────────────────────────────────────
def remove_plugin_state(plugin_id: str) -> None:
    """Remove all state and secrets for a plugin (on uninstall)."""
    _ensure_loaded()
    plugin_id = str(plugin_id)
    for key in set(get_all_plugin_secrets(plugin_id)) | set(_metadata_secret_keys(plugin_id)):
        try:
            secret_store.delete_secret(_secret_name(plugin_id, key), namespace=_SECRETS_NAMESPACE)
        except secret_store.SecretStoreError:
            pass
    _state.pop(plugin_id, None)
    _secrets.pop(plugin_id, None)
    if not _is_legacy_secrets_file(_secrets):
        metadata = _ensure_secret_metadata()
        plugins = metadata.setdefault("plugins", {})
        if isinstance(plugins, dict):
            plugins.pop(plugin_id, None)
    _session_secrets.pop(plugin_id, None)
    _save_state()
    _save_secrets()
    _invalidate_agent_cache()


def _secret_name(plugin_id: str, key: str) -> str:
    return f"{plugin_id}:{key}"


def _empty_secret_metadata() -> dict[str, Any]:
    return {
        "version": _SECRETS_VERSION,
        "storage": "keyring",
        "service": secret_store.SERVICE_NAME,
        "plugins": {},
    }


def _is_legacy_secrets_file(data: dict[str, Any]) -> bool:
    return bool(data) and data.get("version") != _SECRETS_VERSION


def _ensure_secret_metadata() -> dict[str, Any]:
    global _secrets
    if _is_legacy_secrets_file(_secrets):
        _secrets = _empty_secret_metadata()
    if not _secrets:
        _secrets = _empty_secret_metadata()
    _secrets.setdefault("version", _SECRETS_VERSION)
    _secrets.setdefault("storage", "keyring")
    _secrets["service"] = secret_store.SERVICE_NAME
    _secrets.setdefault("plugins", {})
    return _secrets


def _secret_metadata_entry(plugin_id: str, key: str) -> dict[str, Any]:
    plugins = _secrets.get("plugins", {}) if isinstance(_secrets, dict) else {}
    plugin_meta = plugins.get(plugin_id, {}) if isinstance(plugins, dict) else {}
    entry = plugin_meta.get(key) if isinstance(plugin_meta, dict) else None
    return dict(entry) if isinstance(entry, dict) else {}


def _metadata_secret_keys(plugin_id: str) -> list[str]:
    plugins = _secrets.get("plugins", {}) if isinstance(_secrets, dict) else {}
    plugin_meta = plugins.get(plugin_id, {}) if isinstance(plugins, dict) else {}
    if not isinstance(plugin_meta, dict):
        return []
    return [str(k) for k, v in plugin_meta.items() if isinstance(v, dict) and v.get("configured")]


def _migrate_legacy_secrets(data: dict[str, Any]) -> bool:
    legacy: dict[str, dict[str, str]] = {}
    for plugin_id, plugin_values in data.items():
        if not isinstance(plugin_values, dict):
            continue
        values = {str(k): str(v) for k, v in plugin_values.items() if isinstance(v, str) and v}
        if values:
            legacy[str(plugin_id)] = values
    if not legacy:
        _write_json(_SECRETS_PATH, _empty_secret_metadata(), restricted=True)
        return True
    try:
        for plugin_id, values in legacy.items():
            for key, value in values.items():
                secret_store.set_secret(_secret_name(plugin_id, key), value, namespace=_SECRETS_NAMESPACE)
        for plugin_id, values in legacy.items():
            for key, value in values.items():
                stored = secret_store.get_secret(_secret_name(plugin_id, key), namespace=_SECRETS_NAMESPACE)
                if stored != value:
                    raise secret_store.SecretStoreError(f"failed to verify {plugin_id}/{key}")
    except secret_store.SecretStoreError as exc:
        logger.warning("Failed to migrate plugin secrets to keyring; keeping legacy plugin_secrets.json: %s", exc)
        return False
    metadata = _empty_secret_metadata()
    for plugin_id, values in legacy.items():
        plugin_meta = metadata.setdefault("plugins", {}).setdefault(plugin_id, {})
        for key, value in values.items():
            plugin_meta[key] = {
                "configured": True,
                "fingerprint": secret_store.fingerprint(value),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
    _write_json(_SECRETS_PATH, metadata, restricted=True)
    return True


# ── Cache Invalidation ───────────────────────────────────────────────────────
def _invalidate_agent_cache():
    try:
        from agent import clear_agent_cache
        clear_agent_cache()
    except ImportError:
        pass


# ── Reset (for testing) ─────────────────────────────────────────────────────
def _reset():
    """Reset in-memory caches. For testing only."""
    global _state, _secrets, _session_secrets, _loaded
    _state = {}
    _secrets = {}
    _session_secrets = {}
    _loaded = False
