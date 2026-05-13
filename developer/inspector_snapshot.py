from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, replace
from typing import Any

from nicegui import background_tasks, run

from developer.change_ledger import ChangeSet
from developer.devcontainer import DevcontainerInfo
from developer.review import ChangedFile, DiffStats
from developer.runtime import CommandSpec
from developer.sandbox_runtime import SandboxPendingChange, SandboxProbe, SandboxStatus
from developer.state import DeveloperTodo, DeveloperWorkspace

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InspectorSnapshot:
    workspace_id: str
    thread_id: str | None
    version: int
    created_at: float
    workspace: DeveloperWorkspace
    git_summary: dict[str, Any]
    todos: list[DeveloperTodo]
    changed_files: list[ChangedFile]
    diff_stats: DiffStats | None
    agent_changes: list[ChangeSet]
    command_specs: list[CommandSpec]
    devcontainer: DevcontainerInfo
    sandbox_probe: SandboxProbe
    sandbox_status: SandboxStatus | None
    sandbox_pending_changes: list[SandboxPendingChange]
    error: str = ""
    fingerprint: str = ""


@dataclass
class _RefreshState:
    task: asyncio.Task | None = None
    pending: bool = False
    last_requested_at: float = 0.0


_snapshots: dict[tuple[str, str | None], InspectorSnapshot] = {}
_states: dict[tuple[str, str | None], _RefreshState] = {}
_version = 0


def get_snapshot(workspace_id: str, thread_id: str | None) -> InspectorSnapshot | None:
    return _snapshots.get((workspace_id, thread_id))


def request_snapshot_refresh(
    workspace_id: str,
    thread_id: str | None,
    *,
    reason: str = "manual",
    debounce: float = 0.35,
) -> None:
    """Refresh Developer Inspector data in the background.

    This module intentionally has no UI imports. Callers can request a refresh
    freely from streaming/tool events; the active UI only observes snapshot
    version changes and never performs git/file scans directly.
    """

    if not workspace_id:
        return
    key = (workspace_id, thread_id)
    state = _states.setdefault(key, _RefreshState())
    state.last_requested_at = time.time()
    if state.task and not state.task.done():
        state.pending = True
        return

    async def _runner() -> None:
        while True:
            state.pending = False
            await asyncio.sleep(max(0.0, debounce))
            started = time.perf_counter()
            snapshot = await run.io_bound(_collect_snapshot_sync, workspace_id, thread_id)
            _store_snapshot(snapshot)
            elapsed = time.perf_counter() - started
            if elapsed > 0.75:
                logger.info(
                    "perf: developer inspector snapshot refreshed in %.3fs workspace=%s reason=%s",
                    elapsed,
                    workspace_id,
                    reason,
                )
            if not state.pending:
                break

    try:
        state.task = background_tasks.create(
            _runner(),
            name=f"developer inspector snapshot {workspace_id}",
        )
    except AssertionError:
        # Tests or early startup paths may call this before NiceGUI owns a loop.
        _store_snapshot(_collect_snapshot_sync(workspace_id, thread_id))


def update_snapshot_approval_mode(
    workspace_id: str,
    thread_id: str | None,
    approval_mode: str,
) -> None:
    """Patch the cached snapshot immediately after a local approval-mode change."""

    key = (workspace_id, thread_id)
    snapshot = _snapshots.get(key)
    if snapshot is None:
        return
    workspace = replace(snapshot.workspace, approval_mode=approval_mode)  # type: ignore[arg-type]
    _store_snapshot(replace(snapshot, workspace=workspace, created_at=time.time()))


def refresh_snapshot_for_tests(workspace_id: str, thread_id: str | None) -> InspectorSnapshot:
    snapshot = _collect_snapshot_sync(workspace_id, thread_id)
    _store_snapshot(snapshot)
    stored = get_snapshot(workspace_id, thread_id)
    assert stored is not None
    return stored


def _store_snapshot(snapshot: InspectorSnapshot) -> None:
    global _version
    snapshot = replace(snapshot, fingerprint=snapshot.fingerprint or _fingerprint_snapshot(snapshot))
    previous = _snapshots.get((snapshot.workspace_id, snapshot.thread_id))
    if previous is not None and previous.fingerprint == snapshot.fingerprint:
        return
    _version += 1
    _snapshots[(snapshot.workspace_id, snapshot.thread_id)] = replace(snapshot, version=_version)


