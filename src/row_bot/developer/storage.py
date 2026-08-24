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

from row_bot.data_paths import get_row_bot_data_dir
from row_bot.developer.state import DeveloperWorkspace
from row_bot.approval_policy import legacy_developer_mode_to_approval_mode

logger = logging.getLogger(__name__)

DATA_DIR = get_row_bot_data_dir()
DEVELOPER_DIR = DATA_DIR / "developer"
WORKSPACES_PATH = DEVELOPER_DIR / "workspaces.json"
GIT_REPOSITORY_URL_PREFIXES = ("http://", "https://", "git@", "ssh://", "git://")
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


def delete_workspace_record(workspace_id: str) -> bool:
    """Delete only a Row-Bot workspace registry row; never touch its files."""

    clean = str(workspace_id or "").strip()
    if not clean:
        return False
    payload = _load_payload()
    rows = [
        raw
        for raw in payload.get("workspaces", [])
        if not (isinstance(raw, dict) and str(raw.get("id") or "") == clean)
    ]
    if len(rows) == len(payload.get("workspaces", [])):
        return False
    payload["workspaces"] = rows
    _save_payload(payload)
    return True


def clear_thread_references(
    thread_id: str,
    *,
    recovery_workspace_id: str = "",
) -> int:
    """Clear stale default-thread pointers and optionally expose recovery."""

    clean_thread_id = str(thread_id or "").strip()
    clean_recovery_id = str(recovery_workspace_id or "").strip()
    if not clean_thread_id and not clean_recovery_id:
        return 0
    payload = _load_payload()
    changed = 0
    rows: list[dict] = []
    for raw in payload.get("workspaces", []):
        if not isinstance(raw, dict):
            continue
        next_raw = dict(raw)
        row_changed = False
        if clean_thread_id and str(next_raw.get("default_thread_id") or "") == clean_thread_id:
            next_raw["default_thread_id"] = ""
            row_changed = True
        if clean_recovery_id and str(next_raw.get("id") or "") == clean_recovery_id:
            if bool(next_raw.get("hidden")):
                next_raw["hidden"] = False
                row_changed = True
        if row_changed:
            next_raw["updated_at"] = datetime.now().isoformat()
            changed += 1
        rows.append(next_raw)
    if changed:
        payload["workspaces"] = rows
        _save_payload(payload)
    return changed


def set_workspace_approval_mode(workspace_id: str, approval_mode: str) -> DeveloperWorkspace:
    workspace = get_workspace(workspace_id)
    if workspace is None:
        raise ValueError(f"Developer workspace not found: {workspace_id}")
    workspace.approval_mode = legacy_developer_mode_to_approval_mode(approval_mode)
    if workspace.default_thread_id:
        try:
            from row_bot.threads import _set_thread_approval_mode

            _set_thread_approval_mode(workspace.default_thread_id, workspace.approval_mode)
        except Exception:
            logger.debug("Failed to mirror Developer approval mode to thread", exc_info=True)
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
        if execution_mode == "docker":
            from row_bot.developer.sandbox_runtime import (
                OFFICIAL_CONTAINER_SANDBOX_UNAVAILABLE,
            )
            from row_bot.runtime_paths import is_containerized_runtime

            if is_containerized_runtime():
                raise ValueError(OFFICIAL_CONTAINER_SANDBOX_UNAVAILABLE)
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


def looks_like_git_repository_url(source: str) -> bool:
    return str(source or "").strip().startswith(GIT_REPOSITORY_URL_PREFIXES)


