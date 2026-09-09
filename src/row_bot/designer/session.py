"""Designer session state and shared mutation tracking."""

from __future__ import annotations

import logging

from row_bot.designer.history import UndoStack, snapshot
from row_bot.designer.state import DesignerProject

logger = logging.getLogger(__name__)

_active_projects_by_key: dict[str, DesignerProject] = {}
_undo_stacks_by_key: dict[str, UndoStack] = {}
_ui_active_key: str | None = None


def _project_key(project: DesignerProject) -> str:
    """Return the lookup key for a designer project."""

    if project.thread_id:
        return project.thread_id
    return f"project:{project.id}"


def _get_execution_key() -> str | None:
    """Return the current execution thread key when available."""

    from row_bot.conversation_resources import current_execution_context

    resources = current_execution_context()
    if resources is not None:
        binding = resources.resolve("artifact")
        if binding is not None:
            bind_project_to_thread(resources.conversation_id, binding.resource_id)
        else:
            # A captured empty binding set cannot fall back to a visible project
            # or a previously cached execution for this conversation.
            _active_projects_by_key.pop(resources.conversation_id, None)
        return resources.conversation_id

    try:
        from row_bot.agent import get_current_thread_id

        thread_id = get_current_thread_id()
    except Exception:
        return None
    if thread_id:
        if thread_id in _active_projects_by_key:
            return thread_id
        try:
            from row_bot.threads import _get_thread_project_id

            project_id = _get_thread_project_id(thread_id)
        except Exception:
            project_id = ""
        if project_id:
            # A background parent/child must resolve its own durable project.
            # Returning the thread key even when loading fails prevents a
            # fallback to an unrelated project currently visible in the UI.
            try:
                bind_project_to_thread(thread_id, project_id)
            except Exception:
                logger.warning(
                    "Could not restore Designer project %s for thread %s",
                    project_id,
                    thread_id,
                    exc_info=True,
                )
        # An Agent execution thread without a Designer binding must resolve to
        # no project. Falling back to the visible UI project would let normal
        # chat, channels, Goals, or workflows inspect/edit an unrelated design.
        return thread_id
    return None


def _clear_agent_cache() -> None:
    """Drop cached graphs after the visible designer binding changes."""

    try:
        from row_bot.agent import clear_agent_cache

        clear_agent_cache()
    except Exception:
        logger.debug("Failed to clear agent cache after designer session change", exc_info=True)


def set_active_project(project: DesignerProject | None) -> None:
    """Called by the UI when entering or leaving the designer editor."""

    global _ui_active_key
    prev_key = _ui_active_key
    next_key = _project_key(project) if project is not None else None

    if project is not None:
        _active_projects_by_key[next_key] = project
        _undo_stacks_by_key.setdefault(next_key, UndoStack())
        _ui_active_key = next_key
    else:
        _ui_active_key = None

    if prev_key != _ui_active_key:
        _clear_agent_cache()


def bind_project_to_thread(thread_id: str, project_id: str) -> DesignerProject:
    """Load and bind the exact durable Designer project to an execution thread."""

    thread_id = str(thread_id or "").strip()
    project_id = str(project_id or "").strip()
    if not thread_id or not project_id:
        raise ValueError("Designer thread and project ids are required.")
    existing = _active_projects_by_key.get(thread_id)
    if existing is not None and str(existing.id) == project_id:
        return existing
    from row_bot.designer.storage import load_project

    project = load_project(project_id)
    if project is None:
        raise RuntimeError(f"Designer project {project_id} no longer exists.")
    _active_projects_by_key[thread_id] = project
    _undo_stacks_by_key.setdefault(thread_id, UndoStack())
    return project


def clear_thread_session(thread_id: str) -> None:
    """Drop cached Designer and undo state owned by one conversation."""

    global _ui_active_key
    clean = str(thread_id or "").strip()
    if not clean:
        return
    _active_projects_by_key.pop(clean, None)
    _undo_stacks_by_key.pop(clean, None)
    if _ui_active_key == clean:
        _ui_active_key = None
        _clear_agent_cache()


def clear_project_session(project_id: str) -> None:
    """Drop every cached binding and undo stack for a deleted project."""

    global _ui_active_key
    clean = str(project_id or "").strip()
    if not clean:
        return
    keys = {
        key
        for key, project in _active_projects_by_key.items()
        if str(getattr(project, "id", "") or "") == clean
    }
    keys.add(f"project:{clean}")
    for key in keys:
        _active_projects_by_key.pop(key, None)
        _undo_stacks_by_key.pop(key, None)
    if _ui_active_key in keys:
        _ui_active_key = None
        _clear_agent_cache()


def get_ui_active_project() -> DesignerProject | None:
    """Return the project currently bound to the visible designer UI."""

    if _ui_active_key is None:
        return None
    return _active_projects_by_key.get(_ui_active_key)


def get_active_project() -> DesignerProject | None:
    """Return the active designer project for the current context."""

    execution_key = _get_execution_key()
    if execution_key is not None:
        return _active_projects_by_key.get(execution_key)
    return get_ui_active_project()


def get_undo_stack() -> UndoStack | None:
    """Return the undo stack for the current project context, if any."""

    execution_key = _get_execution_key()
    if execution_key is not None:
        return _undo_stacks_by_key.get(execution_key)
    if _ui_active_key is None:
        return None
    return _undo_stacks_by_key.get(_ui_active_key)


def prepare_project_mutation(project: DesignerProject, label: str = "",
                              *, author: str = "user") -> None:
    """Capture undo state and save a persistent snapshot before a mutation.

    ``author`` should be ``"agent"`` when the mutation originates from a
    designer-agent tool call; it defaults to ``"user"`` for UI actions.
    """

    stack = _undo_stacks_by_key.setdefault(_project_key(project), UndoStack())
    stack.push(project)
    try:
        snapshot(project, label=label, author=author)
    except Exception:
        logger.debug("Failed to save snapshot before mutation", exc_info=True)
