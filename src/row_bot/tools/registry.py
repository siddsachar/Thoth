"""Tool registry — discovers, stores, and manages all retrieval tools.

Usage
-----
    from row_bot.tools import registry

    for tool in registry.get_enabled_tools():
        results = tool.get_retriever().invoke(query)
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import tempfile
from typing import TYPE_CHECKING

from row_bot.data_paths import get_row_bot_data_dir

if TYPE_CHECKING:
    from row_bot.tools.base import BaseTool

logger = logging.getLogger(__name__)

# Persist enabled / disabled state alongside other Row-Bot data.  The path is
# resolved dynamically because tests and channel runtimes may isolate
# ROW_BOT_DATA_DIR after module import.
def _data_dir() -> pathlib.Path:
    data_dir = get_row_bot_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def _config_path() -> pathlib.Path:
    return _data_dir() / "tools_config.json"


DATA_DIR = _data_dir()
_CONFIG_PATH = _config_path()
_active_config_path = _CONFIG_PATH

# ── Internal storage ─────────────────────────────────────────────────────────────
_tools: dict[str, "BaseTool"] = {}          # name → tool instance
_enabled: dict[str, bool] = {}              # name → enabled flag (runtime cache)
_tool_configs: dict[str, dict] = {}         # name → {key: value} (tool-specific config)


# ── Config persistence ───────────────────────────────────────────────────────
def _read_config(path: pathlib.Path) -> dict:
    """Load the persisted config from disk."""
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            logger.warning("Failed to load tools config from %s", path, exc_info=True)
            return {}
    return {}


def _load_config() -> dict:
    return _read_config(_config_path())


def _write_config_atomic(path: pathlib.Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd: int | None = None
    tmp_path: pathlib.Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
        tmp_path = pathlib.Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            json.dump(payload, handle, indent=2, ensure_ascii=False)
        tmp_path.replace(path)
    finally:
        if fd is not None:
            os.close(fd)
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                logger.debug("Failed to remove temp tools config %s", tmp_path, exc_info=True)


def _apply_saved_config(tool: "BaseTool", saved: dict) -> None:
    # Support new format {"tools": {...}} and old flat format
    tools_map = saved.get("tools", saved) if isinstance(saved.get("tools"), dict) else saved
    if tool.name in tools_map:
        _enabled[tool.name] = bool(tools_map[tool.name])
    else:
        _enabled[tool.name] = tool.enabled_by_default

    # Restore persisted tool-specific config
    saved_configs = saved.get("tool_configs", {})
    if tool.name in saved_configs and isinstance(saved_configs.get(tool.name), dict):
        _tool_configs[tool.name] = dict(saved_configs[tool.name])
        # Auto-migrate: if the schema adds new default operations that didn't
        # exist before, merge them in so existing users get new sub-tools.
        # We track which options were known at last save via _known_options.
        for key, spec in tool.config_schema.items():
            if spec.get("type") != "multicheck":
                continue
            known_key = f"_{key}_known"
            current_options = set(spec.get("options", []))
            previously_known = set(_tool_configs[tool.name].get(known_key, []))
            if previously_known:
                new_ops = current_options - previously_known
                default_ops = set(spec.get("default", []))
                to_add = new_ops & default_ops  # only auto-enable if in defaults
                if to_add and key in _tool_configs[tool.name]:
                    saved_ops = set(_tool_configs[tool.name][key])
                    _tool_configs[tool.name][key] = list(saved_ops | to_add)
                    logger.info("Auto-enabled new operations for %s: %s", tool.name, to_add)
            # Always update the known set to current options
            _tool_configs[tool.name][known_key] = list(current_options)
    else:
        # Initialise from schema defaults
        _tool_configs.setdefault(tool.name, {})
        for key, spec in tool.config_schema.items():
            if key not in _tool_configs[tool.name]:
                _tool_configs[tool.name][key] = spec.get("default")


def _ensure_config_scope() -> None:
    """Reload tool settings if ROW_BOT_DATA_DIR changed after module import."""
    global DATA_DIR, _CONFIG_PATH, _active_config_path
    current = _config_path()
    if current == _active_config_path:
        return
    logger.info("Tool config scope changed: %s -> %s", _active_config_path, current)
    DATA_DIR = current.parent
    _CONFIG_PATH = current
    _active_config_path = current
    _enabled.clear()
    _tool_configs.clear()
    saved = _read_config(current)
    for tool in _tools.values():
        _apply_saved_config(tool, saved)


def _save_config():
    """Persist the current enabled/disabled map and tool configs to disk."""
    _ensure_config_scope()
    _write_config_atomic(_config_path(), {"tools": _enabled, "tool_configs": _tool_configs})


# ── Public API ───────────────────────────────────────────────────────────────
def register(tool: "BaseTool") -> None:
    """Register a tool instance.  Called by each tool module at import time."""
    logger.debug("Registering tool: %s", tool.name)
    _ensure_config_scope()
    _tools[tool.name] = tool
    # If the user already toggled this tool, honour that; otherwise use default
    saved = _load_config()
    _apply_saved_config(tool, saved)


def get_all_tools() -> list["BaseTool"]:
    """Return all registered tools (enabled + disabled), sorted by name."""
    _ensure_config_scope()
    return [_tools[n] for n in sorted(_tools)]


def get_enabled_tools() -> list["BaseTool"]:
    """Return only the tools the user has enabled."""
    return [t for t in get_all_tools() if is_enabled(t.name)]


def is_enabled(name: str) -> bool:
    _ensure_config_scope()
    return _enabled.get(name, False)


def set_enabled(name: str, value: bool) -> None:
    tool = get_tool(name)
    if tool is None:
        raise KeyError(f"Unknown tool '{name}'")
    logger.info("Tool '%s' %s", tool.name, "enabled" if value else "disabled")
    _enabled[tool.name] = value
    _save_config()
    _invalidate_agent_cache()
    # Also invalidate the task tool-inference keyword map
    try:
        from row_bot.tasks import invalidate_keyword_map_cache
        invalidate_keyword_map_cache()
    except ImportError:
        pass


def get_tool(name: str) -> "BaseTool | None":
    return _tools.get(name)


def get_all_required_api_keys() -> dict[str, str]:
    """Aggregate ``required_api_keys`` from *all* registered tools.
    Returns ``{UI label: ENV_VAR_NAME}``.
    """
    keys: dict[str, str] = {}
    for tool in get_all_tools():
        keys.update(tool.required_api_keys)
    return keys


def get_tool_config(tool_name: str, key: str, default=None):
    """Read a persisted config value for a tool."""
    _ensure_config_scope()
    return _tool_configs.get(tool_name, {}).get(key, default)


def set_tool_config(tool_name: str, key: str, value):
    """Write a config value for a tool and persist."""
    _ensure_config_scope()
    logger.info("Tool config updated: %s.%s", tool_name, key)
    _tool_configs.setdefault(tool_name, {})[key] = value
    _save_config()
    _invalidate_agent_cache()


def _invalidate_agent_cache():
    """Clear cached agent graphs when tool settings change."""
    try:
        from row_bot.agent import clear_agent_cache
        clear_agent_cache()
    except ImportError:
        pass


# ── Global (non-tool-specific) config ────────────────────────────────────────
_global_config: dict = {}

def _load_global_config():
    """Bootstrap global config from the persisted file."""
    global _global_config
    _ensure_config_scope()
    saved = _load_config()
    _global_config = saved.get("global", {})

def get_global_config(key: str, default=None):
    """Read a global (non-tool-specific) config value."""
    if not _global_config:
        _load_global_config()
    return _global_config.get(key, default)

def set_global_config(key: str, value) -> None:
    """Write a global config value and persist."""
    _ensure_config_scope()
    _global_config[key] = value
    # Merge into the config file alongside tools and tool_configs
    saved = _load_config()
    saved["global"] = _global_config
    _write_config_atomic(_config_path(), saved)
    _invalidate_agent_cache()


def get_external_tool_loading_mode() -> str:
    """Return the normalized external-tool binding mode."""

    value = str(get_global_config("external_tool_loading_mode", "auto") or "auto").strip().lower()
    return value if value in {"auto", "eager"} else "auto"


def get_langchain_tools() -> list:
    """Return LangChain-compatible tool wrappers for all enabled tools.
    Uses ``as_langchain_tools()`` (plural) so tools contributing multiple
    operations are handled correctly."""
    tools = []
    for t in get_enabled_tools():
        tools.extend(t.as_langchain_tools())
    return tools
