from __future__ import annotations

DEVELOPER_AUTO_SKILLS = [
    "developer_coding",
    "developer_review",
    "developer_pr_prep",
    "developer_custom_tools",
]

DEVELOPER_CONFLICTING_TOOLS = {
    "filesystem",
    "shell",
}


def effective_tool_names(enabled_tool_names: list[str]) -> list[str]:
    """Return the lean tool set for a Developer Studio thread."""
    result: list[str] = []
    seen: set[str] = set()
    for name in enabled_tool_names:
        if name in DEVELOPER_CONFLICTING_TOOLS or name in seen:
            continue
        result.append(name)
        seen.add(name)
    if "developer" not in seen:
        result.append("developer")
    return result
