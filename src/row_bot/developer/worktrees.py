"""Developer worktree allocation for threads and child Agent runs."""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import uuid
from datetime import datetime
from typing import Any, Mapping

from row_bot.developer.git import create_worktree, get_git_status, sanitize_branch_name


OWNER_KINDS = {"thread", "agent_run", "workflow_run"}
WORKTREE_STATUSES = {"active", "failed", "preserved", "archived"}
WORKTREE_CLEANUP_STATES = {"preserve", "requested", "completed", "failed"}
SEED_MODES = {"current_changes", "last_commit"}


def _now() -> str:
    return datetime.now().isoformat()


def _json_text(value: Mapping[str, Any] | None) -> str:
    return json.dumps(dict(value or {}), ensure_ascii=False, sort_keys=True)


def _json_obj(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _ensure_schema() -> None:
    from row_bot.tasks import _get_conn

    conn = _get_conn()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS developer_worktrees (
                id TEXT PRIMARY KEY,
                owner_kind TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                project_workspace_id TEXT NOT NULL,
                worktree_workspace_id TEXT NOT NULL,
                project_path TEXT NOT NULL,
                worktree_path TEXT NOT NULL,
                branch_name TEXT NOT NULL,
                base_branch TEXT DEFAULT '',
                base_commit TEXT DEFAULT '',
                status TEXT DEFAULT 'active',
                cleanup_state TEXT DEFAULT 'preserve',
                error TEXT DEFAULT '',
                metadata_json TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_developer_worktrees_owner "
            "ON developer_worktrees(owner_kind, owner_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_developer_worktrees_project "
            "ON developer_worktrees(project_workspace_id, status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_developer_worktrees_workspace "
            "ON developer_worktrees(worktree_workspace_id)"
        )
        conn.commit()
    finally:
        conn.close()


def _row_to_worktree(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["metadata_json"] = _json_obj(data.get("metadata_json"))
    return data


def _safe_segment(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-._")
    return text[:80] or "workspace"


def _branch_slug(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._/-]+", "-", str(value or "")).strip("/.-")
    text = re.sub(r"-+", "-", text)
    return sanitize_branch_name(text[:48] or "worktree")


def _run_git_text(path: pathlib.Path, args: list[str], *, timeout: int = 10) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout.strip()


def _run_git_bytes(path: pathlib.Path, args: list[str], *, timeout: int = 10) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        timeout=timeout,
    )
    return result.stdout


def _apply_patch_bytes(path: pathlib.Path, patch: bytes, *, staged: bool) -> None:
    if not patch:
        return
    args = ["git", "-C", str(path), "apply", "--whitespace=nowarn"]
    if staged:
        args.append("--index")
    subprocess.run(args, input=patch, check=True, capture_output=True, timeout=30)


def _git_top_level(path: pathlib.Path) -> pathlib.Path:
    top_level = _run_git_text(path, ["rev-parse", "--show-toplevel"])
    return pathlib.Path(top_level).expanduser().resolve()


def _require_git_root(path: pathlib.Path, *, label: str) -> None:
    status = get_git_status(str(path))
    if status.error:
        raise ValueError(f"Cannot create Worktree: {status.error}")
    if not status.is_git:
        raise ValueError("Worktree requires a git repository.")
    if _git_top_level(path) != path:
        raise ValueError(f"Worktree requires a git repository root for the {label} folder.")


def _untracked_files(path: pathlib.Path) -> list[str]:
    raw = _run_git_bytes(path, ["ls-files", "--others", "--exclude-standard", "-z"])
    return [
        item.decode("utf-8", errors="surrogateescape")
        for item in raw.split(b"\0")
        if item
    ]


def _copy_untracked_files(source: pathlib.Path, target: pathlib.Path, files: list[str]) -> list[str]:
    copied: list[str] = []
    source_root = source.resolve()
    target_root = target.resolve()
    for rel in files:
        clean = pathlib.PurePosixPath(str(rel).replace("\\", "/"))
        if clean.is_absolute() or ".." in clean.parts or ".git" in clean.parts:
            continue
        src = (source_root / pathlib.Path(*clean.parts)).resolve()
        if source_root not in src.parents and src != source_root:
            continue
        if not src.is_file():
            continue
        dst = (target_root / pathlib.Path(*clean.parts)).resolve()
        if target_root not in dst.parents and dst != target_root:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(str(clean))
    return copied


def _capture_seed_state(source_path: pathlib.Path, seed_mode: str) -> dict[str, Any]:
    status = get_git_status(str(source_path))
    base_commit = ""
    try:
        base_commit = _run_git_text(source_path, ["rev-parse", "HEAD"])
    except Exception:
        base_commit = ""
    seed = {
        "seed_mode": seed_mode,
        "base_branch": status.branch,
        "base_commit": base_commit,
        "source_dirty": bool(status.dirty),
        "staged_patch": b"",
        "unstaged_patch": b"",
        "untracked_files": [],
    }
    if seed_mode != "current_changes":
        return seed
    seed["staged_patch"] = _run_git_bytes(source_path, ["diff", "--cached", "--binary"])
    seed["unstaged_patch"] = _run_git_bytes(source_path, ["diff", "--binary"])
    seed["untracked_files"] = _untracked_files(source_path)
    return seed


def _seed_current_changes(
    source_path: pathlib.Path,
    worktree_path: pathlib.Path,
    seed: Mapping[str, Any],
) -> dict[str, Any]:
    staged_patch = seed.get("staged_patch") if isinstance(seed.get("staged_patch"), bytes) else b""
    unstaged_patch = seed.get("unstaged_patch") if isinstance(seed.get("unstaged_patch"), bytes) else b""
    untracked = [str(item) for item in seed.get("untracked_files") or []]
    _apply_patch_bytes(worktree_path, staged_patch, staged=True)
    _apply_patch_bytes(worktree_path, unstaged_patch, staged=False)
    copied = _copy_untracked_files(source_path, worktree_path, untracked)
    return {
        "seeded_from_current_changes": bool(staged_patch or unstaged_patch or copied),
        "seeded_staged_diff": bool(staged_patch),
        "seeded_unstaged_diff": bool(unstaged_patch),
        "seeded_untracked_files": copied,
    }


def _insert_or_update(
    *,
    row_id: str,
    owner_kind: str,
    owner_id: str,
    project_workspace_id: str,
    project_path: str,
    worktree_workspace_id: str = "",
    worktree_path: str = "",
    branch_name: str = "",
    base_branch: str = "",
    base_commit: str = "",
    status: str,
    cleanup_state: str = "preserve",
    error: str = "",
    metadata_json: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    _ensure_schema()
    now = _now()
    from row_bot.tasks import _get_conn

    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO developer_worktrees "
            "(id, owner_kind, owner_id, project_workspace_id, worktree_workspace_id, "
            "project_path, worktree_path, branch_name, base_branch, base_commit, "
            "status, cleanup_state, error, metadata_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(owner_kind, owner_id) DO UPDATE SET "
            "project_workspace_id = excluded.project_workspace_id, "
            "worktree_workspace_id = excluded.worktree_workspace_id, "
            "project_path = excluded.project_path, worktree_path = excluded.worktree_path, "
            "branch_name = excluded.branch_name, base_branch = excluded.base_branch, "
            "base_commit = excluded.base_commit, status = excluded.status, "
            "cleanup_state = excluded.cleanup_state, error = excluded.error, "
            "metadata_json = excluded.metadata_json, updated_at = excluded.updated_at",
            (
                row_id,
                owner_kind,
                owner_id,
                project_workspace_id,
                worktree_workspace_id,
                project_path,
                worktree_path,
                branch_name,
                base_branch,
                base_commit,
                status,
                cleanup_state,
                error,
                _json_text(metadata_json),
                now,
                now,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM developer_worktrees WHERE owner_kind = ? AND owner_id = ?",
            (owner_kind, owner_id),
        ).fetchone()
    finally:
        conn.close()
    return _row_to_worktree(row)


def get_worktree(owner_kind: str, owner_id: str) -> dict[str, Any] | None:
    _ensure_schema()
    from row_bot.tasks import _get_conn

    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM developer_worktrees WHERE owner_kind = ? AND owner_id = ?",
            (str(owner_kind), str(owner_id)),
        ).fetchone()
    finally:
        conn.close()
    return _row_to_worktree(row) if row else None


def get_worktree_for_thread(thread_id: str) -> dict[str, Any] | None:
    return get_worktree("thread", thread_id)


def get_worktree_for_run(run_id: str) -> dict[str, Any] | None:
    return get_worktree("agent_run", run_id)


def get_worktree_for_workspace(workspace_id: str) -> dict[str, Any] | None:
    _ensure_schema()
    from row_bot.tasks import _get_conn

    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM developer_worktrees WHERE worktree_workspace_id = ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (str(workspace_id),),
        ).fetchone()
    finally:
        conn.close()
    return _row_to_worktree(row) if row else None


def list_project_worktrees(
    project_workspace_id: str,
    statuses: set[str] | None = None,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    _ensure_schema()
    clauses = ["project_workspace_id = ?"]
    params: list[Any] = [str(project_workspace_id)]
    if statuses:
        clean_statuses = [status for status in statuses if status in WORKTREE_STATUSES]
        if clean_statuses:
            clauses.append(f"status IN ({', '.join('?' for _ in clean_statuses)})")
            params.extend(clean_statuses)
    params.append(max(1, int(limit)))
    from row_bot.tasks import _get_conn

    conn = _get_conn()
    try:
        rows = conn.execute(
            f"SELECT * FROM developer_worktrees WHERE {' AND '.join(clauses)} "
            "ORDER BY updated_at DESC LIMIT ?",
            params,
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_worktree(row) for row in rows]


def list_worktrees(
    *,
    project_workspace_id: str = "",
    statuses: set[str] | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if project_workspace_id:
        return list_project_worktrees(project_workspace_id, statuses, limit=limit)
    _ensure_schema()
    params: list[Any] = []
    where = ""
    if statuses:
        clean_statuses = [status for status in statuses if status in WORKTREE_STATUSES]
        if clean_statuses:
            where = f"WHERE status IN ({', '.join('?' for _ in clean_statuses)})"
            params.extend(clean_statuses)
    params.append(max(1, int(limit)))
    from row_bot.tasks import _get_conn

    conn = _get_conn()
    try:
        rows = conn.execute(
            f"SELECT * FROM developer_worktrees {where} ORDER BY updated_at DESC LIMIT ?",
            params,
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_worktree(row) for row in rows]


def _owner_prefix(owner_kind: str) -> str:
    return {
        "thread": "thread",
        "agent_run": "agent",
        "workflow_run": "workflow",
    }.get(owner_kind, "worktree")


def _project_for_source_workspace(source_workspace_id: str) -> str:
    row = get_worktree_for_workspace(source_workspace_id)
    if row and row.get("project_workspace_id"):
        return str(row["project_workspace_id"])
    return source_workspace_id


def _copy_workspace_settings(source, target) -> None:
    target.hidden = True
    target.approval_mode = source.approval_mode
    target.execution_mode = source.execution_mode
    target.sandbox_network = source.sandbox_network
    target.sandbox_image = source.sandbox_image
    target.sandbox_env_allowlist = list(source.sandbox_env_allowlist)
    target.trusted = source.trusted


def allocate_worktree(
    owner_kind: str,
    owner_id: str,
    project_workspace_id: str,
    *,
    source_workspace_id: str = "",
    objective: str = "",
    branch_slug: str = "",
    seed_mode: str = "current_changes",
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a hidden Developer workspace backed by a local git worktree."""
    owner_kind = str(owner_kind or "").strip()
    if owner_kind not in OWNER_KINDS:
        raise ValueError(f"Unknown Developer worktree owner kind: {owner_kind}")
    clean_owner_id = _safe_segment(owner_id or uuid.uuid4().hex[:12])
    existing = get_worktree(owner_kind, clean_owner_id)
    if existing and existing.get("worktree_workspace_id") and existing.get("worktree_path"):
        return existing
    seed_mode = str(seed_mode or "current_changes").strip()
    if seed_mode not in SEED_MODES:
        raise ValueError(f"Unknown Worktree seed mode: {seed_mode}")

    from row_bot.developer.storage import add_or_update_local_workspace, get_workspace, save_workspace

    project = get_workspace(project_workspace_id)
    if project is None:
        raise ValueError(f"Developer project workspace not found: {project_workspace_id}")
    source_id = source_workspace_id or project_workspace_id
    source = get_workspace(source_id)
    if source is None:
        raise ValueError(f"Developer source workspace not found: {source_id}")
    project_path = pathlib.Path(project.path).expanduser().resolve()
    source_path = pathlib.Path(source.path).expanduser().resolve()
    _require_git_root(project_path, label="project")
    _require_git_root(source_path, label="source")

    seed = _capture_seed_state(source_path, seed_mode)
    base_parent = project_path.parent / ".row-bot-worktrees" / _safe_segment(project.id)
    base_parent.mkdir(parents=True, exist_ok=True)
    slug = _branch_slug(branch_slug or objective or clean_owner_id)
    branch_base = sanitize_branch_name(f"row-bot/{_owner_prefix(owner_kind)}-{clean_owner_id}-{slug}")
    row_id = uuid.uuid4().hex[:12]
    base_metadata = {
        **dict(metadata or {}),
        "objective": objective,
        "source_workspace_id": source.id,
        "source_path": str(source_path),
        "source_dirty": bool(seed.get("source_dirty")),
        "seed_mode": seed_mode,
        "seeded_from_current_changes": False,
        "seeded_staged_diff": False,
        "seeded_unstaged_diff": False,
        "seeded_untracked_files": [],
    }
    last_error = ""
    for attempt in range(1, 8):
        branch = branch_base if attempt == 1 else sanitize_branch_name(f"{branch_base}-{attempt}")
        try:
            worktree_path = create_worktree(str(source_path), str(base_parent), branch)
        except (FileExistsError, subprocess.CalledProcessError) as exc:
            last_error = str(exc)
            continue
        except Exception as exc:
            last_error = str(exc)
            break
        workspace = add_or_update_local_workspace(str(worktree_path), repo_url=project.repo_url)
        _copy_workspace_settings(source, workspace)
        save_workspace(workspace)
        metadata_json = dict(base_metadata)
        try:
            if seed_mode == "current_changes":
                metadata_json.update(_seed_current_changes(source_path, worktree_path, seed))
            return _insert_or_update(
                row_id=row_id,
                owner_kind=owner_kind,
                owner_id=clean_owner_id,
                project_workspace_id=project.id,
                project_path=str(project_path),
                worktree_workspace_id=workspace.id,
                worktree_path=str(pathlib.Path(worktree_path).resolve()),
                branch_name=branch,
                base_branch=str(seed.get("base_branch") or ""),
                base_commit=str(seed.get("base_commit") or ""),
                status="active",
                cleanup_state="preserve",
                metadata_json=metadata_json,
            )
        except Exception as exc:
            last_error = str(exc)
            return _insert_or_update(
                row_id=row_id,
                owner_kind=owner_kind,
                owner_id=clean_owner_id,
                project_workspace_id=project.id,
                project_path=str(project_path),
                worktree_workspace_id=workspace.id,
                worktree_path=str(pathlib.Path(worktree_path).resolve()),
                branch_name=branch,
                base_branch=str(seed.get("base_branch") or ""),
                base_commit=str(seed.get("base_commit") or ""),
                status="failed",
                cleanup_state="preserve",
                error=last_error or "Failed to seed Worktree.",
                metadata_json=metadata_json,
            )
    return _insert_or_update(
        row_id=row_id,
        owner_kind=owner_kind,
        owner_id=clean_owner_id,
        project_workspace_id=project_workspace_id,
        project_path=str(project_path),
        status="failed",
        cleanup_state="preserve",
        error=last_error or "Failed to create Worktree.",
        metadata_json=base_metadata,
    )


def allocate_thread_worktree(
    thread_id: str,
    project_workspace_id: str,
    *,
    objective: str = "",
    seed_mode: str = "current_changes",
) -> dict[str, Any]:
    return allocate_worktree(
        "thread",
        thread_id,
        project_workspace_id,
        objective=objective,
        seed_mode=seed_mode,
    )


def allocate_agent_worktree(
    run_id: str,
    parent_workspace_id: str,
    *,
    objective: str = "",
    branch_slug: str = "",
    seed_mode: str = "current_changes",
    parent_thread_id: str = "",
) -> dict[str, Any]:
    project_workspace_id = ""
    if parent_thread_id:
        try:
            from row_bot.threads import _get_thread_project_workspace

            project_workspace_id = _get_thread_project_workspace(parent_thread_id)
        except Exception:
            project_workspace_id = ""
    project_workspace_id = project_workspace_id or _project_for_source_workspace(parent_workspace_id)
    return allocate_worktree(
        "agent_run",
        run_id,
        project_workspace_id,
        source_workspace_id=parent_workspace_id,
        objective=objective,
        branch_slug=branch_slug,
        seed_mode=seed_mode,
        metadata={
            "parent_owner_kind": "thread" if parent_thread_id else "",
            "parent_owner_id": parent_thread_id,
        },
    )


def switch_thread_to_worktree(thread_id: str, worktree_workspace_id: str) -> None:
    row = get_worktree_for_workspace(worktree_workspace_id)
    if row is None:
        raise ValueError("Worktree not found for workspace.")
    from row_bot.threads import _set_thread_developer_workspace, _set_thread_project_workspace

    _set_thread_developer_workspace(thread_id, worktree_workspace_id)
    _set_thread_project_workspace(thread_id, str(row.get("project_workspace_id") or ""))


def mark_worktree_preserved(
    owner_kind: str,
    owner_id: str | None = None,
    *,
    reason: str = "",
) -> dict[str, Any] | None:
    if owner_id is None:
        owner_id = owner_kind
        owner_kind = "agent_run"
    current = get_worktree(owner_kind, owner_id)
    if not current:
        return None
    return _insert_or_update(
        row_id=str(current["id"]),
        owner_kind=str(current["owner_kind"]),
        owner_id=str(current["owner_id"]),
        project_workspace_id=str(current["project_workspace_id"]),
        project_path=str(current["project_path"]),
        worktree_workspace_id=str(current.get("worktree_workspace_id") or ""),
        worktree_path=str(current.get("worktree_path") or ""),
        branch_name=str(current.get("branch_name") or ""),
        base_branch=str(current.get("base_branch") or ""),
        base_commit=str(current.get("base_commit") or ""),
        status="preserved",
        cleanup_state="preserve",
        metadata_json={
            **(current.get("metadata_json") or {}),
            "preserved_reason": reason,
        },
    )


def worktree_diff_summary(owner_kind: str, owner_id: str | None = None) -> dict[str, Any]:
    if owner_id is None:
        owner_id = owner_kind
        owner_kind = "agent_run"
    row = get_worktree(owner_kind, owner_id)
    if not row:
        return {"ok": False, "error": "Worktree not found."}
    path = pathlib.Path(str(row.get("worktree_path") or ""))
    if not path.exists():
        return {"ok": False, "error": "Worktree path does not exist.", "worktree": row}
    git_status = get_git_status(str(path))
    summary: dict[str, Any] = {
        "ok": not bool(git_status.error),
        "worktree": row,
        "branch": git_status.branch,
        "dirty": git_status.dirty,
        "ahead_behind": git_status.ahead_behind,
        "error": git_status.error,
        "status_lines": [],
    }
    try:
        status_out = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        summary["status_lines"] = status_out.stdout.splitlines()[:50]
    except Exception as exc:
        summary["ok"] = False
        summary["error"] = str(exc)
    return summary


def _status_paths(path: pathlib.Path) -> set[str]:
    try:
        status_out = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return set()
    paths: set[str] = set()
    for line in status_out.stdout.splitlines():
        if len(line) < 4:
            continue
        raw_path = line[3:].strip()
        if " -> " in raw_path:
            paths.update(part.strip() for part in raw_path.split(" -> ") if part.strip())
        elif raw_path:
            paths.add(raw_path)
    return paths


def source_dirty_missing_from_worktree(owner_kind: str, owner_id: str) -> dict[str, Any]:
    row = get_worktree(owner_kind, owner_id)
    if not row:
        return {"ok": False, "missing": [], "error": "Worktree not found."}
    metadata = row.get("metadata_json") or {}
    source_path = pathlib.Path(str(metadata.get("source_path") or row.get("project_path") or ""))
    worktree_path = pathlib.Path(str(row.get("worktree_path") or ""))
    if not source_path.exists() or not worktree_path.exists():
        return {"ok": False, "missing": [], "error": "Source or Worktree path does not exist."}
    source_paths = _status_paths(source_path)
    worktree_paths = _status_paths(worktree_path)
    missing = [
        rel
        for rel in sorted(source_paths)
        if rel not in worktree_paths and not (worktree_path / pathlib.Path(rel)).exists()
    ]
    return {
        "ok": True,
        "missing": missing,
        "source_path": str(source_path),
        "worktree_path": str(worktree_path),
    }


def _delete_worktree_record(row_id: str) -> bool:
    _ensure_schema()
    from row_bot import tasks

    conn = tasks._get_conn()  # noqa: SLF001
    try:
        cursor = conn.execute("DELETE FROM developer_worktrees WHERE id = ?", (row_id,))
        conn.commit()
    finally:
        conn.close()
    return bool(cursor.rowcount)


def cleanup_thread_developer_state(
    thread_id: str,
    *,
    workspace_id: str = "",
    project_workspace_id: str = "",
    owned_agent_run_id: str = "",
) -> dict[str, Any]:
    """Remove thread-owned Developer state while preserving recoverable work."""

    from row_bot.developer.change_ledger import delete_thread_change_sets
    from row_bot.developer.inspector_snapshot import clear_thread_snapshots
    from row_bot.developer.sandbox_runtime import (
        cleanup_thread_pending_changes,
        cleanup_workspace_sandbox,
        list_pending_changes,
        sandbox_shadow_path,
    )
    from row_bot.developer.storage import (
        clear_thread_references,
        delete_workspace_record,
        get_workspace,
    )
    from row_bot.developer.todos import delete_todos
    from row_bot.thread_cleanup import resolve_managed_path

    clean_thread_id = str(thread_id or "").strip()
    result: dict[str, Any] = {
        "changed": 0,
        "retained_worktree_path": "",
        "retained_sandbox": False,
        "warnings": [],
    }
    if not clean_thread_id:
        return result

    result["changed"] += int(delete_todos(clean_thread_id))
    result["changed"] += int(delete_thread_change_sets(clean_thread_id))
    pending_stats = cleanup_thread_pending_changes(clean_thread_id)
    result["changed"] += int(pending_stats["imported_removed"])
    result["changed"] += clear_thread_snapshots(clean_thread_id)

    worktree = get_worktree_for_thread(clean_thread_id)
    if worktree is None and workspace_id:
        candidate = get_worktree_for_workspace(workspace_id)
        # Resource bindings can be shared. A workspace ID alone does not grant
        # ownership of its allocation; callers capture their durable run owner
        # before purging it, or supply the exact newly allocated run on rollback.
        if candidate is not None and (
            (candidate.get("owner_kind") == "thread" and candidate.get("owner_id") == clean_thread_id)
            or (owned_agent_run_id and candidate.get("owner_kind") == "agent_run"
                and candidate.get("owner_id") == owned_agent_run_id)
        ):
            worktree = candidate

    recovery_workspace_id = ""
    if worktree is None:
        if pending_stats["unimported_retained"]:
            recovery_workspace_id = str(workspace_id or project_workspace_id or "")
            result["retained_sandbox"] = True
        result["changed"] += clear_thread_references(
            clean_thread_id,
            recovery_workspace_id=recovery_workspace_id,
        )
        return result

    worktree_workspace_id = str(worktree.get("worktree_workspace_id") or "")
    worktree_path = pathlib.Path(str(worktree.get("worktree_path") or ""))
    project_path = pathlib.Path(str(worktree.get("project_path") or ""))
    unimported = list_pending_changes(workspace_id=worktree_workspace_id)
    retained_sandbox = bool(unimported or pending_stats["unimported_retained"])
    summary = worktree_diff_summary(
        str(worktree.get("owner_kind") or "thread"),
        str(worktree.get("owner_id") or clean_thread_id),
    )
    dirty = bool(summary.get("dirty") or summary.get("status_lines"))
    unsafe_reason = ""
    try:
        managed_root = project_path.parent / ".row-bot-worktrees"
        safe_worktree_path = resolve_managed_path(managed_root, worktree_path)
    except ValueError as exc:
        safe_worktree_path = worktree_path
        unsafe_reason = str(exc)

    if not unsafe_reason and not safe_worktree_path.exists() and not retained_sandbox:
        result["changed"] += int(delete_workspace_record(worktree_workspace_id))
        result["changed"] += int(_delete_worktree_record(str(worktree.get("id") or "")))
        result["changed"] += clear_thread_references(clean_thread_id)
        return result

    if dirty or retained_sandbox or not bool(summary.get("ok")) or unsafe_reason:
        reason_parts = []
        if dirty:
            reason_parts.append("worktree has changes")
        if retained_sandbox:
            reason_parts.append("sandbox has unimported changes")
        if not bool(summary.get("ok")):
            reason_parts.append(str(summary.get("error") or "worktree status is unknown"))
        if unsafe_reason:
            reason_parts.append(unsafe_reason)
        reason = "; ".join(dict.fromkeys(reason_parts))
        mark_worktree_preserved(
            str(worktree.get("owner_kind") or "thread"),
            str(worktree.get("owner_id") or clean_thread_id),
            reason=reason,
        )
        recovery_workspace_id = worktree_workspace_id
        result["retained_worktree_path"] = str(worktree_path)
        result["retained_sandbox"] = retained_sandbox
        if dirty:
            result["warnings"].append("A Developer worktree with changes was kept.")
        if retained_sandbox:
            result["warnings"].append("A Developer sandbox with unimported changes was kept.")
        if unsafe_reason:
            result["warnings"].append("An unsafe Developer worktree path was kept.")
        result["changed"] += clear_thread_references(
            clean_thread_id,
            recovery_workspace_id=recovery_workspace_id,
        )
        return result

    try:
        if safe_worktree_path.exists():
            subprocess.run(
                ["git", "-C", str(project_path), "worktree", "remove", str(safe_worktree_path)],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        workspace = get_workspace(worktree_workspace_id) if worktree_workspace_id else None
        if workspace is not None and (
            workspace.execution_mode == "docker" or sandbox_shadow_path(workspace.id).exists()
        ):
            cleanup_workspace_sandbox(workspace.id)
        result["changed"] += int(delete_workspace_record(worktree_workspace_id))
        result["changed"] += int(_delete_worktree_record(str(worktree.get("id") or "")))
    except Exception as exc:
        mark_worktree_preserved(
            str(worktree.get("owner_kind") or "thread"),
            str(worktree.get("owner_id") or clean_thread_id),
            reason=f"automatic clean-worktree removal failed: {exc}",
        )
        recovery_workspace_id = worktree_workspace_id
        result["retained_worktree_path"] = str(worktree_path)
        result["warnings"].append("A Developer worktree could not be removed and was kept.")

    result["changed"] += clear_thread_references(
        clean_thread_id,
        recovery_workspace_id=recovery_workspace_id,
    )
    return result
