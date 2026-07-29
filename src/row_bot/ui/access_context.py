"""UI-facing helpers for the centrally resolved access context."""

from __future__ import annotations

from typing import Any

from row_bot.access.models import AccessCapability
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


def is_owner_context(context: AccessContext | None = None) -> bool:
    selected = context or current_access_context()
    return selected is not None and selected.profile == "owner"


def is_companion_context(context: AccessContext | None = None) -> bool:
    selected = context or current_access_context()
    return selected is not None and selected.profile == "companion"


def uses_compact_presentation(context: AccessContext | None = None) -> bool:
    selected = context or current_access_context()
    return (
        selected is not None
        and selected.presentation is PresentationMode.COMPACT
    )


def has_capability(
    capability: AccessCapability | str,
    context: AccessContext | None = None,
) -> bool:
    selected = context or current_access_context()
    return selected is not None and selected.has_capability(capability)


def require_ui_capability(
    capability: AccessCapability | str,
    context: AccessContext | None = None,
) -> AccessContext:
    """Return the context or fail before a privileged UI handler runs."""
    selected = context or current_access_context()
    if selected is None or not selected.authenticated:
        raise PermissionError("authentication_required")
    if not selected.has_capability(capability):
        raise PermissionError("capability_required")
    return selected
