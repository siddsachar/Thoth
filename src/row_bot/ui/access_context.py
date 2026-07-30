"""UI-facing helpers for the centrally resolved access context."""

from __future__ import annotations

from typing import Any

from row_bot.access.request_context import AccessContext, PresentationMode


def access_context_from_request(request: Any) -> AccessContext | None:
    """Return the middleware-provided context without re-parsing request data."""
    state = getattr(request, "state", None)
    context = getattr(state, "row_bot_access_context", None)
    return context if isinstance(context, AccessContext) else None


def access_context_from_client(client: Any) -> AccessContext | None:
    """Return the immutable context attached to a NiceGUI client request."""
    return access_context_from_request(getattr(client, "request", None))


def current_access_context() -> AccessContext | None:
    """Resolve the active NiceGUI request context, if one exists."""
    try:
        from nicegui import ui

        return access_context_from_client(ui.context.client)
    except Exception:
        return None


def is_authenticated_owner(context: AccessContext | None = None) -> bool:
    selected = context or current_access_context()
    return selected is not None and selected.authenticated


def uses_compact_presentation(context: AccessContext | None = None) -> bool:
    selected = context or current_access_context()
    return (
        selected is not None
        and selected.presentation is PresentationMode.COMPACT
    )


def require_ui_owner(
    context: AccessContext | None = None,
) -> AccessContext:
    """Return the authenticated owner context before a UI handler runs."""
    selected = context or current_access_context()
    if selected is None or not selected.authenticated:
        raise PermissionError("authentication_required")
    return selected
