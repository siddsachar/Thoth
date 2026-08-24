"""Cross-subsystem conversation deletion and idle storage maintenance.

The public deletion helpers in this module are the product boundary for
conversation removal.  Low-level owners keep their own storage primitives;
this service coordinates them without introducing a separate cleanup UX.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import pathlib
import shutil
import sqlite3
import sys
import threading
import time
import uuid
from collections.abc import Iterable
from typing import Any


logger = logging.getLogger(__name__)

SQLITE_COMPACT_MIN_FREE_BYTES = 32 * 1024 * 1024
SQLITE_COMPACT_MIN_FREE_RATIO = 0.20
STALE_TEMP_FILE_AGE = timedelta(days=1)


@dataclass(frozen=True)
class ThreadDeletionResult:
    thread_id: str
    deleted: bool
    retained_worktree_path: str = ""
    retained_sandbox: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class BulkThreadDeletionResult:
    results: tuple[ThreadDeletionResult, ...]
    failures: tuple[tuple[str, str], ...] = ()

    @property
    def deleted(self) -> int:
        return sum(1 for result in self.results if result.deleted)

    @property
    def retained_worktrees(self) -> tuple[str, ...]:
        return tuple(
            result.retained_worktree_path
            for result in self.results
            if result.retained_worktree_path
        )

    @property
    def retained_sandboxes(self) -> int:
        return sum(1 for result in self.results if result.retained_sandbox)


_deletion_lock = threading.RLock()
_deleting_threads: dict[str, str] = {}
_maintenance_lock = threading.Lock()


def normalize_thread_id(thread_id: str) -> str:
    """Return a safe database identifier or raise ``ValueError``.

    Colons are valid because internal orchestration threads use them.  Path
    separators and control characters are rejected because thread ids also
    participate in managed filenames.
    """

    clean = str(thread_id or "").strip()
    if not clean:
        raise ValueError("Conversation id is required.")
    if len(clean) > 512:
        raise ValueError("Conversation id is too long.")
    if clean in {".", ".."} or "/" in clean or "\\" in clean:
        raise ValueError("Conversation id contains an unsafe path segment.")
    if any(ord(char) < 32 or ord(char) == 127 for char in clean):
        raise ValueError("Conversation id contains a control character.")
    return clean


def resolve_managed_path(
    root: str | pathlib.Path,
    candidate: str | pathlib.Path,
    *,
    allow_root: bool = False,
) -> pathlib.Path:
    """Resolve *candidate* and prove it is contained by *root*.

    This helper follows symlinks for the containment decision.  Destructive
    callers therefore cannot escape through ``..`` or a managed-root symlink.
    """

    managed_root = pathlib.Path(root).expanduser().resolve(strict=False)
    raw_candidate = pathlib.Path(candidate).expanduser()
    if not raw_candidate.is_absolute():
        raw_candidate = managed_root / raw_candidate
    resolved = raw_candidate.resolve(strict=False)
    try:
        resolved.relative_to(managed_root)
    except ValueError as exc:
        raise ValueError(
            f"Refusing to modify a path outside the managed root: {resolved}"
        ) from exc
    if not allow_root and resolved == managed_root:
        raise ValueError(f"Refusing to modify the managed root itself: {managed_root}")
    return resolved


def is_thread_deleting(thread_id: str | None) -> bool:
    clean = str(thread_id or "").strip()
    if not clean:
        return False
    with _deletion_lock:
        return clean in _deleting_threads


def allow_thread_recreation(thread_id: str | None) -> None:
    """End an in-process deletion guard for an explicit new conversation."""

    clean = str(thread_id or "").strip()
    if not clean:
        return
    with _deletion_lock:
        _deleting_threads.pop(clean, None)


def _mark_thread_deleting(thread_id: str) -> str:
    token = uuid.uuid4().hex
    with _deletion_lock:
        _deleting_threads[thread_id] = token
    return token


def _purge_thread_files(thread_id: str) -> int:
    from row_bot import threads

    removed = 0
    ui_names = (
        f"{thread_id}.media.json",
        f"{thread_id}.images.json",
        f"{thread_id}.draft.json",
    )
    for name in ui_names:
        path = resolve_managed_path(threads._THREAD_UI_DIR, name)  # noqa: SLF001
        if path.exists():
            path.unlink()
            removed += 1

    media_dir = resolve_managed_path(threads._MEDIA_DIR, thread_id)  # noqa: SLF001
    if media_dir.exists():
        if media_dir.is_dir():
            shutil.rmtree(media_dir)
        else:
            media_dir.unlink()
        removed += 1
    return removed


def _request_thread_cancellation(thread_id: str, *, deletion_token: str = "") -> bool:
    """Request every known producer to stop; return whether work was active."""

    active = False
    try:
        from row_bot.ui.state import _active_generations

        generation = _active_generations.get(thread_id)
        active = generation is not None
        if generation is not None:
            generation.deletion_token = deletion_token
        from row_bot.ui.streaming import request_generation_stop

        request_generation_stop(thread_id, reason="conversation deleted")
    except Exception:
        try:
            from row_bot.ui.state import _active_generations

            generation = _active_generations.get(thread_id)
            if generation is not None:
                active = True
                generation.stop_event.set()
        except Exception:
            pass

    try:
        from row_bot.tasks import get_running_tasks, stop_task

        active = active or thread_id in get_running_tasks()
        stop_task(thread_id)
    except Exception:
        pass

    try:
        from row_bot.agent_runs import TERMINAL_STATUSES, list_agent_runs, stop_agent_run

        for run in list_agent_runs(parent_thread_id=thread_id, limit=500):
            if str(run.get("status") or "") not in TERMINAL_STATUSES:
                active = True
                stop_agent_run(str(run.get("id") or ""))
    except Exception:
        logger.debug("Could not stop Agent work for %s", thread_id, exc_info=True)

    try:
        from row_bot.tools.shell_tool import clear_shell_history, get_session_manager

        get_session_manager().kill_session(thread_id)
        clear_shell_history(thread_id)
    except Exception:
        logger.debug("Could not clear shell state for %s", thread_id, exc_info=True)

    try:
        from row_bot.tools.browser_tool import clear_browser_history
        from row_bot.tools.browser_tool import get_session_manager as get_browser_session_manager

        get_browser_session_manager().kill_session(thread_id)
        clear_browser_history(thread_id)
    except Exception:
        logger.debug("Could not clear browser state for %s", thread_id, exc_info=True)

    try:
        from row_bot.computer_use.service import get_computer_use_service

        get_computer_use_service().close_for_thread(thread_id)
    except Exception:
        logger.debug("Could not clear Computer Use state for %s", thread_id, exc_info=True)

    return active


def _clear_in_memory_state(thread_id: str) -> None:
    app_module = sys.modules.get("row_bot.app")
    app_state = getattr(app_module, "state", None) if app_module else None
    if app_state is not None:
        try:
            app_state.invalidate_thread_cache(thread_id)
            app_state.context_usage_cache.pop(thread_id, None)
            app_state.context_policy_notice_keys.pop(thread_id, None)
        except Exception:
            logger.debug("Could not clear UI cache for %s", thread_id, exc_info=True)

    try:
        from row_bot.designer.session import clear_thread_session

        clear_thread_session(thread_id)
    except Exception:
        logger.debug("Could not clear Designer session for %s", thread_id, exc_info=True)

    try:
        from row_bot.developer.inspector_snapshot import clear_thread_snapshots

        clear_thread_snapshots(thread_id)
    except Exception:
        logger.debug("Could not clear Developer Inspector cache for %s", thread_id, exc_info=True)

    try:
        from row_bot.memory_extraction import set_active_thread

        set_active_thread(None, previous_id=thread_id)
    except Exception:
        pass


def _purge_owned_state(thread_id: str) -> tuple[int, tuple[str, ...]]:
    """Run idempotent database/file purges and return changes plus warnings."""

    from row_bot import threads

    changed = threads._purge_thread_rows(thread_id)  # noqa: SLF001
    warnings: list[str] = []
    try:
        from row_bot.tasks import cleanup_thread_state

        changed += sum(cleanup_thread_state(thread_id).values())
    except Exception as exc:
        warnings.append("Some workflow or notification state could not be removed.")
        logger.warning(
            "Task cleanup failed for thread %s: %s",
            thread_id,
            exc,
            exc_info=True,
        )
    try:
        from row_bot.agent_runs import cleanup_thread_agent_runs

        stats = cleanup_thread_agent_runs(thread_id)
        changed += int(stats.get("runs_deleted", 0))
        changed += int(stats.get("threads_deleted", 0))
    except Exception:
        warnings.append("Some Agent state could not be removed.")
        logger.warning("Agent cleanup failed for thread %s", thread_id, exc_info=True)
    try:
        changed += _purge_thread_files(thread_id)
    except Exception:
        warnings.append("Some conversation attachments could not be removed.")
        logger.warning("File cleanup failed for thread %s", thread_id, exc_info=True)
    try:
        from row_bot.skills_activation import delete_thread_activation_state

        delete_thread_activation_state(thread_id)
    except Exception:
        logger.debug("Skill activation cleanup failed for %s", thread_id, exc_info=True)
    return changed, tuple(warnings)


def finish_thread_deletion(thread_id: str, token: str | None = None) -> None:
    """Perform the late-writer purge and release an active deletion guard."""

    clean = str(thread_id or "").strip()
    if not clean:
        return
    with _deletion_lock:
        current = _deleting_threads.get(clean)
        if current is None or (token is not None and current != token):
            return
    try:
        _purge_owned_state(clean)
        _clear_in_memory_state(clean)
    finally:
        with _deletion_lock:
            if _deleting_threads.get(clean) == current:
                _deleting_threads.pop(clean, None)


def _thread_has_active_producer(thread_id: str) -> bool:
    try:
        from row_bot.ui.state import _active_generations

        if thread_id in _active_generations:
            return True
    except Exception:
        pass
    try:
        from row_bot.tasks import get_running_tasks

        if thread_id in get_running_tasks():
            return True
    except Exception:
        pass
    try:
        from row_bot.agent_runs import TERMINAL_STATUSES, list_agent_runs

        return any(
            str(run.get("status") or "") not in TERMINAL_STATUSES
            for run in list_agent_runs(parent_thread_id=thread_id, limit=500)
        )
    except Exception:
        return False


def _deferred_finish(thread_id: str, token: str) -> None:
    """Finalize only after every known checkpoint producer has stopped."""

    while True:
        with _deletion_lock:
            if _deleting_threads.get(thread_id) != token:
                return
        if not _thread_has_active_producer(thread_id):
            finish_thread_deletion(thread_id, token)
            return
        time.sleep(0.25)


def delete_thread(thread_id: str) -> ThreadDeletionResult:
    clean = normalize_thread_id(thread_id)
    from row_bot import threads

    context = threads._get_thread_cleanup_context(clean)  # noqa: SLF001
    token = _mark_thread_deleting(clean)
    active_work = False
    warnings: list[str] = []
    retained_worktree_path = ""
    retained_sandbox = False
    changed = 0
    try:
        active_work = _request_thread_cancellation(clean, deletion_token=token)
        first_changed, first_warnings = _purge_owned_state(clean)
        changed += first_changed
        warnings.extend(first_warnings)

        project_id = str(context.get("project_id") or "")
        if project_id:
            try:
                from row_bot.designer.storage import detach_thread

                changed += int(detach_thread(project_id, clean))
            except Exception:
                warnings.append("The design could not be detached from its deleted conversation.")
                logger.warning(
                    "Designer detach failed for project %s thread %s",
                    project_id,
                    clean,
                    exc_info=True,
                )

        try:
            from row_bot.developer.worktrees import cleanup_thread_developer_state

            developer_result = cleanup_thread_developer_state(
                clean,
                workspace_id=str(context.get("developer_workspace_id") or ""),
                project_workspace_id=str(context.get("project_workspace_id") or ""),
            )
            retained_worktree_path = str(
                developer_result.get("retained_worktree_path") or ""
            )
            retained_sandbox = bool(developer_result.get("retained_sandbox"))
            changed += int(developer_result.get("changed", 0))
            warnings.extend(str(item) for item in developer_result.get("warnings", ()) if item)
        except Exception:
            warnings.append("Some Developer state could not be reconciled.")
            logger.warning("Developer cleanup failed for thread %s", clean, exc_info=True)

        second_changed, second_warnings = _purge_owned_state(clean)
        changed += second_changed
        warnings.extend(second_warnings)
        _clear_in_memory_state(clean)
    except Exception:
        with _deletion_lock:
            if _deleting_threads.get(clean) == token:
                _deleting_threads.pop(clean, None)
        raise

    if active_work:
        threading.Thread(
            target=_deferred_finish,
            args=(clean, token),
            name=f"thread-delete-finalize-{clean[:24]}",
            daemon=True,
        ).start()
    else:
        finish_thread_deletion(clean, token)

    return ThreadDeletionResult(
        thread_id=clean,
        deleted=bool(context.get("exists") or changed),
        retained_worktree_path=retained_worktree_path,
        retained_sandbox=retained_sandbox,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def delete_threads(thread_ids: Iterable[str]) -> BulkThreadDeletionResult:
    results: list[ThreadDeletionResult] = []
    failures: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_thread_id in thread_ids:
        display_id = str(raw_thread_id or "")
        try:
            clean = normalize_thread_id(display_id)
            if clean in seen:
                continue
            seen.add(clean)
            results.append(delete_thread(clean))
        except Exception as exc:
            failures.append((display_id, str(exc)))
            logger.exception("Conversation deletion failed for %s", display_id)
    return BulkThreadDeletionResult(tuple(results), tuple(failures))


def _sqlite_space_stats(db_path: pathlib.Path) -> dict[str, int | float]:
    with sqlite3.connect(str(db_path), timeout=1.0) as connection:
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        freelist_count = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
    free_bytes = page_size * freelist_count
    total_bytes = page_size * page_count
    return {
        "page_size": page_size,
        "page_count": page_count,
        "freelist_count": freelist_count,
        "free_bytes": free_bytes,
        "free_ratio": (free_bytes / total_bytes) if total_bytes else 0.0,
    }


def compact_sqlite_database(
    db_path: str | pathlib.Path,
    *,
    min_free_bytes: int = SQLITE_COMPACT_MIN_FREE_BYTES,
    min_free_ratio: float = SQLITE_COMPACT_MIN_FREE_RATIO,
) -> dict[str, Any]:
    """Threshold and compact one SQLite database at most once."""

    path = pathlib.Path(db_path).expanduser().resolve(strict=False)
    result: dict[str, Any] = {"path": str(path), "compacted": False, "reason": "missing"}
    if not path.exists() or not path.is_file():
        return result
    stats = _sqlite_space_stats(path)
    result.update(stats)
    if int(stats["free_bytes"]) < int(min_free_bytes):
        result["reason"] = "below_free_bytes_threshold"
        return result
    if float(stats["free_ratio"]) < float(min_free_ratio):
        result["reason"] = "below_free_ratio_threshold"
        return result

    required_free = max(path.stat().st_size * 2, 64 * 1024 * 1024)
    if shutil.disk_usage(path.parent).free < required_free:
        result["reason"] = "insufficient_temporary_space"
        return result
    try:
        with sqlite3.connect(str(path), timeout=1.0, isolation_level=None) as connection:
            connection.execute("PRAGMA busy_timeout=1000")
            connection.execute("VACUUM")
        result["compacted"] = True
        result["reason"] = "compacted"
    except sqlite3.Error as exc:
        result["reason"] = "locked_or_failed"
        result["error"] = str(exc)
        logger.info("SQLite compaction deferred for %s: %s", path, exc)
    return result


def _thread_ids() -> set[str]:
    from row_bot.threads import _list_threads

    return {str(row[0]) for row in _list_threads(include_details=False)}


def _project_ids() -> set[str]:
    from row_bot.designer.storage import PROJECTS_DIR

    if not PROJECTS_DIR.exists():
        return set()
    # A malformed-but-present project is still an owner.  Keeping its history
    # and publication is safer than treating a load error as proof of orphaning.
    return {candidate.stem for candidate in PROJECTS_DIR.glob("*.json")}


def _remove_stale_temp_files(
    roots: Iterable[pathlib.Path],
    *,
    cutoff: datetime,
) -> int:
    removed = 0
    cutoff_ts = cutoff.timestamp()
    for root in roots:
        if not root.exists():
            continue
        for candidate in root.glob("*.tmp"):
            try:
                path = resolve_managed_path(root, candidate)
                if path.is_file() and path.stat().st_mtime < cutoff_ts:
                    path.unlink()
                    removed += 1
            except (OSError, ValueError):
                logger.debug("Skipping unsafe or busy temp file %s", candidate, exc_info=True)
    return removed


def sweep_orphaned_thread_artifacts(
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Remove only unambiguous Row-Bot-owned orphan artifacts."""

    from row_bot import threads
    from row_bot.designer.history import HISTORY_DIR
    from row_bot.designer.publish import PUBLISHED_DIR
    from row_bot.designer.storage import PROJECTS_DIR
    from row_bot.developer.sandbox_runtime import SANDBOX_ROOT
    from row_bot.developer.storage import DEVELOPER_DIR
    from row_bot.developer.todos import TODOS_DIR, _todo_path

    owners = _thread_ids()
    projects = _project_ids()
    stats = {
        "media_dirs": 0,
        "thread_ui_files": 0,
        "developer_todos": 0,
        "designer_history": 0,
        "designer_published": 0,
        "pending_changes": 0,
        "temp_files": 0,
    }

    if threads._MEDIA_DIR.exists():  # noqa: SLF001
        for child in list(threads._MEDIA_DIR.iterdir()):  # noqa: SLF001
            if child.name in owners:
                continue
            try:
                path = resolve_managed_path(threads._MEDIA_DIR, child)  # noqa: SLF001
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                stats["media_dirs"] += 1
            except (OSError, ValueError):
                logger.warning("Could not remove orphan media path %s", child, exc_info=True)

    suffixes = (".media.json", ".images.json", ".draft.json")
    if threads._THREAD_UI_DIR.exists():  # noqa: SLF001
        for candidate in list(threads._THREAD_UI_DIR.iterdir()):  # noqa: SLF001
            matched = next((suffix for suffix in suffixes if candidate.name.endswith(suffix)), "")
            if not matched or candidate.name[: -len(matched)] in owners:
                continue
            try:
                resolve_managed_path(threads._THREAD_UI_DIR, candidate).unlink()  # noqa: SLF001
                stats["thread_ui_files"] += 1
            except (OSError, ValueError):
                logger.warning("Could not remove orphan thread UI file %s", candidate, exc_info=True)

    if TODOS_DIR.exists():
        owned_todo_stems = {_todo_path(thread_id).stem for thread_id in owners}
        for candidate in list(TODOS_DIR.glob("*.json")):
            if candidate.stem in owned_todo_stems:
                continue
            try:
                resolve_managed_path(TODOS_DIR, candidate).unlink()
                stats["developer_todos"] += 1
            except (OSError, ValueError):
                logger.warning("Could not remove orphan Developer todo %s", candidate, exc_info=True)

    if HISTORY_DIR.exists():
        for child in list(HISTORY_DIR.iterdir()):
            if child.name in projects:
                continue
            try:
                path = resolve_managed_path(HISTORY_DIR, child)
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                stats["designer_history"] += 1
            except (OSError, ValueError):
                logger.warning("Could not remove orphan Designer history %s", child, exc_info=True)

    if PUBLISHED_DIR.exists():
        for candidate in list(PUBLISHED_DIR.glob("*.html")):
            if candidate.stem in projects:
                continue
            try:
                resolve_managed_path(PUBLISHED_DIR, candidate).unlink()
                stats["designer_published"] += 1
            except (OSError, ValueError):
                logger.warning("Could not remove orphan published design %s", candidate, exc_info=True)

    try:
        from row_bot.developer.sandbox_runtime import cleanup_orphaned_imported_changes

        stats["pending_changes"] = cleanup_orphaned_imported_changes(owners)
    except Exception:
        logger.debug("Could not clean imported sandbox orphans", exc_info=True)

    cutoff = (now or datetime.now()) - STALE_TEMP_FILE_AGE
    stats["temp_files"] = _remove_stale_temp_files(
        (
            PROJECTS_DIR,
            DEVELOPER_DIR,
            TODOS_DIR,
            SANDBOX_ROOT,
        ),
        cutoff=cutoff,
    )
    return stats


def run_idle_maintenance() -> dict[str, Any]:
    """Run checkpoint pruning, safe orphan repair, and thresholded compaction."""

    if not _maintenance_lock.acquire(blocking=False):
        return {"skipped": "already_running"}
    try:
        from row_bot import tasks, threads

        checkpoint_stats = threads.cleanup_old_checkpoints()
        orphan_stats = sweep_orphaned_thread_artifacts()
        compaction = []
        checkpointer_lock = getattr(threads.checkpointer, "lock", None)
        if checkpointer_lock is None:
            compaction.append(compact_sqlite_database(threads.DB_PATH))
        else:
            with checkpointer_lock:
                compaction.append(compact_sqlite_database(threads.DB_PATH))
        compaction.append(compact_sqlite_database(tasks._DB_PATH))  # noqa: SLF001
        logger.info(
            "Idle conversation maintenance checkpoints=%s orphans=%s compaction=%s",
            checkpoint_stats,
            orphan_stats,
            [item.get("reason") for item in compaction],
        )
        return {
            "checkpoints": checkpoint_stats,
            "orphans": orphan_stats,
            "compaction": compaction,
        }
    finally:
        _maintenance_lock.release()
