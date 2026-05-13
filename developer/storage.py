from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import re
import subprocess
import tempfile
import uuid
from datetime import datetime

from developer.state import DeveloperWorkspace

logger = logging.getLogger(__name__)

DATA_DIR = pathlib.Path(os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth"))
DEVELOPER_DIR = DATA_DIR / "developer"
WORKSPACES_PATH = DEVELOPER_DIR / "workspaces.json"
_REPLACE_RETRY_WINERRORS = {5, 32}


def _ensure_dirs() -> None:
    DEVELOPER_DIR.mkdir(parents=True, exist_ok=True)


def _write_json_atomic(path: pathlib.Path, payload: dict) -> None:
    _ensure_dirs()
    fd: int | None = None
    tmp_path: pathlib.Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
        tmp_path = pathlib.Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        for attempt in range(5):
            try:
                tmp_path.replace(path)
                return
            except OSError as exc:
                if getattr(exc, "winerror", None) not in _REPLACE_RETRY_WINERRORS or attempt >= 4:
                    raise
    finally:
        if fd is not None:
            os.close(fd)
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                logger.debug("Failed to remove temp developer storage file %s", tmp_path, exc_info=True)


def _load_payload() -> dict:
    _ensure_dirs()
    if not WORKSPACES_PATH.exists():
        return {"workspaces": [], "clone_parent_folders": []}
    try:
        data = json.loads(WORKSPACES_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to load Developer workspaces from %s", WORKSPACES_PATH)
        return {"workspaces": [], "clone_parent_folders": []}
    if not isinstance(data, dict):
        return {"workspaces": [], "clone_parent_folders": []}
    data.setdefault("workspaces", [])
    data.setdefault("clone_parent_folders", [])
    return data


def _save_payload(payload: dict) -> None:
    payload.setdefault("workspaces", [])
    payload.setdefault("clone_parent_folders", [])
    _write_json_atomic(WORKSPACES_PATH, payload)


def _workspace_id_for_path(path: pathlib.Path) -> str:
    normalized = str(path.resolve()).casefold()
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]
    return f"dev_{digest}"


def _workspace_name(path: pathlib.Path) -> str:
    return path.name or str(path)


def list_workspaces(*, include_hidden: bool = False) -> list[DeveloperWorkspace]:
    payload = _load_payload()
    workspaces: list[DeveloperWorkspace] = []
    for raw in payload.get("workspaces", []):
        if isinstance(raw, dict):
            try:
                workspace = DeveloperWorkspace.from_dict(raw)
                if include_hidden or not workspace.hidden:
                    workspaces.append(workspace)
            except Exception:
                logger.debug("Skipping invalid Developer workspace entry: %r", raw, exc_info=True)
    workspaces.sort(key=lambda ws: ws.updated_at or "", reverse=True)
    return workspaces


def get_workspace(workspace_id: str) -> DeveloperWorkspace | None:
    for workspace in list_workspaces(include_hidden=True):
        if workspace.id == workspace_id:
            return workspace
    return None


def save_workspace(workspace: DeveloperWorkspace) -> DeveloperWorkspace:
    payload = _load_payload()
    seen = False
    rows: list[dict] = []
    for raw in payload.get("workspaces", []):
        if not isinstance(raw, dict):
            continue
        if raw.get("id") == workspace.id:
            rows.append(workspace.to_dict())
            seen = True
        else:
            rows.append(raw)
    if not seen:
        rows.append(workspace.to_dict())
    payload["workspaces"] = rows
    _save_payload(payload)
    return workspace


def remove_workspace(workspace_id: str) -> DeveloperWorkspace:
    """Hide a Developer workspace from recents without touching files or history."""
    workspace = get_workspace(workspace_id)
    if workspace is None:
        raise ValueError(f"Developer workspace not found: {workspace_id}")
    workspace.hidden = True
    workspace.touch()
    return save_workspace(workspace)


def set_workspace_approval_mode(workspace_id: str, approval_mode: str) -> DeveloperWorkspace:
    workspace = get_workspace(workspace_id)
    if workspace is None:
        raise ValueError(f"Developer workspace not found: {workspace_id}")
    if approval_mode not in {"read_only", "ask", "auto_edit", "agent_run"}:
        raise ValueError(f"Unknown approval mode: {approval_mode}")
    workspace.approval_mode = approval_mode  # type: ignore[assignment]
    workspace.touch()
    return save_workspace(workspace)


def set_workspace_execution_settings(
    workspace_id: str,
    *,
    execution_mode: str | None = None,
    sandbox_network: str | None = None,
    sandbox_image: str | None = None,
) -> DeveloperWorkspace:
    workspace = get_workspace(workspace_id)
    if workspace is None:
        raise ValueError(f"Developer workspace not found: {workspace_id}")
    if execution_mode is not None:
        if execution_mode not in {"local", "docker"}:
            raise ValueError(f"Unknown execution mode: {execution_mode}")
        workspace.execution_mode = execution_mode  # type: ignore[assignment]
    if sandbox_network is not None:
        if sandbox_network not in {"off", "ask", "on"}:
            raise ValueError(f"Unknown sandbox network policy: {sandbox_network}")
        workspace.sandbox_network = sandbox_network  # type: ignore[assignment]
    if sandbox_image is not None:
        image = str(sandbox_image or "").strip()
        if not image:
            raise ValueError("Sandbox image cannot be empty.")
        workspace.sandbox_image = image
    workspace.touch()
    return save_workspace(workspace)


def add_or_update_local_workspace(path: str, *, repo_url: str = "") -> DeveloperWorkspace:
    if not str(path or "").strip():
        raise ValueError("Choose a workspace folder before opening a Developer project.")
    raw = pathlib.Path(path).expanduser()
    resolved = raw.resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise ValueError(f"Workspace folder does not exist: {path}")
    workspace_id = _workspace_id_for_path(resolved)
    existing = get_workspace(workspace_id)
    if existing:
        existing.path = str(resolved)
        existing.name = _workspace_name(resolved)
        if repo_url:
            existing.repo_url = repo_url
        existing.hidden = False
        existing.touch()
        return save_workspace(existing)
    workspace = DeveloperWorkspace(
        id=workspace_id,
        name=_workspace_name(resolved),
        path=str(resolved),
        repo_url=repo_url,
        trusted=True,
    )
    return save_workspace(workspace)


def list_clone_parent_folders() -> list[str]:
    payload = _load_payload()
    rows = [str(p) for p in payload.get("clone_parent_folders", []) if p]
    return rows[:8]


def remember_clone_parent_folder(path: str) -> None:
    resolved = str(pathlib.Path(path).expanduser().resolve())
    payload = _load_payload()
    rows = [str(p) for p in payload.get("clone_parent_folders", []) if p and str(p) != resolved]
    payload["clone_parent_folders"] = [resolved] + rows[:7]
    _save_payload(payload)


def suggested_clone_name(repo_url: str) -> str:
    tail = repo_url.rstrip("/").rsplit("/", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", tail).strip("-._")
    return safe or "repository"


def clone_repository(repo_url: str, destination_parent: str) -> DeveloperWorkspace:
    if not repo_url.strip():
        raise ValueError("Repository URL is required.")
    parent = pathlib.Path(destination_parent).expanduser().resolve()
    if not parent.exists() or not parent.is_dir():
        raise ValueError(f"Clone destination folder does not exist: {destination_parent}")
    remember_clone_parent_folder(str(parent))
    target = parent / suggested_clone_name(repo_url)
    if target.exists():
        raise FileExistsError(f"Clone target already exists: {target}")
    subprocess.run(
        ["git", "clone", repo_url, str(target)],
        cwd=str(parent),
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return add_or_update_local_workspace(str(target), repo_url=repo_url)


def ensure_workspace_thread(workspace_id: str) -> str:
    workspace = get_workspace(workspace_id)
    if workspace is None:
        raise ValueError(f"Developer workspace not found: {workspace_id}")
    from threads import (
        _save_thread_meta,
        _set_thread_developer_workspace,
        _set_thread_type,
        _thread_exists,
    )

    if workspace.default_thread_id and _thread_exists(workspace.default_thread_id):
        return workspace.default_thread_id
    thread_id = uuid.uuid4().hex[:12]
    name = f"Developer: {workspace.name}"
    _save_thread_meta(thread_id, name)
    _set_thread_type(thread_id, "code")
    _set_thread_developer_workspace(thread_id, workspace.id)
    workspace.default_thread_id = thread_id
    workspace.touch()
    save_workspace(workspace)
    return thread_id


def detect_git_summary(path: str) -> dict:
    from developer.git import get_git_status

    status = get_git_status(path)
    return {
        "is_git": status.is_git,
        "branch": status.branch,
        "dirty": status.dirty,
        "remote": status.remote,
        "ahead_behind": status.ahead_behind,
        "error": status.error,
    }


def workspace_updated_label(workspace: DeveloperWorkspace) -> str:
    try:
        dt = datetime.fromisoformat(workspace.updated_at)
        return dt.strftime("%b %d, %H:%M")
    except Exception:
        return workspace.updated_at[:16] if workspace.updated_at else ""
