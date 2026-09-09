"""Plugin loader — discovery, validation, and lifecycle.

Scans ``~/.row-bot/installed_plugins/`` for plugin directories, validates
their manifests, and loads enabled plugins safely (try/except + timeout).
"""

from __future__ import annotations

import ast
import importlib.util
import json
import logging
import os
import pathlib
import re
import signal
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from row_bot.data_paths import get_row_bot_data_dir
from row_bot.runtime_paths import app_path
from row_bot.plugins.manifest import PluginManifest, ManifestError, parse_manifest
from row_bot.plugins.api import PluginAPI, PluginTool
from row_bot.plugins import registry as plugin_registry
from row_bot.plugins import state as plugin_state

logger = logging.getLogger(__name__)

DATA_DIR = get_row_bot_data_dir()
PLUGINS_DIR = DATA_DIR / "installed_plugins"
PLUGIN_LOGS_DIR = DATA_DIR / "plugin_logs"
STALE_PLUGINS_DIR = DATA_DIR / "stale_plugins"
STALE_PLUGIN_REPORT = "stale_plugins.json"

# Timeout for plugin register() calls (seconds)
REGISTER_TIMEOUT = 5.0

# Plugin code may import this public API and ordinary third-party/local modules.
# Row-Bot internals stay behind PluginAPI so plugins cannot bypass lifecycle,
# approval, channel, MCP, or data-access boundaries.
_ALLOWED_CORE_IMPORTS = {"plugins.api", "row_bot.plugins.api"}
_FORBIDDEN_TOP_LEVEL_IMPORTS = {
    "agent",
    "app",
    "channels",
    "documents",
    "dream_cycle",
    "knowledge_graph",
    "memory",
    "memory_extraction",
    "models",
    "prompts",
    "tasks",
    "threads",
    "tools",
    "ui",
}
_FORBIDDEN_UI_FRAMEWORK_IMPORTS = {
    "gradio",
    "nicegui",
    "pywebview",
    "streamlit",
    "webview",
}
_DANGEROUS_BUILTIN_CALLS = {"eval", "exec", "__import__"}


@dataclass
class LoadResult:
    """Summary of a plugin load attempt."""
    plugin_id: str
    success: bool
    manifest: PluginManifest | None = None
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    stale: bool = False
    stale_path: str = ""


# ── Module-level state ───────────────────────────────────────────────────────
_load_results: list[LoadResult] = []
_registration_lock = threading.RLock()
_registrations: dict[str, PluginAPI] = {}


def _install_plugin_api_compat_aliases() -> None:
    """Preserve the public plugin import path for installed third-party plugins."""
    import row_bot.plugins as _plugins_pkg
    import row_bot.plugins.api as _plugins_api

    sys.modules.setdefault("plugins", _plugins_pkg)
    sys.modules.setdefault("plugins.api", _plugins_api)


