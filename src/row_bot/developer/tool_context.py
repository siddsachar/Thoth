from __future__ import annotations

import contextvars


_workspace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "developer_workspace_id",
    default="",
)
_thread_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "developer_thread_id",
    default="",
)


def set_context(*, workspace_id: str = "", thread_id: str = "") -> tuple[contextvars.Token, contextvars.Token]:
    workspace_token = _workspace_id_var.set(workspace_id or "")
    thread_token = _thread_id_var.set(thread_id or "")
    return workspace_token, thread_token


def reset_context(tokens: tuple[contextvars.Token, contextvars.Token]) -> None:
    workspace_token, thread_token = tokens
    _workspace_id_var.reset(workspace_token)
    _thread_id_var.reset(thread_token)


def get_workspace_id() -> str:
    from row_bot.conversation_resources import current_execution_context

    resources = current_execution_context()
    if resources is not None:
        binding = resources.resolve("workspace")
        if binding is None:
            return ""
        # Child allocation persists its actual worktree as a relationship.
        # A context inherited from a renderer is not an allocation proof.
        return binding.resource_id
    return _workspace_id_var.get()


def get_thread_id() -> str:
    thread_id = _thread_id_var.get()
    if thread_id:
        return thread_id
    try:
        from row_bot.agent import _current_thread_id_var

        return _current_thread_id_var.get()
    except Exception:
        return ""


def infer_workspace_id_from_thread(thread_id: str) -> str:
    if not thread_id:
        return ""
    try:
        from row_bot.threads import _get_thread_developer_workspace

        return _get_thread_developer_workspace(thread_id)
    except Exception:
        return ""
