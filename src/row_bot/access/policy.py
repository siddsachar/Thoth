"""Transport-neutral route authorization policy for the single owner."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping
from row_bot.access.request_context import (
    ACCESS_CONTEXT_SCOPE_KEY,
    AccessContext,
)


class RouteKind(StrEnum):
    PUBLIC = "public"
    AUTHENTICATED = "authenticated"
    DELEGATED = "delegated"
    LAUNCHER = "launcher"


@dataclass(frozen=True, slots=True)
class RouteClassification:
    kind: RouteKind
    browser_navigation: bool = False
    require_same_origin: bool = False


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    allowed: bool
    status_code: int
    reason: str


PUBLIC_HTTP_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/connect"),
        ("POST", "/api/access/invitations/claim"),
        ("GET", "/api/access/session"),
        ("GET", "/healthz"),
        ("GET", "/readyz"),
        # Legacy mobile/PWA compatibility. None of these routes grants a session.
        ("GET", "/mobile/pair"),
        ("POST", "/api/mobile/pair/confirm"),
        ("GET", "/api/mobile/session"),
        ("GET", "/mobile/manifest.webmanifest"),
        ("GET", "/mobile/offline"),
        ("GET", "/mobile/service-worker.js"),
        ("GET", "/static/row_bot_glyph_256.png"),
    }
)
LAUNCHER_ONLY_PATHS = frozenset(
    {
        "/api/launcher-ping",
        "/api/launcher-shutdown",
        "/api/launcher-restart",
        "/api/startup-state",
    }
)
AUTHENTICATED_ROUTE_PREFIXES: tuple[str, ...] = (
    "/api/access/status",
    "/api/access/invitations",
    "/api/access/devices",
    "/api/access/routes",
    "/api/access/diagnostics",
    "/api/mobile/pair/start",
    "/api/mobile/devices",
    "/api/mobile/access-events",
    "/api/providers",
    "/api/channels",
    "/api/plugins",
    "/api/mcp",
    "/api/developer",
    "/api/designer",
    "/api/shell",
    "/api/terminal",
    "/api/voice/local",
    "/api/voice/realtime/client-secret",
)


def _method(scope: Mapping[str, object]) -> str:
    return str(scope.get("method") or "GET").upper()


def _path(scope: Mapping[str, object]) -> str:
    path = str(scope.get("path") or "/")
    return path if path.startswith("/") else "/"


def _header_values(scope: Mapping[str, object], name: bytes) -> tuple[bytes, ...]:
    return tuple(
        bytes(value)
        for raw_name, value in scope.get("headers", []) or []
        if bytes(raw_name).lower() == name
    )


def is_browser_navigation(scope: Mapping[str, object]) -> bool:
    if scope.get("type") != "http" or _method(scope) != "GET":
        return False
    path = _path(scope)
    if path.startswith(("/api/", "/_nicegui/", "/_media/", "/published/")):
        return False
    accept = b",".join(_header_values(scope, b"accept")).decode(
        "latin-1", errors="ignore"
    )
    return "text/html" in accept.lower() or path == "/"


def is_safe_method(method: object) -> bool:
    return str(method or "").upper() in {"GET", "HEAD", "OPTIONS", "TRACE"}


class AccessPolicy:
    """Central route policy shared by HTTP and WebSockets."""

    def classify(self, scope: Mapping[str, object]) -> RouteClassification:
        scope_type = str(scope.get("type") or "")
        path = _path(scope)
        method = _method(scope)
        if scope_type == "websocket":
            return RouteClassification(
                RouteKind.AUTHENTICATED,
                browser_navigation=False,
                require_same_origin=True,
            )
        if scope_type != "http":
            return RouteClassification(RouteKind.PUBLIC)
        if path in LAUNCHER_ONLY_PATHS or path.startswith("/api/launcher/"):
            return RouteClassification(RouteKind.LAUNCHER)
        if path.startswith("/api/webhook/"):
            # Webhook task secrets remain authoritative at the route itself.
            return RouteClassification(RouteKind.DELEGATED)
        if (method, path) in PUBLIC_HTTP_ROUTES:
            return RouteClassification(
                RouteKind.PUBLIC,
                require_same_origin=(method == "POST"),
            )
        for prefix in AUTHENTICATED_ROUTE_PREFIXES:
            if path == prefix or path.startswith(f"{prefix}/"):
                return RouteClassification(
                    RouteKind.AUTHENTICATED,
                    require_same_origin=not is_safe_method(method),
                )
        return RouteClassification(
            RouteKind.AUTHENTICATED,
            browser_navigation=is_browser_navigation(scope),
            require_same_origin=not is_safe_method(method),
        )

    def authorize(
        self,
        context: AccessContext,
        classification: RouteClassification,
    ) -> AuthorizationDecision:
        if classification.kind in {RouteKind.PUBLIC, RouteKind.DELEGATED}:
            return AuthorizationDecision(True, 200, "allowed")
        if classification.kind is RouteKind.LAUNCHER:
            if context.direct_loopback and not context.forwarding_headers:
                return AuthorizationDecision(True, 200, "local_launcher")
            return AuthorizationDecision(False, 403, "launcher_local_only")
        if not context.authenticated:
            return AuthorizationDecision(False, 401, "authentication_required")
        return AuthorizationDecision(True, 200, "allowed")


def context_from_scope(scope: Mapping[str, object]) -> AccessContext | None:
    value = scope.get(ACCESS_CONTEXT_SCOPE_KEY)
    return value if isinstance(value, AccessContext) else None


def require_authenticated_owner(
    context_or_scope: AccessContext | Mapping[str, object],
) -> AccessContext:
    """Return the authenticated owner context or raise ``PermissionError``."""

    context = (
        context_or_scope
        if isinstance(context_or_scope, AccessContext)
        else context_from_scope(context_or_scope)
    )
    if context is None or not context.authenticated:
        raise PermissionError("authentication_required")
    return context