# ── Public API ───────────────────────────────────────────────────────────────
def load_plugins() -> list[LoadResult]:
    """Discover and load all installed plugins. Safe to call multiple times.

    Returns a list of LoadResult objects summarising each plugin attempt.
    """
    global _load_results
    _load_results = []
    _unregister_loaded_plugins()

    # Re-read state from disk so enable/disable changes are picked up
    plugin_state.reload()

    if not PLUGINS_DIR.exists():
        PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
        logger.info("Created plugins directory: %s", PLUGINS_DIR)

    try:
        from row_bot.plugins.devtools import iter_linked_plugin_dirs

        linked_dirs = iter_linked_plugin_dirs()
    except Exception:
        linked_dirs = {}

    for entry in sorted(PLUGINS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith((".", "_")):
            continue
        if entry.name in linked_dirs:
            continue

        stale_reason = classify_stale_legacy_plugin(entry)
        result = (
            _quarantine_stale_plugin(entry, stale_reason)
            if stale_reason
            else _load_single_plugin(entry)
        )
        _load_results.append(result)

        if result.stale:
            logger.info(
                "Plugin '%s' moved aside as stale legacy plugin: %s",
                result.plugin_id,
                stale_reason,
            )
        elif result.success:
            logger.info("✅ Plugin '%s' v%s loaded (%d tools, %d skills)",
                         result.plugin_id,
                         result.manifest.version if result.manifest else "?",
                         result.manifest.tool_count if result.manifest else 0,
                         result.manifest.skill_count if result.manifest else 0)
        else:
            logger.warning("❌ Plugin '%s' failed to load: %s",
                            result.plugin_id, result.error)

        for w in result.warnings:
            logger.warning("⚠️  %s", w)

    for plugin_id, linked_dir in sorted(linked_dirs.items()):
        result = _load_single_plugin(linked_dir)
        _load_results.append(result)

        if result.success:
            logger.info(
                "Linked plugin '%s' v%s loaded",
                result.plugin_id,
                result.manifest.version if result.manifest else "?",
            )
        else:
            logger.warning("Linked plugin '%s' failed to load: %s", result.plugin_id, result.error)
        for warning in result.warnings:
            logger.warning("Plugin warning: %s", warning)

    try:
        from row_bot.developer.tool_capsules import (
            list_promoted_capsules,
            register_promoted_capsules_with_plugins,
        )

        capsule_warnings = register_promoted_capsules_with_plugins()
        for capsule in list_promoted_capsules():
            _load_results.append(
                LoadResult(
                    plugin_id=capsule.promoted_plugin_id,
                    success=True,
                    manifest=plugin_registry.get_manifest(capsule.promoted_plugin_id),
                    warnings=[
                        "Custom Tool plugin wrapper; source files remain in the tool folder.",
                    ],
                )
            )
        for warning in capsule_warnings:
            logger.warning("Custom Tool plugin warning: %s", warning)
    except Exception as exc:
        logger.debug("Custom Tool plugin registration skipped: %s", exc)

    loaded = sum(1 for r in _load_results if r.success and not r.stale)
    failed = sum(1 for r in _load_results if not r.success)
    stale = sum(1 for r in _load_results if r.stale)
    logger.info("Plugin loading complete: %d loaded, %d failed, %d stale", loaded, failed, stale)

    return _load_results


def get_load_summary() -> dict:
    """Return a summary dict for the status bar / UI."""
    return {
        "total": len(_load_results),
        "loaded": sum(1 for r in _load_results if r.success and not r.stale),
        "failed": sum(1 for r in _load_results if not r.success),
        "stale": sum(1 for r in _load_results if r.stale),
        "results": _load_results,
    }


def get_load_results() -> list[LoadResult]:
    return list(_load_results)


def get_plugin_log_path(plugin_id: str) -> pathlib.Path:
    """Return the persisted JSONL load log path for a plugin."""
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", plugin_id).strip("._")
    return PLUGIN_LOGS_DIR / f"{safe_name or 'unknown'}.jsonl"


def refresh_plugin_runtime(
    reason: str = "",
    *,
    discover_mcp: bool = True,
    clear_agent: bool = True,
) -> list[LoadResult]:
    """Reload plugin runtime state and dependent agent/MCP caches."""

    _unregister_loaded_plugins()
    results = load_plugins()
    if discover_mcp:
        try:
            from row_bot.mcp_client.runtime import discover_enabled_servers

            discover_enabled_servers()
        except Exception as exc:
            logger.debug("Plugin runtime MCP refresh skipped: %s", exc, exc_info=True)
    if clear_agent:
        try:
            from row_bot.agent import clear_agent_cache

            clear_agent_cache()
        except Exception:
            logger.debug("Plugin runtime agent cache clear skipped", exc_info=True)
    logger.info("Plugin runtime refreshed%s", f" ({reason})" if reason else "")
    return results


def _unregister_loaded_plugins() -> None:
    manifests = list(plugin_registry.get_loaded_manifests())
    with _registration_lock:
        for api in _registrations.values():
            api._revoke()
        _registrations.clear()
        for manifest in manifests:
            plugin_id = str(getattr(manifest, "id", "") or "")
            if plugin_id:
                _cleanup_plugin_runtime(plugin_id)


def revoke_registration(plugin_id: str) -> None:
    """Invalidate even a registration that has not published a manifest yet."""
    with _registration_lock:
        api = _registrations.get(plugin_id)
        if api is not None:
            api._revoke()


def _cleanup_plugin_runtime(plugin_id: str, *, expected_api: PluginAPI | None = None) -> None:
    with _registration_lock:
        if expected_api is not None and _registrations.get(plugin_id) is not expected_api:
            expected_api._revoke()
            return
        api = _registrations.pop(plugin_id, None)
        if api is not None:
            api._revoke()
        _unregister_plugin_contributions(plugin_id)


def _unregister_plugin_contributions(plugin_id: str) -> None:
    # Caller holds the registration lock through contribution removal, so a
    # replacement cannot publish between epoch revocation and stale cleanup.
    try:
        plugin_registry.unregister_plugin(plugin_id)
    except Exception:
        logger.debug("Plugin registry unregister skipped for %s", plugin_id, exc_info=True)
    try:
        from row_bot.channels import registry as channel_registry

        channel_registry.unregister_plugin_channels(plugin_id)
    except Exception:
        logger.debug("Plugin channel unregister skipped for %s", plugin_id, exc_info=True)
    try:
        from row_bot.plugins.webhooks import unregister_plugin_webhooks

        unregister_plugin_webhooks(plugin_id)
    except Exception:
        logger.debug("Plugin webhook unregister skipped for %s", plugin_id, exc_info=True)


def read_plugin_logs(plugin_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """Read recent persisted load log entries for the Plugin Center."""
    path = get_plugin_log_path(plugin_id)
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _append_plugin_log(result: LoadResult) -> None:
    try:
        PLUGIN_LOGS_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "plugin_id": result.plugin_id,
            "success": result.success,
            "version": result.manifest.version if result.manifest else "",
            "error": _redact_log_text(result.error),
            "warnings": [_redact_log_text(warning) for warning in result.warnings],
            "stale": result.stale,
            "stale_path": result.stale_path,
        }
        with open(get_plugin_log_path(result.plugin_id), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception:
        logger.debug("Plugin load log write skipped for %s", result.plugin_id, exc_info=True)


def _redact_log_text(text: str) -> str:
    if not text:
        return ""
    return re.sub(
        r"(?i)\b(api[_-]?key|token|secret|password)\b\s*[:=]\s*[^,\s;]+",
        r"\1=<redacted>",
        str(text),
    )


def classify_stale_legacy_plugin(plugin_dir: pathlib.Path) -> str | None:
    """Return a stale legacy reason for unsupported pre-v2 plugin directories."""

    manifest_path = plugin_dir / "plugin.json"
    if manifest_path.exists():
        try:
            raw = manifest_path.read_text(encoding="utf-8")
        except OSError:
            raw = ""
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {}
            if "thoth" in raw.lower():
                return "legacy Thoth manifest"
        if isinstance(data, dict) and data:
            if "min_thoth_version" in data:
                return "legacy min_thoth_version manifest"
            provides = data.get("provides")
            if isinstance(provides, dict) and "tools" in provides:
                return "legacy provides.tools manifest"
            if data.get("schema_version") != 2:
                return "missing manifest schema_version 2"

    for py_file in sorted(plugin_dir.rglob("*.py")):
        try:
            text = py_file.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
        if "thoth" in text or "min_thoth_version" in text:
            rel_path = py_file.relative_to(plugin_dir).as_posix()
            return f"legacy Thoth plugin code in {rel_path}"
    return None


def _quarantine_stale_plugin(plugin_dir: pathlib.Path, reason: str) -> LoadResult:
    plugin_id = _stale_plugin_id(plugin_dir)
    try:
        STALE_PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
        destination = _unique_stale_destination(plugin_id)
        shutil.move(str(plugin_dir), str(destination))
        _record_stale_plugin(plugin_id, plugin_dir, destination, reason)
        result = LoadResult(
            plugin_id=plugin_id,
            success=True,
            warnings=[f"Legacy plugin moved to {destination}: {reason}"],
            stale=True,
            stale_path=str(destination),
        )
        _append_plugin_log(result)
        return result
    except Exception as exc:
        return LoadResult(
            plugin_id=plugin_id,
            success=False,
            error=f"Failed to move stale legacy plugin aside: {exc}",
        )


def _stale_plugin_id(plugin_dir: pathlib.Path) -> str:
    manifest_path = plugin_dir / "plugin.json"
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if isinstance(data, dict):
        plugin_id = str(data.get("id") or "").strip()
        if plugin_id:
            return plugin_id
    return plugin_dir.name


def _unique_stale_destination(plugin_id: str) -> pathlib.Path:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", plugin_id).strip(".-") or "legacy-plugin"
    destination = STALE_PLUGINS_DIR / safe_id
    if not destination.exists():
        return destination
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    stamped = STALE_PLUGINS_DIR / f"{safe_id}-{stamp}"
    if not stamped.exists():
        return stamped
    suffix = 2
    while True:
        candidate = STALE_PLUGINS_DIR / f"{safe_id}-{stamp}-{suffix}"
        if not candidate.exists():
            return candidate
        suffix += 1


def _record_stale_plugin(
    plugin_id: str,
    source: pathlib.Path,
    destination: pathlib.Path,
    reason: str,
) -> None:
    report_path = STALE_PLUGINS_DIR / STALE_PLUGIN_REPORT
    try:
        if report_path.exists():
            raw = json.loads(report_path.read_text(encoding="utf-8"))
            report = raw if isinstance(raw, dict) else {}
        else:
            report = {}
    except (OSError, json.JSONDecodeError):
        report = {}
    entries = report.setdefault("plugins", [])
    if not isinstance(entries, list):
        entries = []
        report["plugins"] = entries
    entries.append({
        "plugin_id": plugin_id,
        "source_path": str(source),
        "destination_path": str(destination),
        "reason": reason,
        "moved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    })
    report["version"] = 1
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ── Single Plugin Loader ─────────────────────────────────────────────────────
def _load_single_plugin(plugin_dir: pathlib.Path) -> LoadResult:
    result = _load_single_plugin_impl(plugin_dir)
    _append_plugin_log(result)
    return result


def _load_single_plugin_impl(plugin_dir: pathlib.Path) -> LoadResult:
    """Load a single plugin from its directory. Never raises."""
    plugin_id = plugin_dir.name

    # Step 1: Parse manifest
    try:
        manifest = parse_manifest(plugin_dir)
        plugin_id = manifest.id
    except ManifestError as exc:
        return LoadResult(plugin_id=plugin_id, success=False, error=str(exc))
    except Exception as exc:
        return LoadResult(plugin_id=plugin_id, success=False,
                          error=f"Unexpected error parsing manifest: {exc}")

        # Step 2: Check Row-Bot version compatibility
    compat_err = _check_version_compat(manifest)
    if compat_err:
        return LoadResult(plugin_id=plugin_id, success=False,
                          manifest=manifest, error=compat_err)

    # Step 3: Check if enabled
    if not plugin_state.is_plugin_enabled(plugin_id):
        # Still register the manifest so the plugin appears in the UI
        # (users need to see the card to re-enable the plugin).
        _cleanup_plugin_runtime(plugin_id)
        plugin_registry.register_plugin(
            manifest=manifest, tools=[], skills=[],
        )
        return LoadResult(
            plugin_id=plugin_id, success=True, manifest=manifest,
            warnings=["Plugin is disabled — skipping tool/skill registration"]
        )

    # Step 4: Security scan
    sec_err = _security_scan(plugin_dir)
    if sec_err:
        return LoadResult(plugin_id=plugin_id, success=False,
                          manifest=manifest, error=sec_err)

    # Step 5: Import and call register()
    api = None
    try:
        api = PluginAPI(
            plugin_id=plugin_id,
            plugin_dir=plugin_dir,
            state_backend=plugin_state,
            staged=True,
        )
        with _registration_lock:
            old_api = _registrations.get(plugin_id)
            if old_api is not None:
                old_api._revoke()
            _registrations[plugin_id] = api
        _call_register_with_timeout(plugin_dir, api)
    except TimeoutError:
        _cleanup_plugin_runtime(plugin_id, expected_api=api)
        return LoadResult(
            plugin_id=plugin_id, success=False, manifest=manifest,
            error=f"Plugin register() timed out after {REGISTER_TIMEOUT}s"
        )
    except Exception as exc:
        _cleanup_plugin_runtime(plugin_id, expected_api=api)
        return LoadResult(
            plugin_id=plugin_id, success=False, manifest=manifest,
            error=f"Plugin register() crashed: {exc}"
        )

    # Step 6: Discover skills from skills/ directory
    # Step 7: Register with plugin registry
    try:
        skills = _discover_plugin_skills(plugin_dir)
        for skill in skills:
            api.register_skill(skill)

        with _registration_lock, api._registration_lock:
            if _registrations.get(plugin_id) is not api:
                raise RuntimeError("Plugin registration superseded")
            api._check_active()
            _validate_plugin_channels(manifest, api._registered_channels)
            warnings = plugin_registry.register_plugin(
                manifest=manifest,
                tools=api._registered_tools,
                skills=api._registered_skills,
            )
            _register_plugin_channels(manifest, api._registered_channels)
            api._publish_webhooks()
    except Exception as exc:
        _cleanup_plugin_runtime(plugin_id, expected_api=api)
        return LoadResult(
            plugin_id=plugin_id, success=False, manifest=manifest,
            error=f"Skill discovery / registry failed: {exc}"
        )

    return LoadResult(
        plugin_id=plugin_id, success=True, manifest=manifest,
        warnings=warnings,
    )


def _validate_plugin_channels(manifest: PluginManifest, channels: list[Any]) -> None:
    declared_channels = [
        str(item.get("id") or "").strip()
        for item in getattr(getattr(manifest, "provides", None), "channels", []) or []
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]
    if not channels:
        if declared_channels:
            raise ValueError(
                f"Plugin '{manifest.id}' declares channel(s) "
                f"{declared_channels} but registered none"
            )
        return
    if not declared_channels:
        registered = [str(getattr(channel, "name", "") or "") for channel in channels]
        raise ValueError(
            f"Plugin '{manifest.id}' registered channel(s) {registered} "
            "but declares no channels in plugin.json"
        )
    allowed = set(declared_channels) | {name.replace("-", "_") for name in declared_channels}
    seen = set()
    from row_bot.channels import registry as channel_registry
    for channel in channels:
        channel_name = str(getattr(channel, "name", "") or "").strip()
        if channel_name not in allowed:
            raise ValueError(
                f"Plugin '{manifest.id}' registered undeclared channel "
                f"'{channel_name}'. Declared channels: {declared_channels}"
            )
        if channel_name in seen:
            raise ValueError("Plugin registered a duplicate channel")
        seen.add(channel_name)
        existing = channel_registry.get(channel_name)
        source = channel_registry.get_source(channel_name)
        if existing is not None and (source.kind != "plugin" or source.plugin_id != manifest.id):
            raise ValueError("Plugin channel conflicts with an existing channel")


def _register_plugin_channels(manifest: PluginManifest, channels: list[Any]) -> None:
    _validate_plugin_channels(manifest, channels)
    if not channels:
        return

    from row_bot.channels import registry as channel_registry

    channel_registry.unregister_plugin_channels(manifest.id)
    source = channel_registry.ChannelSource(
        kind="plugin",
        plugin_id=manifest.id,
        label=manifest.name,
    )
    for channel in channels:
        channel_name = str(getattr(channel, "name", "") or "").strip()
        channel_registry.register(channel, source=source)


# ── Version Check ────────────────────────────────────────────────────────────
def _check_version_compat(manifest: PluginManifest) -> str | None:
    """Check min_row_bot_version. Returns error string or None."""
    try:
        from importlib.metadata import version as pkg_version
        # Try to get Row-Bot's version from various sources
        row_bot_version = _get_row_bot_version()
        if row_bot_version and manifest.min_row_bot_version:
            if _version_tuple(row_bot_version) < _version_tuple(manifest.min_row_bot_version):
                return (
                    f"Requires Row-Bot >= {manifest.min_row_bot_version}, "
                    f"but current version is {row_bot_version}"
                )
    except Exception:
        pass  # Skip version check if we can't determine Row-Bot version
    return None


def _get_row_bot_version() -> str | None:
    """Try to get Row-Bot's current version."""
    # Check if there's a VERSION file or version in app.py
    version_file = app_path("VERSION")
    if version_file.exists():
        return version_file.read_text().strip()
    # Try to extract from RELEASE_NOTES.md
    rn = app_path("RELEASE_NOTES.md")
    if rn.exists():
        try:
            with open(rn, "r", encoding="utf-8") as f:
                for line in f:
                    m = re.search(r"##\s+v(\d+\.\d+\.\d+)", line)
                    if m:
                        return m.group(1)
        except Exception:
            pass
    return None


def _version_tuple(v: str) -> tuple[int, ...]:
    """Convert '3.12.0' to (3, 12, 0)."""
    return tuple(int(x) for x in v.split(".") if x.isdigit())


# ── Security Scan ────────────────────────────────────────────────────────────
def _security_scan(plugin_dir: pathlib.Path) -> str | None:
    """Scan plugin Python files for blocked calls and core imports.

    Returns error string or None.
    """
    for py_file in plugin_dir.rglob("*.py"):
        try:
            content = py_file.read_text(encoding="utf-8")
        except Exception:
            continue
        try:
            tree = ast.parse(content, filename=str(py_file))
        except SyntaxError as exc:
            rel_path = py_file.relative_to(plugin_dir)
            return f"Security violation in {rel_path}:{exc.lineno}: Python syntax error: {exc.msg}"

        rel_path = py_file.relative_to(plugin_dir)
        error = _scan_ast_for_security_errors(tree, rel_path)
        if error:
            return error

    return None


def _scan_ast_for_security_errors(tree: ast.AST, rel_path: pathlib.Path) -> str | None:
    os_aliases = {"os"}
    os_system_aliases: set[str] = set()
    subprocess_aliases: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name
                forbidden = _forbidden_import(module)
                if forbidden:
                    return _security_error(rel_path, node.lineno, forbidden)
                if module == "os":
                    os_aliases.add(alias.asname or "os")
                if module == "subprocess" or module.startswith("subprocess."):
                    subprocess_aliases.add(alias.asname or module.split(".", 1)[0])
                    return _dangerous_error(rel_path, node.lineno, "subprocess")

        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            module = node.module or ""
            forbidden = _forbidden_import(module)
            if forbidden:
                return _security_error(rel_path, node.lineno, forbidden)
            if module == "subprocess" or module.startswith("subprocess."):
                return _dangerous_error(rel_path, node.lineno, "subprocess")
            if module == "os":
                for alias in node.names:
                    if alias.name == "system":
                        os_system_aliases.add(alias.asname or alias.name)

        elif isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                if target.id in _DANGEROUS_BUILTIN_CALLS:
                    return _dangerous_error(rel_path, node.lineno, target.id)
                if target.id in os_system_aliases:
                    return _dangerous_error(rel_path, node.lineno, "os.system")
                if target.id in subprocess_aliases:
                    return _dangerous_error(rel_path, node.lineno, "subprocess")
            elif isinstance(target, ast.Attribute):
                if (
                    target.attr == "system"
                    and isinstance(target.value, ast.Name)
                    and target.value.id in os_aliases
                ):
                    return _dangerous_error(rel_path, node.lineno, "os.system")
                if isinstance(target.value, ast.Name) and target.value.id in subprocess_aliases:
                    return _dangerous_error(rel_path, node.lineno, "subprocess")

    return None


def _forbidden_import(module: str) -> str | None:
    if not module:
        return None
    if module in _ALLOWED_CORE_IMPORTS:
        return None
    if module == "subprocess" or module.startswith("subprocess."):
        return "subprocess"
    if module == "row_bot" or module.startswith("row_bot."):
        return module
    if module == "plugins" or module.startswith("plugins."):
        return module
    top_level = module.split(".", 1)[0]
    if top_level in _FORBIDDEN_TOP_LEVEL_IMPORTS | _FORBIDDEN_UI_FRAMEWORK_IMPORTS:
        return module
    return None


def _dangerous_error(rel_path: pathlib.Path, line_no: int, name: str) -> str:
    return (
        f"Security violation in {rel_path}:{line_no}: forbidden pattern "
        f"'{name}' detected. Plugins must not use eval(), exec(), os.system(), "
        f"subprocess, or __import__()."
    )


def _security_error(rel_path: pathlib.Path, line_no: int, module: str) -> str:
    return (
        f"Security violation in {rel_path}:{line_no}: plugins must not import "
        f"module '{module}'. Use plugins.api and the native Plugin Center contract instead."
    )


# ── Plugin Registration ──────────────────────────────────────────────────────
def _call_register_with_timeout(plugin_dir: pathlib.Path, api: PluginAPI) -> None:
    """Import plugin_main.py and call register(api) with timeout."""
    main_path = plugin_dir / "plugin_main.py"
    if not main_path.exists():
        raise FileNotFoundError(f"Missing plugin_main.py in {plugin_dir}")

    # Add plugin dir to sys.path temporarily for local imports
    plugin_dir_str = str(plugin_dir)
    added_path = plugin_dir_str not in sys.path
    if added_path:
        sys.path.insert(0, plugin_dir_str)
    _install_plugin_api_compat_aliases()

    error_holder: list[Exception] = []

    def _do_register():
        try:
            spec = importlib.util.spec_from_file_location(
                f"_row_bot_plugin_{api.plugin_id}", main_path
            )
            if spec is None or spec.loader is None:
                raise ImportError(f"Cannot create module spec for {main_path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            register_fn = getattr(module, "register", None)
            if register_fn is None:
                raise AttributeError("plugin_main.py has no register() function")

            register_fn(api)
        except Exception as exc:
            error_holder.append(exc)

    thread = threading.Thread(target=_do_register, daemon=True)
    thread.start()
    thread.join(timeout=REGISTER_TIMEOUT)

    # Restore sys.path
    if added_path and plugin_dir_str in sys.path:
        sys.path.remove(plugin_dir_str)

    if thread.is_alive():
        api._revoke()
        raise TimeoutError(f"register() timed out after {REGISTER_TIMEOUT}s")

    if error_holder:
        api._revoke()
        raise error_holder[0]


# ── Skill Discovery ─────────────────────────────────────────────────────────
def _discover_plugin_skills(plugin_dir: pathlib.Path) -> list[dict]:
    """Discover SKILL.md files in plugin's skills/ directory."""
    skills_dir = plugin_dir / "skills"
    if not skills_dir.is_dir():
        return []

    import yaml
    skills: list[dict] = []

    for skill_dir in sorted(skills_dir.iterdir()):
        skill_md = None
        if skill_dir.is_dir():
            skill_md = skill_dir / "SKILL.md"
        elif skill_dir.is_file() and skill_dir.name == "SKILL.md":
            skill_md = skill_dir

        if skill_md is None or not skill_md.exists():
            continue

        try:
            content = skill_md.read_text(encoding="utf-8")
            # Parse YAML frontmatter
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    frontmatter = yaml.safe_load(parts[1]) or {}
                    instructions = parts[2].strip()
                else:
                    frontmatter = {}
                    instructions = content
            else:
                frontmatter = {}
                instructions = content

            skill_name = frontmatter.get("name", skill_dir.stem if skill_dir.is_dir() else "unnamed")
            raw_tags = frontmatter.get("tags", [])
            if isinstance(raw_tags, str):
                raw_tags = [item.strip() for item in raw_tags.split(",") if item.strip()]
            if not isinstance(raw_tags, list):
                raw_tags = []
            activation = frontmatter.get("activation", {})
            if not isinstance(activation, dict):
                activation = {}
            skills.append({
                "name": skill_name,
                "display_name": frontmatter.get("display_name", skill_name),
                "icon": frontmatter.get("icon", "🔌"),
                "description": frontmatter.get("description", ""),
                "instructions": instructions,
                "tags": [str(item) for item in raw_tags if str(item or "").strip()],
                "activation": activation,
                "version": str(frontmatter.get("version", "1.0")),
                "author": str(frontmatter.get("author", "")),
                "root": skill_md.parent,
            })
        except Exception as exc:
            logger.warning("Failed to parse skill %s: %s", skill_md, exc)

    return skills


# ── Reset (for testing) ─────────────────────────────────────────────────────
def _reset():
    """Reset load state. For testing only."""
    global _load_results
    _load_results = []
    with _registration_lock:
        for api in _registrations.values():
            api._revoke()
        _registrations.clear()
