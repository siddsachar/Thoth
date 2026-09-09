"""Plugin registry — separate from tools.registry.

Stores loaded plugin tools and skills, provides LangChain-compatible
tool wrappers and skills prompt text for injection into the agent.

This registry is completely independent of the core tools/registry.py.
"""

from __future__ import annotations

from collections.abc import Iterable
import logging
from functools import wraps
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from row_bot.plugins.api import PluginTool
    from row_bot.plugins.manifest import PluginManifest

logger = logging.getLogger(__name__)

# ── Internal storage ─────────────────────────────────────────────────────────
_plugin_tools: dict[str, "PluginTool"] = {}       # tool_name → tool instance
_plugin_skills: dict[str, dict] = {}              # skill_name → skill info dict
_loaded_manifests: dict[str, "PluginManifest"] = {}  # plugin_id → manifest
_tool_to_plugin: dict[str, str] = {}              # tool_name → plugin_id
_skill_to_plugin: dict[str, str] = {}             # skill_name → plugin_id


# ── Registration (called by loader) ──────────────────────────────────────────
def register_plugin(manifest: "PluginManifest",
                     tools: list["PluginTool"],
                     skills: list[dict]) -> list[str]:
    """Register a loaded plugin's tools and skills.

    Returns a list of warnings (e.g. name collisions with built-in tools).
    """
    warnings: list[str] = []
    _loaded_manifests[manifest.id] = manifest

    for tool in tools:
        # Check for collision with built-in tools
        if _name_collides_with_builtin(tool.name):
            warnings.append(
                f"Plugin '{manifest.id}' tool '{tool.name}' collides with a "
                f"built-in tool — skipped"
            )
            continue
        # Check for collision with other plugin tools
        if tool.name in _plugin_tools:
            other = _tool_to_plugin.get(tool.name, "unknown")
            warnings.append(
                f"Plugin '{manifest.id}' tool '{tool.name}' collides with "
                f"plugin '{other}' — skipped"
            )
            continue
        _plugin_tools[tool.name] = tool
        _tool_to_plugin[tool.name] = manifest.id

    for skill in skills:
        skill_name = skill.get("name", "")
        if not skill_name:
            continue
        storage_key = f"{manifest.id}:{skill_name}"
        if storage_key in _plugin_skills:
            other = _skill_to_plugin.get(storage_key, "unknown")
            warnings.append(
                f"Plugin '{manifest.id}' skill '{skill_name}' collides with "
                f"plugin '{other}' — skipped"
            )
            continue
        _plugin_skills[storage_key] = skill
        _skill_to_plugin[storage_key] = manifest.id

    return warnings


# ── Public API ───────────────────────────────────────────────────────────────
def _allow_set(allow_names: Iterable[str] | None) -> set[str] | None:
    if allow_names is None:
        return None
    return {str(name) for name in allow_names if str(name or "").strip()}


def _plugin_runtime_allowed(
    *,
    allow: set[str] | None,
    runtime_name: str,
    parent_name: str,
    plugin_id: str,
) -> bool:
    if allow is None:
        return True
    return runtime_name in allow or parent_name in allow or plugin_id in allow


def _plugin_mcp_allow_names(plugin_id: str, allow: set[str] | None) -> set[str] | None:
    if allow is None or plugin_id in allow:
        return None
    return allow


def _plugin_native_runtime_names(plugin_id: str) -> set[str]:
    names: set[str] = set()
    for tool_name, tool in _plugin_tools.items():
        if _tool_to_plugin.get(tool_name) != plugin_id:
            continue
        names.add(tool_name)
        try:
            for lc_tool in tool.as_langchain_tools():
                runtime_name = str(getattr(lc_tool, "name", "") or tool_name).strip()
                if runtime_name:
                    names.add(runtime_name)
        except Exception as exc:
            logger.debug(
                "Plugin native tool name lookup skipped for %s/%s: %s",
                plugin_id,
                tool_name,
                exc,
                exc_info=True,
            )
    return names


