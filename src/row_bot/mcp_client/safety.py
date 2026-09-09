"""Safety classification and naming helpers for MCP tools."""

from __future__ import annotations

import re
from typing import Any

_DESTRUCTIVE_RE = re.compile(
    r"(^|_)(delete|remove|destroy|drop|write|update|edit|create|send|post|put|patch|"
    r"move|rename|run|exec|execute|shell|command|deploy|publish|merge|commit|push|"
    r"payment|pay|transfer|withdraw|trade|buy|sell|order|book|cancel)(_|$)",
    re.IGNORECASE,
)

_BROWSER_SESSION_SAFE_TOOLS = {
    "browser_click",
    "browser_close",
    "browser_console_messages",
    "browser_drag",
    "browser_fill_form",
    "browser_hover",
    "browser_navigate",
    "browser_navigate_back",
    "browser_network_requests",
    "browser_press_key",
    "browser_resize",
    "browser_select_option",
    "browser_snapshot",
    "browser_take_screenshot",
    "browser_wait_for",
}


def sanitize_name_component(value: str) -> str:
    """Return a function-call safe identifier component."""
    text = re.sub(r"[^a-zA-Z0-9_]+", "_", str(value or "").strip())
    text = re.sub(r"_+", "_", text).strip("_").lower()
    return text or "unnamed"


def prefixed_tool_name(server_name: str, tool_name: str) -> str:
    return f"mcp_{sanitize_name_component(server_name)}_{sanitize_name_component(tool_name)}"


def _annotation_value(tool: Any, key: str) -> Any:
    annotations = tool.get("annotations") if isinstance(tool, dict) else getattr(tool, "annotations", None)
    if annotations is None:
        return None
    if isinstance(annotations, dict):
        return annotations.get(key)
    return getattr(annotations, key, None)


def is_destructive_tool(tool_name: str, description: str = "", tool_obj: Any = None) -> bool:
    """Classify whether an MCP tool should require approval by default."""
    normalized_name = sanitize_name_component(tool_name)
    if tool_obj is not None:
        destructive_hint = _annotation_value(tool_obj, "destructiveHint")
        read_only_hint = _annotation_value(tool_obj, "readOnlyHint")
        if destructive_hint is True:
            return True
        if _DESTRUCTIVE_RE.search(normalized_name):
            return True
        if read_only_hint is True:
            return False
    if normalized_name in _BROWSER_SESSION_SAFE_TOOLS:
        return False
    haystack = f"{tool_name} {description or ''}"
    normalized = sanitize_name_component(haystack)
    return bool(_DESTRUCTIVE_RE.search(normalized))


def tool_enabled_by_default(is_destructive: bool) -> bool:
    """Default selection rule after a successful server test/discovery."""
    return not is_destructive
