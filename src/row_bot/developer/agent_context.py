from __future__ import annotations

import pathlib
import os

from row_bot.developer.git import get_git_status
from row_bot.developer.storage import get_workspace


_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    "dist",
    "build",
}

def _top_level_inventory(path: pathlib.Path, *, limit: int = 40) -> list[str]:
    rows: list[str] = []
    try:
        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except Exception:
        return rows
    for entry in entries:
        if entry.name in _SKIP_DIRS:
            continue
        suffix = "/" if entry.is_dir() else ""
        rows.append(f"- {entry.name}{suffix}")
        if len(rows) >= limit:
            break
    return rows


def _thread_approval_mode(thread_id: str, fallback: str) -> str:
    if thread_id:
        try:
            from row_bot.threads import _get_thread_approval_mode

            return _get_thread_approval_mode(thread_id)
        except Exception:
            pass
    try:
        from row_bot.approval_policy import normalize_approval_mode

        return normalize_approval_mode(fallback)
    except Exception:
        return fallback or "approve"


def build_developer_agent_context(workspace_id: str, thread_id: str = "") -> str:
    """Build a compact context prefix for Developer chat turns.

    This intentionally avoids reading arbitrary file contents.  The agent
    gets workspace identity, approval mode, Git state, and a small top-level
    inventory, then can ask for or use explicit file tools in later phases.
    """
    workspace = get_workspace(workspace_id)
    if workspace is None:
        return ""
    path = pathlib.Path(workspace.path)
    status = get_git_status(workspace.path)
    inventory = _top_level_inventory(path)
    shell_name = "PowerShell" if os.name == "nt" else "POSIX sh"
    approval_mode = _thread_approval_mode(thread_id, workspace.approval_mode)

    lines = [
        "[Developer Studio context]",
        f"Workspace: {workspace.name}",
        f"Path: {workspace.path}",
        f"Approval mode: {approval_mode}",
        f"Execution mode: {workspace.execution_mode}",
        f"Sandbox network: {workspace.sandbox_network}",
        f"Command shell: {shell_name}",
        "Workspace/path/branch facts above are authoritative for identity questions. "
        "If the user asks what repo, workspace, path, or branch is active, answer directly from this context; "
        "do not call shell, git, filesystem, or browser tools.",
        "For code work, prefer Developer-native tools (developer_read_file, developer_search, "
        "developer_apply_patch, developer_get_diff, developer_update_todos, developer_run_detected_test, "
        "developer_create_branch, developer_switch_branch, developer_commit_changes, developer_push_current_branch) "
        "over generic shell or filesystem tools.",
        "When shell is necessary, write commands for the command shell above. On PowerShell, do not use POSIX heredocs like `python - <<'PY'`.",
        "Prefer small targeted edits and preserve unrelated formatting. Avoid whole-file rewrites unless the file format or change requires it.",
        "For structured files, run a cheap parse/validation check when available. For notebooks, JSON parse is the minimum; use nbformat validation if available, but do not execute the whole notebook unless asked or clearly safe.",
        "Execution modes: Local runs commands in the selected repo folder. Docker Sandbox runs commands in an isolated shadow copy; "
        "real repo files change only after developer_import_sandbox_changes imports an approved sandbox patch.",
        "Do not clone repositories into Row-Bot app data. If cloning is needed, ask for an explicit destination.",
        "Do not install dependencies, delete files, commit, push, or use external network without the configured approval policy.",
    ]
    if status.is_git:
        lines.append(f"Git branch: {status.branch or '(detached/unknown)'}")
        lines.append(f"Git dirty: {'yes' if status.dirty else 'no'}")
        if status.remote:
            lines.append(f"Git remote: {status.remote}")
    elif status.error:
        lines.append(f"Git status error: {status.error}")
    else:
        lines.append("Git: not detected")
    if inventory:
        lines.append("Top-level files:")
        lines.extend(inventory)
    lines.append("[/Developer Studio context]")
    return "\n".join(lines)
