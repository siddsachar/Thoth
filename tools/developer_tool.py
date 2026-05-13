"""Developer Studio native tools."""

from __future__ import annotations

import json
import pathlib
import subprocess

from langchain_core.tools import StructuredTool
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from developer import change_ledger
from developer import edits as developer_edits
from developer.git import commit_changes, create_branch, fast_forward_merge, get_git_status, switch_branch
from developer.github import push_current_branch
from developer.sandbox import ApprovalDecision, decide_action
from developer.review import get_file_diff, list_changed_files
from developer.runtime import detect_project_commands, run_workspace_command, run_workspace_shell_command
from developer.sandbox_runtime import (
    apply_patch_in_docker_sandbox,
    get_pending_change,
    mark_pending_change_imported,
    write_file_in_docker_sandbox,
)
from developer.storage import get_workspace
from developer.tool_context import get_thread_id, get_workspace_id, infer_workspace_id_from_thread
from developer.todos import list_todos, replace_todos_from_labels, set_todo_status
from tools import registry
from tools.base import BaseTool


def _active_workspace():
    workspace_id = get_workspace_id()
    if not workspace_id:
        workspace_id = infer_workspace_id_from_thread(get_thread_id())
    if not workspace_id:
        raise ValueError("No active Developer workspace. Open a code thread in Developer Studio first.")
    workspace = get_workspace(workspace_id)
    if workspace is None:
        raise ValueError("The active Developer workspace could not be found.")
    root = pathlib.Path(workspace.path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Developer workspace folder does not exist: {root}")
    return workspace, root


def _inside(root: pathlib.Path, rel_path: str) -> pathlib.Path:
    clean = (rel_path or ".").strip().replace("\\", "/")
    if clean in {"", "."}:
        return root
    if clean.startswith("/") or clean.startswith("../") or "/../" in clean:
        raise ValueError(f"Path escapes workspace: {rel_path}")
    target = (root / clean).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Path escapes workspace: {rel_path}") from exc
    return target


def _workspace_rel(root: pathlib.Path, path: pathlib.Path) -> str:
    return path.resolve().relative_to(root).as_posix()


def _workspace_info() -> str:
    workspace, _root = _active_workspace()
    from developer.agent_context import build_developer_agent_context

    return build_developer_agent_context(workspace.id)


class _ListFilesInput(BaseModel):
    path: str = Field(default=".", description="Workspace-relative folder to list.")
    limit: int = Field(default=80, description="Maximum entries to return.")


def _list_files(path: str = ".", limit: int = 80) -> str:
    _workspace, root = _active_workspace()
    folder = _inside(root, path)
    if not folder.exists():
        return f"Path not found: {path}"
    if folder.is_file():
        return f"{_workspace_rel(root, folder)} ({folder.stat().st_size} bytes)"
    skip = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "dist", "build"}
    rows: list[str] = []
    for entry in sorted(folder.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        if entry.name in skip:
            continue
        suffix = "/" if entry.is_dir() else ""
        rows.append(f"- {_workspace_rel(root, entry)}{suffix}")
        if len(rows) >= max(1, min(limit, 300)):
            break
    return "\n".join(rows) if rows else "No files found."


class _ReadFileInput(BaseModel):
    path: str = Field(description="Workspace-relative text file path.")
    max_chars: int = Field(default=20000, description="Maximum characters to return.")


def _read_file(path: str, max_chars: int = 20000) -> str:
    _workspace, root = _active_workspace()
    target = _inside(root, path)
    if not target.is_file():
        return f"File not found: {path}"
    text = target.read_text(encoding="utf-8", errors="replace")
    limit = max(1000, min(max_chars, 100_000))
    if len(text) > limit:
        return text[:limit] + "\n...[file truncated]"
    return text


class _SearchInput(BaseModel):
    query: str = Field(description="Text or regex to search for inside the active workspace.")
    glob: str = Field(default="", description="Optional ripgrep glob such as '*.py'.")
    limit: int = Field(default=80, description="Maximum matching lines to return.")


def _search(query: str, glob: str = "", limit: int = 80) -> str:
    _workspace, root = _active_workspace()
    if not str(query or "").strip():
        return "Enter a non-empty search query."
    args = ["rg", "--line-number", "--no-heading", "--color=never", "--hidden"]
    for ignored in [".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", "dist", "build"]:
        args.extend(["--glob", f"!{ignored}/**"])
    if glob:
        args.extend(["--glob", glob])
    args.append(query)
    try:
        proc = subprocess.run(args, cwd=str(root), capture_output=True, text=True, timeout=30)
        output = proc.stdout.strip()
    except (FileNotFoundError, PermissionError):
        matches: list[str] = []
        lowered = query.lower()
        for file in root.rglob("*"):
            if not file.is_file():
                continue
            rel = _workspace_rel(root, file)
            if any(part in {".git", "node_modules", ".venv", "venv", "__pycache__"} for part in file.parts):
                continue
            try:
                for idx, line in enumerate(file.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if lowered in line.lower():
                        matches.append(f"{rel}:{idx}:{line}")
                        if len(matches) >= limit:
                            break
            except Exception:
                continue
            if len(matches) >= limit:
                break
        output = "\n".join(matches)
    if not output:
        return "No matches found."
    rows = output.splitlines()[: max(1, min(limit, 300))]
    return "\n".join(rows)


def _git_status() -> str:
    workspace, _root = _active_workspace()
    status = get_git_status(workspace.path)
    return json.dumps(status.__dict__, indent=2)


class _DiffInput(BaseModel):
    path: str = Field(default="", description="Optional workspace-relative file path. Leave empty for changed file summary.")


def _diff(path: str = "") -> str:
    workspace, _root = _active_workspace()
    if path:
        diff = get_file_diff(workspace.path, path)
        return diff or "No textual diff available."
    changed = list_changed_files(workspace.path)
    if not changed:
        return "No changed files detected."
    return "\n".join(f"- {item.status} {item.path}" for item in changed)


class _TodoInput(BaseModel):
    labels: list[str] = Field(default_factory=list, description="Todo labels to replace the current plan with.")
    todo_id: str = Field(default="", description="Existing todo id to update.")
    status: str = Field(default="", description="New status: pending, in_progress, completed, or blocked.")


def _update_todos(labels: list[str] | None = None, todo_id: str = "", status: str = "") -> str:
    thread_id = get_thread_id()
    if not thread_id:
        raise ValueError("No active Developer thread.")
    if labels:
        todos = replace_todos_from_labels(thread_id, labels)
    elif todo_id and status:
        todos = set_todo_status(thread_id, todo_id, status)  # type: ignore[arg-type]
    else:
        todos = list_todos(thread_id)
    return json.dumps([todo.__dict__ for todo in todos], indent=2)


class _RunDetectedInput(BaseModel):
    command: str = Field(description="Detected command label or exact detected command to run.")


def _run_detected(command: str) -> str:
    workspace, _root = _active_workspace()
    thread_id = get_thread_id()
    specs = detect_project_commands(workspace.path)
    chosen = next((spec for spec in specs if spec.label == command or spec.command == command), None)
    if chosen is None:
        return "Command is not in the detected Developer command list. Use the Inspector to add/approve custom commands later."
    result = run_workspace_command(
        workspace.path,
        chosen.command,
        workspace.approval_mode,
        workspace_id=workspace.id,
        thread_id=thread_id,
    )
    return json.dumps(result.__dict__, indent=2, default=str)


class _RunCommandInput(BaseModel):
    command: str = Field(description="Shell command to run in the active Developer workspace.")
    timeout: int = Field(default=120, description="Maximum seconds to allow the command to run.")


def _run_command(command: str, timeout: int = 120) -> str:
    workspace, _root = _active_workspace()
    thread_id = get_thread_id()
    result = run_workspace_shell_command(
        workspace.path,
        command,
        workspace.approval_mode,
        workspace_id=workspace.id,
        thread_id=thread_id,
        timeout=max(5, min(int(timeout or 120), 600)),
        confirmed=False,
    )
    if result.decision and result.decision.requires_approval:
        approval = interrupt({
            "tool": "developer_run_command",
            "label": "Run Developer shell command",
            "description": f"Run in {workspace.name}: {command}",
            "args": {"workspace": workspace.name, "command": command},
        })
        if not approval:
            return "Command cancelled by user."
        result = run_workspace_shell_command(
            workspace.path,
            command,
            workspace.approval_mode,
            workspace_id=workspace.id,
            thread_id=thread_id,
            timeout=max(5, min(int(timeout or 120), 600)),
            confirmed=True,
        )
    return json.dumps(result.__dict__, indent=2, default=str)


class _GitBranchInput(BaseModel):
    branch_name: str = Field(description="Git branch name.")


def _create_branch(branch_name: str) -> str:
    workspace, _root = _active_workspace()
    decision = decide_action(workspace.approval_mode, "git_branch")
    if not decision.allowed:
        approval = interrupt({
            "tool": "developer_create_branch",
            "label": "Create Developer branch",
            "description": f"Create branch {branch_name} in {workspace.name}",
            "args": {"workspace": workspace.name, "branch_name": branch_name},
        }) if decision.requires_approval else False
        if not approval:
            return decision.reason
    status = create_branch(workspace.path, branch_name)
    return json.dumps(status.__dict__, indent=2)


def _switch_branch(branch_name: str) -> str:
    workspace, _root = _active_workspace()
    decision = decide_action(workspace.approval_mode, "git_branch")
    if not decision.allowed:
        approval = interrupt({
            "tool": "developer_switch_branch",
            "label": "Switch Developer branch",
            "description": f"Switch to branch {branch_name} in {workspace.name}",
            "args": {"workspace": workspace.name, "branch_name": branch_name},
        }) if decision.requires_approval else False
        if not approval:
            return decision.reason
    status = switch_branch(workspace.path, branch_name)
    return json.dumps(status.__dict__, indent=2)


class _GitCommitInput(BaseModel):
    message: str = Field(description="Commit message.")
    paths: list[str] = Field(default_factory=list, description="Workspace-relative paths to commit. Leave empty to commit all staged/unstaged changes.")


def _commit_changes(message: str, paths: list[str] | None = None) -> str:
    workspace, _root = _active_workspace()
    decision = decide_action(workspace.approval_mode, "git_commit")
    if not decision.allowed:
        approval = interrupt({
            "tool": "developer_commit_changes",
            "label": "Create Developer commit",
            "description": f"Commit changes in {workspace.name}: {message}",
            "args": {"workspace": workspace.name, "message": message, "paths": paths or []},
        }) if decision.requires_approval else False
        if not approval:
            return decision.reason
        decision = ApprovalDecision("allow", "User explicitly approved this commit.")
    status = commit_changes(workspace.path, message, paths or None)
    return json.dumps({"decision": decision.__dict__, "status": status.__dict__}, indent=2)


def _push_current_branch() -> str:
    workspace, _root = _active_workspace()
    result = push_current_branch(workspace.path, workspace.approval_mode, confirmed=False)
    if result.decision and result.decision.requires_approval:
        approval = interrupt({
            "tool": "developer_push_current_branch",
            "label": "Push Developer branch",
            "description": f"Push the current branch from {workspace.name} to origin",
            "args": {"workspace": workspace.name},
        })
        if not approval:
            return "Push cancelled by user."
        result = push_current_branch(workspace.path, workspace.approval_mode, confirmed=True)
    return json.dumps(result.__dict__, indent=2, default=str)


class _GitFastForwardInput(BaseModel):
    branch_name: str = Field(description="Branch name to fast-forward merge into the current branch.")


def _fast_forward_merge(branch_name: str) -> str:
    workspace, _root = _active_workspace()
    decision = decide_action(workspace.approval_mode, "git_branch")
    if not decision.allowed:
        approval = interrupt({
            "tool": "developer_fast_forward_merge",
            "label": "Fast-forward merge",
            "description": f"Fast-forward merge {branch_name} into the current branch in {workspace.name}",
            "args": {"workspace": workspace.name, "branch_name": branch_name},
        }) if decision.requires_approval else False
        if not approval:
            return decision.reason
    status = fast_forward_merge(workspace.path, branch_name)
    return json.dumps(status.__dict__, indent=2)


class _ImportSandboxInput(BaseModel):
    pending_change_id: str = Field(description="Sandbox pending change id to import into the host workspace.")
    summary: str = Field(default="", description="Short summary for the imported sandbox patch.")


def _import_sandbox_changes(pending_change_id: str, summary: str = "") -> str:
    workspace, _root = _active_workspace()
    thread_id = get_thread_id()
    pending = get_pending_change(pending_change_id)
    if pending is None or pending.workspace_id != workspace.id:
        raise ValueError(f"Sandbox pending change not found: {pending_change_id}")
    if pending.imported:
        return f"Sandbox change {pending_change_id} was already imported."
    change_set, decision = developer_edits.apply_patch_to_workspace(
        workspace_id=workspace.id,
        thread_id=thread_id,
        patch=pending.patch,
        approval_mode=workspace.approval_mode,
        summary=summary or f"Import sandbox changes from: {pending.command[:80]}",
        confirmed=False,
    )
    if decision.requires_approval:
        approval = interrupt({
            "tool": "developer_import_sandbox_changes",
            "label": "Import sandbox changes",
            "description": summary or f"Import {len(pending.files)} file change(s) from Docker Sandbox",
            "args": {"workspace": workspace.name, "pending_change_id": pending_change_id, "files": pending.files},
        })
        if not approval:
            return "Sandbox import cancelled by user."
        change_set, decision = developer_edits.apply_patch_to_workspace(
            workspace_id=workspace.id,
            thread_id=thread_id,
            patch=pending.patch,
            approval_mode=workspace.approval_mode,
            summary=summary or f"Import sandbox changes from: {pending.command[:80]}",
            confirmed=True,
        )
    if change_set is None:
        return decision.reason
    mark_pending_change_imported(pending_change_id)
    return (
        f"Imported sandbox change {pending_change_id} as change set {change_set.id}.\n"
        + "\n".join(f"- {item.action} {item.path}" for item in change_set.files)
    )


class _PatchInput(BaseModel):
    patch: str = Field(description="Unified diff patch to apply inside the active workspace.")
    summary: str = Field(default="", description="Short summary of the intended change.")


def _preview_patch(patch: str, summary: str = "") -> str:
    workspace, _root = _active_workspace()
    return developer_edits.preview_patch(workspace.id, patch)


def _apply_patch(patch: str, summary: str = "") -> str:
    workspace, _root = _active_workspace()
    thread_id = get_thread_id()
    if workspace.execution_mode == "docker":
        decision = decide_action(workspace.approval_mode, "edit")
        if decision.decision == "block":
            return decision.reason
        if decision.requires_approval:
            approval = interrupt({
                "tool": "developer_apply_patch",
                "label": "Apply Developer patch in Docker Sandbox",
                "description": summary or "Apply a patch inside the Docker Sandbox shadow workspace",
                "args": {"workspace": workspace.name, "summary": summary},
            })
            if not approval:
                return "Sandbox patch application cancelled by user."
        outcome = apply_patch_in_docker_sandbox(
            workspace,
            patch,
            thread_id=thread_id,
            summary=summary,
        )
        if outcome.returncode != 0:
            return outcome.stderr or "Docker Sandbox patch failed."
        if not outcome.pending_change_id:
            return "Patch applied in Docker Sandbox, but it produced no file changes."
        return (
            f"Applied patch in Docker Sandbox as pending change {outcome.pending_change_id}.\n"
            "The real workspace was not changed. Import this sandbox change to apply it to the repo.\n"
            + "\n".join(f"- {path}" for path in outcome.changed_files)
        )
    change_set, decision = developer_edits.apply_patch_to_workspace(
        workspace_id=workspace.id,
        thread_id=thread_id,
        patch=patch,
        approval_mode=workspace.approval_mode,
        summary=summary,
        confirmed=False,
    )
    if decision.requires_approval:
        approval = interrupt({
            "tool": "developer_apply_patch",
            "label": "Apply Developer patch",
            "description": summary or "Apply a patch inside the active Developer workspace",
            "args": {"workspace": workspace.name, "summary": summary},
        })
        if not approval:
            return "Patch application cancelled by user."
        change_set, decision = developer_edits.apply_patch_to_workspace(
            workspace_id=workspace.id,
            thread_id=thread_id,
            patch=patch,
            approval_mode=workspace.approval_mode,
            summary=summary,
            confirmed=True,
        )
    if change_set is None:
        return decision.reason
    return (
        f"Applied patch as change set {change_set.id}.\n"
        + "\n".join(f"- {item.action} {item.path}" for item in change_set.files)
    )


class _WriteFileInput(BaseModel):
    path: str = Field(description="Workspace-relative file path to create or replace.")
    content: str = Field(description="Complete UTF-8 text content for the file.")
    summary: str = Field(default="", description="Short summary of the intended file change.")


def _write_file(path: str, content: str, summary: str = "") -> str:
    workspace, _root = _active_workspace()
    thread_id = get_thread_id()
    if workspace.execution_mode == "docker":
        decision = decide_action(workspace.approval_mode, "edit")
        if decision.decision == "block":
            return decision.reason
        if decision.requires_approval:
            approval = interrupt({
                "tool": "developer_write_file",
                "label": "Write Developer file in Docker Sandbox",
                "description": summary or f"Write {path} inside the Docker Sandbox shadow workspace",
                "args": {"workspace": workspace.name, "path": path, "summary": summary},
            })
            if not approval:
                return "Sandbox file write cancelled by user."
        outcome = write_file_in_docker_sandbox(
            workspace,
            path,
            content,
            thread_id=thread_id,
        )
        if outcome.returncode != 0:
            return outcome.stderr or "Docker Sandbox file write failed."
        if not outcome.pending_change_id:
            return f"Wrote {path} in Docker Sandbox, but it produced no file changes."
        return (
            f"Wrote {path} in Docker Sandbox as pending change {outcome.pending_change_id}.\n"
            "The real workspace was not changed. Import this sandbox change to apply it to the repo."
        )
    change_set, decision = developer_edits.write_file_to_workspace(
        workspace_id=workspace.id,
        thread_id=thread_id,
        path=path,
        content=content,
        approval_mode=workspace.approval_mode,
        summary=summary,
        confirmed=False,
    )
    if decision.requires_approval:
        approval = interrupt({
            "tool": "developer_write_file",
            "label": "Write Developer file",
            "description": summary or f"Write {path} inside the active Developer workspace",
            "args": {"workspace": workspace.name, "path": path, "summary": summary},
        })
        if not approval:
            return "File write cancelled by user."
        change_set, decision = developer_edits.write_file_to_workspace(
            workspace_id=workspace.id,
            thread_id=thread_id,
            path=path,
            content=content,
            approval_mode=workspace.approval_mode,
            summary=summary,
            confirmed=True,
        )
    if change_set is None:
        return decision.reason
    return f"Wrote {path} as change set {change_set.id}."


class _RevertInput(BaseModel):
    change_set_id: str = Field(description="Agent change set id to revert.")


def _revert_change_set(change_set_id: str) -> str:
    workspace, _root = _active_workspace()
    return developer_edits.revert_change_set(workspace.id, change_set_id)


def _list_agent_changes() -> str:
    workspace, _root = _active_workspace()
    rows = change_ledger.list_change_sets(workspace_id=workspace.id, thread_id=get_thread_id())
    if not rows:
        return "No agent-owned change sets recorded for this Developer thread."
    return json.dumps([
        {
            "id": item.id,
            "summary": item.summary,
            "files": [file.__dict__ for file in item.files],
            "created_at": item.created_at,
        }
        for item in rows[:20]
    ], indent=2)


class DeveloperTool(BaseTool):
    @property
    def name(self) -> str:
        return "developer"

    @property
    def display_name(self) -> str:
        return "Developer"

    @property
    def description(self) -> str:
        return "Developer Studio workspace tools for reading, searching, editing, testing, and reviewing code."

    @property
    def enabled_by_default(self) -> bool:
        return False

    def execute(self, query: str) -> str:
        return _workspace_info()

    def as_langchain_tools(self) -> list:
        return [
            StructuredTool.from_function(func=_workspace_info, name="developer_workspace_info", description="Return the active Developer workspace, path, branch, dirty state, remote, and approval mode."),
            StructuredTool.from_function(func=_list_files, name="developer_list_files", description="List files under the active Developer workspace. Paths must be workspace-relative.", args_schema=_ListFilesInput),
            StructuredTool.from_function(func=_read_file, name="developer_read_file", description="Read a workspace-relative text file from the active Developer workspace.", args_schema=_ReadFileInput),
            StructuredTool.from_function(func=_search, name="developer_search", description="Search text in the active Developer workspace using a safe workspace-scoped search.", args_schema=_SearchInput),
            StructuredTool.from_function(func=_git_status, name="developer_git_status", description="Return structured Git state for the active Developer workspace."),
            StructuredTool.from_function(func=_create_branch, name="developer_create_branch", description="Create a Git branch in the active Developer workspace using Developer approval policy.", args_schema=_GitBranchInput),
            StructuredTool.from_function(func=_switch_branch, name="developer_switch_branch", description="Switch Git branches in the active Developer workspace using Developer approval policy.", args_schema=_GitBranchInput),
            StructuredTool.from_function(func=_commit_changes, name="developer_commit_changes", description="Create a Git commit in the active Developer workspace using Developer approval policy.", args_schema=_GitCommitInput),
            StructuredTool.from_function(func=_push_current_branch, name="developer_push_current_branch", description="Push the current branch to origin using Developer approval policy."),
            StructuredTool.from_function(func=_fast_forward_merge, name="developer_fast_forward_merge", description="Fast-forward merge another branch into the current branch using Developer approval policy.", args_schema=_GitFastForwardInput),
            StructuredTool.from_function(func=_diff, name="developer_get_diff", description="Return changed file summary or one file diff for the active Developer workspace.", args_schema=_DiffInput),
            StructuredTool.from_function(func=_update_todos, name="developer_update_todos", description="Create or update the visible Developer todo plan for this code thread.", args_schema=_TodoInput),
            StructuredTool.from_function(func=_run_detected, name="developer_run_detected_test", description="Run a command from the detected Developer test/lint/typecheck command list.", args_schema=_RunDetectedInput),
            StructuredTool.from_function(func=_run_command, name="developer_run_command", description="Run a shell command in the active Developer workspace after policy checks and record file side effects.", args_schema=_RunCommandInput),
            StructuredTool.from_function(func=_import_sandbox_changes, name="developer_import_sandbox_changes", description="Import a Docker Sandbox pending patch into the real workspace after approval.", args_schema=_ImportSandboxInput),
            StructuredTool.from_function(func=_preview_patch, name="developer_preview_patch", description="Validate and preview a unified diff patch without writing files.", args_schema=_PatchInput),
            StructuredTool.from_function(func=_apply_patch, name="developer_apply_patch", description="Apply a validated unified diff patch inside the active Developer workspace and record an agent-owned change set.", args_schema=_PatchInput),
            StructuredTool.from_function(func=_write_file, name="developer_write_file", description="Create or replace a workspace-relative text file and record an agent-owned change set.", args_schema=_WriteFileInput),
            StructuredTool.from_function(func=_list_agent_changes, name="developer_list_agent_changes", description="List agent-owned change sets recorded for this Developer thread."),
            StructuredTool.from_function(func=_revert_change_set, name="developer_revert_agent_changes", description="Revert an agent-owned change set if files have not drifted.", args_schema=_RevertInput),
        ]


registry.register(DeveloperTool())
