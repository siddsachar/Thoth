"""Tool-call grouping helpers for chat rendering.

The persisted message shape stays as a flat ``tool_results`` list.  This
module groups that list at render time so the transcript stays compact while
individual call results remain available.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResultGroup:
    """Display group for one tool name."""

    name: str
    results: list[dict[str, Any]] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.results)

    @property
    def label(self) -> str:
        prefix = "error" if any(tool_result_failed(item) for item in self.results) else ""
        if is_browser_tool_name(self.name):
            base = "Browser activity"
            suffix = "step" if self.count == 1 else "steps"
            label = f"{base} · {self.count} {suffix}"
            return f"{label} · {prefix}" if prefix else label
        suffix = "call" if self.count == 1 else "calls"
        label = f"{self.name} · {self.count} {suffix}"
        return f"{label} · {prefix}" if prefix else label


def canonical_tool_name(name: Any) -> str:
    """Return a stable, readable tool display name."""

    clean = str(name or "tool").strip() or "tool"
    if clean.startswith("browser_"):
        return clean.replace("browser_", "Browser ").replace("_", " ").title()
    return clean


def is_browser_tool_name(name: Any) -> bool:
    clean = str(name or "").strip().lower()
    return clean.startswith("browser_") or clean.startswith("browser ")


def group_tool_results(tool_results: list[dict[str, Any]] | None) -> list[ToolResultGroup]:
    """Group tool results by tool name while preserving first-seen order."""

    grouped: "OrderedDict[str, ToolResultGroup]" = OrderedDict()
    for result in tool_results or []:
        name = canonical_tool_name(result.get("name", "tool") if isinstance(result, dict) else "tool")
        key = "Browser activity" if is_browser_tool_name(name) else name
        if key not in grouped:
            grouped[key] = ToolResultGroup(name=key)
        grouped[key].results.append(result if isinstance(result, dict) else {"name": name, "content": str(result)})
    return list(grouped.values())


def display_tool_content(content: Any, *, limit: int = 5_000) -> str:
    """Return UI-safe display text with render-time truncation."""

    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    else:
        text = str(content) if content is not None else ""
    if len(text) > limit:
        return text[:limit] + "\n\n… (truncated)"
    return text


def tool_result_failed(result_or_content: Any) -> bool:
    """Return True when a tool result should be rendered as failed."""

    if isinstance(result_or_content, dict):
        if result_or_content.get("error") is True:
            return True
        content = result_or_content.get("content", "")
    else:
        content = result_or_content
    text = display_tool_content(content, limit=800).strip().lower()
    return text.startswith("tool error:") or text.startswith("error:") or "traceback (most recent call last)" in text
