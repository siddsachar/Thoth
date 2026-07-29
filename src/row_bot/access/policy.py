"""Profile capabilities and transport-neutral route authorization policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable, Mapping

from row_bot.access.models import (
    AccessCapability,
    AccessProfile,
    COMPANION_CAPABILITIES,
    OWNER_CAPABILITIES,
    capabilities_for_profile,
)
from row_bot.access.request_context import (
    ACCESS_CONTEXT_SCOPE_KEY,
    AccessContext,
)


# Compatibility name for route-policy callers.  The canonical vocabulary and
# presets live in access.models.
Capability = AccessCapability


class RouteKind(StrEnum):
    PUBLIC = "public"
    AUTHENTICATED = "authenticated"
    OWNER = "owner"
    DELEGATED = "delegated"
    LAUNCHER = "launcher"


@dataclass(frozen=True, slots=True)
class RouteClassification:
    kind: RouteKind
    required_capability: str | None = None
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
        # Companion/PWA compatibility.  None of these routes grants a session.
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
OWNER_ROUTE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("/api/access/status", Capability.ACCESS_ADMIN.value),
    ("/api/access/invitations", Capability.ACCESS_ADMIN.value),
    ("/api/access/devices", Capability.ACCESS_ADMIN.value),
    ("/api/access/routes", Capability.ACCESS_ADMIN.value),
    ("/api/access/diagnostics", Capability.ACCESS_ADMIN.value),
    ("/api/mobile/pair/start", Capability.ACCESS_ADMIN.value),
    ("/api/mobile/devices", Capability.ACCESS_ADMIN.value),
    ("/api/mobile/access-events", Capability.ACCESS_ADMIN.value),
    ("/api/providers", Capability.PROVIDER_ADMIN.value),
    ("/api/channels", Capability.CHANNEL_ADMIN.value),
    ("/api/plugins", Capability.PLUGIN_ADMIN.value),
    ("/api/mcp", Capability.MCP_ADMIN.value),
    ("/api/developer", Capability.DEVELOPER_STUDIO.value),
    ("/api/designer", Capability.DESIGNER_STUDIO.value),
    ("/api/shell", Capability.SHELL.value),
    ("/api/terminal", Capability.TERMINAL.value),
    ("/api/voice/local", Capability.CHAT.value),
    ("/api/voice/realtime/client-secret", Capability.TOOLS.value),
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
    """Central route and capability policy shared by HTTP and WebSockets."""

    owner_capabilities = OWNER_CAPABILITIES
    companion_capabilities = COMPANION_CAPABILITIES

    def capabilities_for_profile(
        self, profile: AccessProfile | str
    ) -> frozenset[AccessCapability]:
        value = str(getattr(profile, "value", profile) or "").strip().lower()
        if value == "computer":
            value = AccessProfile.OWNER.value
        try:
            return capabilities_for_profile(AccessProfile(value))
        except ValueError:
            return frozenset()

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
        for prefix, capability in OWNER_ROUTE_PREFIXES:
            if path == prefix or path.startswith(f"{prefix}/"):
                return RouteClassification(
                    RouteKind.OWNER,
                    required_capability=capability,
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
        if classification.required_capability and not context.has_capability(
            classification.required_capability
        ):
            return AuthorizationDecision(False, 403, "capability_required")
        return AuthorizationDecision(True, 200, "allowed")


def context_from_scope(scope: Mapping[str, object]) -> AccessContext | None:
    value = scope.get(ACCESS_CONTEXT_SCOPE_KEY)
    return value if isinstance(value, AccessContext) else None


def require_capability(
    context_or_scope: AccessContext | Mapping[str, object],
    capability: Capability | str,
) -> None:
    """Raise ``PermissionError`` for unauthorized server-side handlers."""

    context = (
        context_or_scope
        if isinstance(context_or_scope, AccessContext)
        else context_from_scope(context_or_scope)
    )
    value = str(getattr(capability, "value", capability))
    if context is None or not context.authenticated:
        raise PermissionError("authentication_required")
    if not context.has_capability(value):
        raise PermissionError("capability_required")


def normalized_capabilities(values: Iterable[object]) -> frozenset[str]:
    return frozenset(str(getattr(value, "value", value)) for value in values)