def _plugin_mcp_runtime_names(plugin_id: str) -> set[str]:
    names: set[str] = set()
    manifest = _loaded_manifests.get(plugin_id)
    if not manifest or not getattr(getattr(manifest, "provides", None), "mcp_servers", []):
        return names
    try:
        from row_bot.mcp_client import runtime as mcp_runtime

        for record in mcp_runtime.get_plugin_tool_records(plugin_id):
            for key in ("runtime_name", "parent_name", "server_name"):
                name = str(record.get(key) or "").strip()
                if name:
                    names.add(name)
    except Exception as exc:
        logger.debug(
            "Plugin MCP tool name lookup skipped for %s: %s",
            plugin_id,
            exc,
            exc_info=True,
        )
    return names


def _plugin_skill_allowed(plugin_id: str, allow: set[str] | None) -> bool:
    if allow is None:
        return True
    if plugin_id in allow:
        return True
    if _plugin_native_runtime_names(plugin_id) & allow:
        return True
    if _plugin_mcp_runtime_names(plugin_id) & allow:
        return True
    return False


def get_langchain_tools(
    allow_names: Iterable[str] | None = None,
    *,
    refresh_mcp: bool = True,
) -> list:
    """Return LangChain tool wrappers for all tools from enabled plugins."""
    from row_bot.plugins import state

    allow = _allow_set(allow_names)
    tools = []
    for tool_name, tool in _plugin_tools.items():
        plugin_id = _tool_to_plugin.get(tool_name)
        if plugin_id and state.is_plugin_enabled(plugin_id):
            for lc_tool in tool.as_langchain_tools():
                runtime_name = str(getattr(lc_tool, "name", "") or tool_name)
                if _plugin_runtime_allowed(
                    allow=allow,
                    runtime_name=runtime_name,
                    parent_name=tool_name,
                    plugin_id=plugin_id,
                ):
                    tools.append(_guard_tool(lc_tool, plugin_id, tool_name, tool))
    try:
        from row_bot.mcp_client import runtime as mcp_runtime

        for plugin_id, manifest in _loaded_manifests.items():
            if not state.is_plugin_enabled(plugin_id):
                continue
            if not getattr(getattr(manifest, "provides", None), "mcp_servers", []):
                continue
            kwargs = {"allow_names": _plugin_mcp_allow_names(plugin_id, allow)}
            if not refresh_mcp:
                kwargs["refresh"] = False
            try:
                tools.extend(mcp_runtime.get_plugin_langchain_tools(plugin_id, **kwargs))
            except TypeError:
                kwargs.pop("refresh", None)
                tools.extend(mcp_runtime.get_plugin_langchain_tools(plugin_id, **kwargs))
    except Exception as exc:
        logger.debug("Plugin MCP tool injection skipped: %s", exc, exc_info=True)
    return tools


def _guard_tool(lc_tool, plugin_id: str, tool_name: str, original):
    """A retained graph callable cannot outlive its registration or enablement."""
    from row_bot.plugins import state

    def check() -> None:
        if (not state.is_plugin_enabled(plugin_id)
                or _plugin_tools.get(tool_name) is not original
                or _tool_to_plugin.get(tool_name) != plugin_id):
            raise RuntimeError("Plugin capability was revoked")
        api = getattr(original, "plugin_api", None)
        if api is not None:
            api._check_dispatch()

    updates = {}
    func = getattr(lc_tool, "func", None)
    if func is not None:
        @wraps(func)
        def guarded(*args, **kwargs):
            check()
            return func(*args, **kwargs)
        updates["func"] = guarded
    coroutine = getattr(lc_tool, "coroutine", None)
    if coroutine is not None:
        @wraps(coroutine)
        async def guarded_async(*args, **kwargs):
            check()
            return await coroutine(*args, **kwargs)
        updates["coroutine"] = guarded_async
    return lc_tool.model_copy(update=updates) if updates else lc_tool


def get_skills_prompt(allow_names: Iterable[str] | None = None) -> str:
    """Return skills prompt text from enabled plugins allowed for this runtime."""
    from row_bot.plugins import state

    allow = _allow_set(allow_names)
    parts: list[str] = []
    allowed_plugins: dict[str, bool] = {}
    for storage_key, skill_info in _plugin_skills.items():
        plugin_id = _skill_to_plugin.get(storage_key)
        if not plugin_id or not state.is_plugin_enabled(plugin_id):
            continue
        if plugin_id not in allowed_plugins:
            allowed_plugins[plugin_id] = _plugin_skill_allowed(plugin_id, allow)
        if not allowed_plugins[plugin_id]:
            continue
        instructions = skill_info.get("instructions", "")
        if instructions:
            icon = skill_info.get("icon", "🔌")
            display = skill_info.get("display_name", skill_info.get("name", storage_key))
            manifest = _loaded_manifests.get(plugin_id)
            plugin_name = str(getattr(manifest, "name", "") or plugin_id)
            parts.append(f"\n### {icon} {display} (plugin: {plugin_name})\n{instructions}\n")

    if not parts:
        return ""

    return (
        "## Plugin Skills\n\n"
        "The following skills are provided by installed plugins.\n"
        + "\n".join(parts)
    )