def _fingerprint_snapshot(snapshot: InspectorSnapshot) -> str:
    payload = {
        "workspace": {
            "id": snapshot.workspace.id,
            "name": snapshot.workspace.name,
            "path": snapshot.workspace.path,
            "repo_url": snapshot.workspace.repo_url,
            "approval_mode": snapshot.workspace.approval_mode,
            "execution_mode": snapshot.workspace.execution_mode,
            "sandbox_network": snapshot.workspace.sandbox_network,
            "sandbox_image": snapshot.workspace.sandbox_image,
            "sandbox_env_allowlist": snapshot.workspace.sandbox_env_allowlist,
            "trusted": snapshot.workspace.trusted,
            "hidden": snapshot.workspace.hidden,
        },
        "git": snapshot.git_summary,
        "todos": [
            {
                "id": todo.id,
                "label": todo.label,
                "status": todo.status,
                "detail": todo.detail,
                "reference": todo.reference,
            }
            for todo in snapshot.todos
        ],
        "changed": [
            {
                "path": item.path,
                "status": item.status,
                "additions": item.additions,
                "deletions": item.deletions,
            }
            for item in snapshot.changed_files
        ],
        "diff_stats": {
            "files": snapshot.diff_stats.files,
            "additions": snapshot.diff_stats.additions,
            "deletions": snapshot.diff_stats.deletions,
        }
        if snapshot.diff_stats
        else None,
        "agent_changes": [
            {
                "id": change.id,
                "summary": change.summary,
                "reverted": change.reverted,
                "reviewed": change.reviewed,
                "files": [(file.path, file.action, file.before_hash, file.after_hash) for file in change.files],
            }
            for change in snapshot.agent_changes
        ],
        "commands": [(spec.label, spec.command, spec.kind) for spec in snapshot.command_specs],
        "devcontainer": {
            "present": snapshot.devcontainer.present,
            "path": snapshot.devcontainer.path,
            "name": snapshot.devcontainer.name,
            "image": snapshot.devcontainer.image,
            "dockerfile": snapshot.devcontainer.dockerfile,
            "message": snapshot.devcontainer.message,
        },
        "sandbox_probe": {
            "available": snapshot.sandbox_probe.available,
            "binary": snapshot.sandbox_probe.binary,
            "version": snapshot.sandbox_probe.version,
            "message": snapshot.sandbox_probe.message,
        },
        "sandbox_status": {
            "available": snapshot.sandbox_status.available,
            "container_name": snapshot.sandbox_status.container_name,
            "running": snapshot.sandbox_status.running,
            "exists": snapshot.sandbox_status.exists,
            "image": snapshot.sandbox_status.image,
            "network": snapshot.sandbox_status.network,
            "shadow_workspace": snapshot.sandbox_status.shadow_workspace,
            "message": snapshot.sandbox_status.message,
            "processes": [(p.pid, p.command, p.log_path) for p in snapshot.sandbox_status.processes],
        }
        if snapshot.sandbox_status
        else None,
        "sandbox_pending": [
            {
                "id": item.id,
                "workspace_id": item.workspace_id,
                "thread_id": item.thread_id,
                "command": item.command,
                "files": item.files,
                "created_at": item.created_at,
                "imported": item.imported,
            }
            for item in snapshot.sandbox_pending_changes
        ],
        "error": snapshot.error,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()


def _collect_snapshot_sync(workspace_id: str, thread_id: str | None) -> InspectorSnapshot:
    from developer.change_ledger import list_change_sets
    from developer.devcontainer import detect_devcontainer
    from developer.review import list_changed_files
    from developer.runtime import detect_project_commands
    from developer.sandbox_runtime import SandboxStatus, detect_container_runtime, get_docker_sandbox_status, list_pending_changes
    from developer.storage import detect_git_summary, get_workspace
    from developer.todos import list_todos

    workspace = get_workspace(workspace_id)
    if workspace is None:
        raise ValueError(f"Developer workspace not found: {workspace_id}")
    error = ""
    try:
        git_summary = detect_git_summary(workspace.path)
    except Exception as exc:
        git_summary = {"is_git": False, "error": str(exc)}
        error = str(exc)
    try:
        changed_files = list_changed_files(workspace.path) if git_summary.get("is_git") else []
    except Exception as exc:
        logger.debug("Developer snapshot changed-file scan failed", exc_info=True)
        changed_files = []
        error = error or str(exc)
    diff_stats = (
        DiffStats(
            files=len(changed_files),
            additions=sum(item.additions for item in changed_files),
            deletions=sum(item.deletions for item in changed_files),
        )
        if git_summary.get("is_git")
        else None
    )
    try:
        command_specs = detect_project_commands(workspace.path)
    except Exception:
        logger.debug("Developer snapshot command detection failed", exc_info=True)
        command_specs = []
    try:
        devcontainer = detect_devcontainer(workspace.path, check_docker=False)
    except Exception as exc:
        logger.debug("Developer snapshot devcontainer detection failed", exc_info=True)
        from developer.devcontainer import DevcontainerInfo

        devcontainer = DevcontainerInfo(present=False, message=str(exc))
    if workspace.execution_mode == "docker":
        try:
            sandbox_probe = detect_container_runtime()
        except Exception as exc:
            sandbox_probe = SandboxProbe(False, message=str(exc))
        try:
            sandbox_status = get_docker_sandbox_status(workspace)
        except Exception as exc:
            sandbox_status = SandboxStatus(
                available=False,
                image=workspace.sandbox_image,
                network=workspace.sandbox_network,
                message=str(exc),
            )
    else:
        sandbox_probe = SandboxProbe(False, message="Docker Sandbox is not active.")
        sandbox_status = None
    try:
        sandbox_pending_changes = list_pending_changes(workspace_id=workspace.id, thread_id=thread_id)
    except Exception:
        logger.debug("Developer snapshot sandbox pending scan failed", exc_info=True)
        sandbox_pending_changes = []
    return InspectorSnapshot(
        workspace_id=workspace_id,
        thread_id=thread_id,
        version=0,
        created_at=time.time(),
        workspace=workspace,
        git_summary=git_summary,
        todos=list_todos(thread_id),
        changed_files=changed_files,
        diff_stats=diff_stats,
        agent_changes=list_change_sets(workspace_id=workspace.id, thread_id=thread_id or ""),
        command_specs=command_specs,
        devcontainer=devcontainer,
        sandbox_probe=sandbox_probe,
        sandbox_status=sandbox_status,
        sandbox_pending_changes=sandbox_pending_changes,
        error=error,
    )
