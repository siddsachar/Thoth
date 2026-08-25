"""Tool-call grouping helpers for chat rendering.

The persisted message shape stays as a flat ``tool_results`` list.  This
module groups that list at render time so the transcript stays compact while
individual call results remain available.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any


AGENT_TOOL_NAMES = {
    "agents",
    "delegate_work",
    "agent_status",
    "agent_wait",
    "agent_stop",
    "agent_message",
    "agent_retry",
}

TOOL_TRACE_EXPANSION_CLASSES = "row-bot-tool-trace w-full"
TOOL_TRACE_ITEM_EXPANSION_CLASSES = "row-bot-tool-trace-item w-full"


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
        if is_browser_tool_name(self.name):
            base = "Browser activity"
            suffix = "step" if self.count == 1 else "steps"
            return f"{base} · {self.count} {suffix}"
        if is_computer_tool_name(self.name):
            base = "Computer activity"
            suffix = "step" if self.count == 1 else "steps"
            return f"{base} · {self.count} {suffix}"
        suffix = "call" if self.count == 1 else "calls"
        return f"{self.name} · {self.count} {suffix}"


def canonical_tool_name(name: Any) -> str:
    """Return a stable, readable tool display name."""

    clean = str(name or "tool").strip() or "tool"
    if clean.startswith("browser_"):
        return clean.replace("browser_", "Browser ").replace("_", " ").title()
    if clean == "computer_use":
        return "Computer activity"
    return clean


def is_browser_tool_name(name: Any) -> bool:
    clean = str(name or "").strip().lower()
    return clean.startswith("browser_") or clean.startswith("browser ")


def is_computer_tool_name(name: Any) -> bool:
    clean = str(name or "").strip().lower()
    return clean in {"computer_use", "computer activity"} or clean.startswith("computer ·")


def group_tool_results(tool_results: list[dict[str, Any]] | None) -> list[ToolResultGroup]:
    """Group tool results by tool name while preserving first-seen order."""

    grouped: "OrderedDict[str, ToolResultGroup]" = OrderedDict()
    for result in tool_results or []:
        name = canonical_tool_name(result.get("name", "tool") if isinstance(result, dict) else "tool")
        key = "Browser activity" if is_browser_tool_name(name) else "Computer activity" if is_computer_tool_name(name) else name
        if key not in grouped:
            grouped[key] = ToolResultGroup(name=key)
        grouped[key].results.append(result if isinstance(result, dict) else {"name": name, "content": str(result)})
    return list(grouped.values())


def parse_agent_tool_payload(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return parsed Agents tool JSON when a tool result contains Agent Runs."""

    if not isinstance(result, dict):
        return None
    name = str(result.get("name") or "").strip().lower()
    content = result.get("content")
    payload: Any = None
    if isinstance(content, dict):
        payload = content
    elif isinstance(content, str):
        text = content.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        try:
            payload = json.loads(text)
        except Exception:
            return None
    if not isinstance(payload, dict):
        return None
    if name not in AGENT_TOOL_NAMES and not (
        isinstance(payload.get("run"), dict) or isinstance(payload.get("runs"), list)
    ):
        return None
    if not isinstance(payload.get("run"), dict) and not isinstance(payload.get("runs"), list):
        return None
    return payload


def agent_runs_from_payload(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Extract public Agent Run dictionaries from an Agents tool payload."""

    if not isinstance(payload, dict):
        return []
    runs: list[dict[str, Any]] = []
    run = payload.get("run")
    if isinstance(run, dict) and run:
        runs.append(run)
    raw_runs = payload.get("runs")
    if isinstance(raw_runs, list):
        runs.extend(item for item in raw_runs if isinstance(item, dict) and item)
    return runs


def is_agent_tool_result(result: dict[str, Any] | None) -> bool:
    return parse_agent_tool_payload(result) is not None


def _parse_skill_load_payload(
    result: dict[str, Any] | None,
    *,
    newly_active: bool | None,
) -> dict[str, Any] | None:
    if not isinstance(result, dict) or str(result.get("name") or "") != "skill_load":
        return None
    content = result.get("content")
    if isinstance(content, dict):
        payload = content
    elif isinstance(content, str):
        try:
            payload = json.loads(content.strip())
        except Exception:
            return None
    else:
        return None
    if not isinstance(payload, dict) or payload.get("ok") is not True or payload.get("kind") != "skill_loaded":
        return None
    if not isinstance(payload.get("newly_active"), bool):
        return None
    if newly_active is not None and payload["newly_active"] is not newly_active:
        return None
    skill_id = payload.get("skill_id")
    display_name = payload.get("display_name")
    source = payload.get("source")
    evicted = payload.get("evicted_skill_id")
    if not isinstance(skill_id, str) or not skill_id.strip() or len(skill_id) > 180:
        return None
    if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 180:
        return None
    if not isinstance(source, str) or len(source) > 180:
        return None
    if evicted is not None and (not isinstance(evicted, str) or len(evicted) > 180):
        return None
    return {
        "skill_id": skill_id,
        "display_name": display_name,
        "source": source,
        "newly_active": payload["newly_active"],
        **({"evicted_skill_id": evicted} if evicted else {}),
    }


def parse_skill_load_result(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return bounded display metadata for one genuinely new skill activation."""

    return _parse_skill_load_payload(result, newly_active=True)


def is_skill_load_result(result: dict[str, Any] | None) -> bool:
    return parse_skill_load_result(result) is not None


def is_skill_load_noop_result(result: dict[str, Any] | None) -> bool:
    """Return True for a valid successful reload that should stay visually quiet."""

    return _parse_skill_load_payload(result, newly_active=False) is not None


def display_tool_content(content: Any, *, limit: int = 5_000) -> str:
    """Return UI-safe display text with render-time truncation."""

    if isinstance(content, dict) and content.get("display_summary"):
        text = str(content.get("display_summary") or "")
    elif isinstance(content, str):
        text = content
        stripped = text.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                payload = json.loads(stripped)
            except Exception:
                payload = None
            if isinstance(payload, dict) and payload.get("display_summary"):
                text = str(payload.get("display_summary") or "")
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
    payload: Any = content if isinstance(content, dict) else None
    if isinstance(content, str):
        stripped = content.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                payload = json.loads(stripped)
            except Exception:
                payload = None
    if isinstance(payload, dict):
        if payload.get("error") is True or payload.get("ok") is False:
            return True
        if str(payload.get("status") or "").strip().casefold() in {
            "error",
            "failed",
            "blocked",
            "cancelled",
        }:
            return True
    text = display_tool_content(content, limit=800).strip().lower()
    return text.startswith("tool error:") or text.startswith("error:") or "traceback (most recent call last)" in text


def tool_group_status(results: list[dict[str, Any]] | None) -> tuple[str, str]:
    """Return the truthful aggregate label/icon for a completed tool group."""

    if any(tool_result_failed(item) for item in results or []):
        return "Needs attention", "warning"
    return "Done", "check_circle"


def tool_group_completion_summary(results: list[dict[str, Any]] | None) -> str:
    """Return truthful settled counts without calling failed work complete."""

    settled = list(results or [])
    failed = sum(1 for item in settled if tool_result_failed(item))
    succeeded = len(settled) - failed
    if failed:
        return f"{succeeded} succeeded · {failed} failed"
    return f"{succeeded}/{len(settled)} complete"