def get_plugin_tool_names() -> list[str]:
    """Return names of all registered plugin tools (enabled or not)."""
    return list(_plugin_tools.keys())


def get_destructive_names(allow_names: Iterable[str] | None = None) -> set[str]:
    """Return destructive sub-tool names from all enabled plugin tools."""
    from row_bot.plugins import state

    allow = _allow_set(allow_names)
    names: set[str] = set()
    for tool_name, tool in _plugin_tools.items():
        plugin_id = _tool_to_plugin.get(tool_name)
        if plugin_id and state.is_plugin_enabled(plugin_id):
            if allow is None or tool_name in allow or plugin_id in allow:
                names.update(tool.destructive_tool_names)
            else:
                names.update(name for name in tool.destructive_tool_names if name in allow)
    try:
        from row_bot.mcp_client import runtime as mcp_runtime

        for plugin_id, manifest in _loaded_manifests.items():
            if not state.is_plugin_enabled(plugin_id):
                continue
            if not getattr(getattr(manifest, "provides", None), "mcp_servers", []):
                continue
            names.update(
                mcp_runtime.get_plugin_destructive_tool_names(
                    plugin_id,
                    allow_names=_plugin_mcp_allow_names(plugin_id, allow),
                )
            )
    except Exception as exc:
        logger.debug("Plugin MCP destructive lookup skipped: %s", exc, exc_info=True)
    return names


def get_background_allowed_names() -> set[str]:
    """Return background-allowed destructive sub-tool names from all enabled plugin tools."""
    from row_bot.plugins import state

    names: set[str] = set()
    for tool_name, tool in _plugin_tools.items():
        plugin_id = _tool_to_plugin.get(tool_name)
        if plugin_id and state.is_plugin_enabled(plugin_id):
            names.update(tool.background_allowed_tool_names)
    return names


def get_enabled_plugin_tool_names() -> list[str]:
    """Return names of tools from enabled plugins only."""
    from row_bot.plugins import state
    names = [
        name for name, tool in _plugin_tools.items()
        if state.is_plugin_enabled(_tool_to_plugin.get(name, ""))
    ]
    try:
        from row_bot.mcp_client import runtime as mcp_runtime

        for plugin_id, manifest in _loaded_manifests.items():
            if not state.is_plugin_enabled(plugin_id):
                continue
            if not getattr(getattr(manifest, "provides", None), "mcp_servers", []):
                continue
            names.extend(
                record["runtime_name"]
                for record in mcp_runtime.get_plugin_tool_records(plugin_id)
            )
    except Exception as exc:
        logger.debug("Plugin MCP name lookup skipped: %s", exc, exc_info=True)
    return names