def suggested_clone_name(repo_url: str) -> str:
    cleaned = repo_url.strip().rstrip("/")
    cleaned = cleaned.split("?", 1)[0].split("#", 1)[0]
    scp_match = re.match(r"^[^/@\s]+@[^:\s]+:(?P<path>.+)$", cleaned)
    if scp_match:
        cleaned = scp_match.group("path").rstrip("/")
    tail = cleaned.rsplit("/", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", tail).strip("-._")
    return safe or "repository"


def git_clone_error_message(repo_url: str, target: pathlib.Path, exc: subprocess.CalledProcessError) -> str:
    detail = (exc.stderr or exc.stdout or "").strip()
    message = f"Git clone failed for {repo_url} into {target} (exit {exc.returncode})."
    if detail:
        message = f"{message}\n{detail}"
    return message


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
    try:
        subprocess.run(
            ["git", "clone", repo_url, str(target)],
            cwd=str(parent),
            check=True,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(git_clone_error_message(repo_url, target, exc)) from exc
    return add_or_update_local_workspace(str(target), repo_url=repo_url)


def list_workspace_threads(workspace_id: str) -> list[tuple]:
    """Return Developer code threads grouped under a project workspace."""
    from row_bot.threads import list_developer_workspace_threads

    return list_developer_workspace_threads(workspace_id)


def latest_workspace_thread(workspace_id: str) -> str | None:
    """Return the most recently updated thread id for a Developer workspace."""
    rows = list_workspace_threads(workspace_id)
    return str(rows[0][0]) if rows else None


def create_workspace_thread(
    workspace_id: str,
    *,
    name: str | None = None,
    name_source: str = "auto",
    use_worktree: bool | None = None,
    seed_mode: str = "current_changes",
) -> str:
    """Create a new empty Developer thread for a project workspace."""
    workspace = get_workspace(workspace_id)
    if workspace is None:
        raise ValueError(f"Developer workspace not found: {workspace_id}")
    from row_bot.threads import create_thread

    thread_name = str(name or "").strip()
    if not thread_name:
        thread_name = f"Thread {datetime.now().strftime('%b %d, %H:%M')}"
    thread_id = create_thread(
        thread_name,
        thread_type="code",
        developer_workspace_id=workspace.id,
        project_workspace_id=workspace.id,
        approval_mode=workspace.approval_mode,
        name_source=name_source,
    )
    if use_worktree is None:
        use_worktree = is_git_repository_root(workspace.path)
    if use_worktree:
        from row_bot.developer.worktrees import allocate_thread_worktree, switch_thread_to_worktree

        allocated = allocate_thread_worktree(
            thread_id,
            workspace.id,
            objective=thread_name,
            seed_mode=seed_mode,
        )
        if str(allocated.get("status") or "") != "active":
            raise ValueError(str(allocated.get("error") or "Failed to create Worktree."))
        switch_thread_to_worktree(thread_id, str(allocated.get("worktree_workspace_id") or ""))
    _seed_developer_thread_skills(thread_id)
    workspace.touch()
    save_workspace(workspace)
    return thread_id


def create_thread_worktree(
    thread_id: str,
    project_workspace_id: str,
    *,
    objective: str = "",
    seed_mode: str = "current_changes",
) -> dict:
    """Create a Worktree for an existing current-folder Developer thread."""
    project = get_workspace(project_workspace_id)
    if project is None:
        raise ValueError(f"Developer workspace not found: {project_workspace_id}")
    from row_bot.developer.worktrees import allocate_thread_worktree, switch_thread_to_worktree

    allocated = allocate_thread_worktree(
        thread_id,
        project.id,
        objective=objective or f"Developer thread {thread_id}",
        seed_mode=seed_mode,
    )
    if str(allocated.get("status") or "") != "active":
        raise ValueError(str(allocated.get("error") or "Failed to create Worktree."))
    switch_thread_to_worktree(thread_id, str(allocated.get("worktree_workspace_id") or ""))
    return allocated


def ensure_latest_workspace_thread(workspace_id: str) -> str:
    """Return the latest workspace thread, falling back to the legacy default."""
    latest = latest_workspace_thread(workspace_id)
    if latest:
        return latest
    return create_workspace_thread(workspace_id)


def ensure_workspace_thread(workspace_id: str) -> str:
    workspace = get_workspace(workspace_id)
    if workspace is None:
        raise ValueError(f"Developer workspace not found: {workspace_id}")
    from row_bot.threads import (
        create_thread,
        _get_thread_approval_mode_raw,
        _set_thread_approval_mode,
        _thread_exists,
    )

    if workspace.default_thread_id and _thread_exists(workspace.default_thread_id):
        if not _get_thread_approval_mode_raw(workspace.default_thread_id):
            _set_thread_approval_mode(workspace.default_thread_id, workspace.approval_mode)
        return workspace.default_thread_id
    thread_id = uuid.uuid4().hex[:12]
    name = f"Developer: {workspace.name}"
    create_thread(
        name,
        thread_id=thread_id,
        thread_type="code",
        developer_workspace_id=workspace.id,
        project_workspace_id=workspace.id,
        approval_mode=workspace.approval_mode,
    )
    _seed_developer_thread_skills(thread_id)
    workspace.default_thread_id = thread_id
    workspace.touch()
    save_workspace(workspace)
    return thread_id


def _seed_developer_thread_skills(thread_id: str) -> None:
    """Snapshot current Developer skill defaults onto a new Developer thread."""
    try:
        from row_bot.skills import get_default_active_skill_names
        from row_bot.threads import set_thread_skills_override

        set_thread_skills_override(
            thread_id,
            get_default_active_skill_names("developer"),
        )
    except Exception:
        logger.debug("Failed to seed Developer thread skills", exc_info=True)


def detect_git_summary(path: str) -> dict:
    from row_bot.developer.git import get_git_status

    status = get_git_status(path)
    return {
        "is_git": status.is_git,
        "branch": status.branch,
        "dirty": status.dirty,
        "remote": status.remote,
        "ahead_behind": status.ahead_behind,
        "repo_root": status.repo_root,
        "is_repo_root": status.is_repo_root,
        "error": status.error,
    }


def is_git_repository_root(path: str) -> bool:
    folder = pathlib.Path(path).expanduser().resolve()
    status = detect_git_summary(str(folder))
    if not status.get("is_git") or status.get("error"):
        return False
    return bool(status.get("is_repo_root"))


def workspace_updated_label(workspace: DeveloperWorkspace) -> str:
    try:
        dt = datetime.fromisoformat(workspace.updated_at)
        return dt.strftime("%b %d, %H:%M")
    except Exception:
        return workspace.updated_at[:16] if workspace.updated_at else ""