def get_enabled_plugin_tool_records() -> list[dict]:
    """Return enabled plugin runtime tool metadata for Agent Profile cataloging."""
    from row_bot.plugins import state

    records: list[dict] = []
    for tool_name, tool in _plugin_tools.items():
        plugin_id = _tool_to_plugin.get(tool_name, "")
        if not plugin_id or not state.is_plugin_enabled(plugin_id):
            continue
        manifest = _loaded_manifests.get(plugin_id)
        tags = list(getattr(manifest, "tags", []) or []) if manifest else []
        plugin_name = str(getattr(manifest, "name", "") or plugin_id)
        manifest_description = str(getattr(manifest, "description", "") or "")
        try:
            lc_tools = list(tool.as_langchain_tools())
        except Exception as exc:
            logger.debug("Plugin tool catalog skipped %s: %s", tool_name, exc, exc_info=True)
            continue
        destructive = set(getattr(tool, "destructive_tool_names", set()) or set())
        for lc_tool in lc_tools:
            runtime_name = str(getattr(lc_tool, "name", "") or tool_name)
            label = str(getattr(tool, "display_name", "") or runtime_name)
            if runtime_name != tool_name:
                label = f"{label} / {runtime_name}"
            records.append({
                "runtime_name": runtime_name,
                "parent_name": tool_name,
                "plugin_id": plugin_id,
                "plugin_name": plugin_name,
                "tags": tags,
                "label": label,
                "description": str(
                    getattr(lc_tool, "description", "")
                    or getattr(tool, "description", "")
                    or manifest_description
                    or ""
                ),
                "destructive": runtime_name in destructive,
            })
    try:
        from row_bot.mcp_client import runtime as mcp_runtime

        for plugin_id, manifest in _loaded_manifests.items():
            if not state.is_plugin_enabled(plugin_id):
                continue
            if not getattr(getattr(manifest, "provides", None), "mcp_servers", []):
                continue
            records.extend(mcp_runtime.get_plugin_tool_records(plugin_id))
    except Exception as exc:
        logger.debug("Plugin MCP catalog records skipped: %s", exc, exc_info=True)
    return records


def get_loaded_manifests() -> list["PluginManifest"]:
    """Return manifests of all loaded plugins."""
    return list(_loaded_manifests.values())


def get_manifest(plugin_id: str) -> "PluginManifest | None":
    return _loaded_manifests.get(plugin_id)


def get_plugin_tools(plugin_id: str) -> list["PluginTool"]:
    """Return all tools belonging to a specific plugin."""
    return [
        tool for name, tool in _plugin_tools.items()
        if _tool_to_plugin.get(name) == plugin_id
    ]


def get_plugin_skills(plugin_id: str, *, enabled_only: bool = True) -> list[dict]:
    """Return skills belonging to a specific plugin.

    By default this mirrors runtime visibility: disabled plugin skills
    disappear from inventory as well as prompt injection.
    """
    if enabled_only:
        from row_bot.plugins import state

        if not state.is_plugin_enabled(plugin_id):
            return []
    return [
        info for name, info in _plugin_skills.items()
        if _skill_to_plugin.get(name) == plugin_id
    ]


def get_enabled_plugin_skill_records() -> list[dict]:
    """Return enabled plugin skills with their plugin and root metadata."""

    from row_bot.plugins import state

    records: list[dict] = []
    for storage_key, info in _plugin_skills.items():
        plugin_id = _skill_to_plugin.get(storage_key, "")
        if not plugin_id or not state.is_plugin_enabled(plugin_id):
            continue
        manifest = _loaded_manifests.get(plugin_id)
        records.append({
            **dict(info),
            "plugin_id": plugin_id,
            "plugin_name": str(getattr(manifest, "name", "") or plugin_id),
        })
    return records


# ── Unregister ───────────────────────────────────────────────────────────────
def unregister_plugin(plugin_id: str) -> None:
    """Remove all tools and skills for a plugin."""
    try:
        from row_bot.plugins.webhooks import unregister_plugin_webhooks

        unregister_plugin_webhooks(plugin_id)
    except Exception:
        logger.debug("Plugin webhook unregister skipped for %s", plugin_id, exc_info=True)

    tool_names = [n for n, pid in _tool_to_plugin.items() if pid == plugin_id]
    for name in tool_names:
        _plugin_tools.pop(name, None)
        _tool_to_plugin.pop(name, None)

    skill_names = [n for n, pid in _skill_to_plugin.items() if pid == plugin_id]
    for name in skill_names:
        _plugin_skills.pop(name, None)
        _skill_to_plugin.pop(name, None)

    _loaded_manifests.pop(plugin_id, None)


# ── Helpers ──────────────────────────────────────────────────────────────────
def _name_collides_with_builtin(tool_name: str) -> bool:
    """Check if a plugin tool name collides with a built-in tool."""
    try:
        from row_bot.tools import registry as core_registry
        return core_registry.get_tool(tool_name) is not None
    except ImportError:
        return False


# ── Reset (for testing) ─────────────────────────────────────────────────────
def _reset():
    """Clear all plugin registrations. For testing only."""
    _plugin_tools.clear()
    _plugin_skills.clear()
    _loaded_manifests.clear()
    _tool_to_plugin.clear()
    _skill_to_plugin.clear()
