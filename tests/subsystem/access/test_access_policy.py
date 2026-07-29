from __future__ import annotations

import pytest

from row_bot.access.config import DeploymentMode
from row_bot.access.policy import (
    AccessPolicy,
    Capability,
    RouteKind,
    require_capability,
)
from row_bot.access.request_context import (
    AccessContext,
    AuthenticationKind,
    PresentationMode,
)


def _context(
    *,
    kind: AuthenticationKind,
    profile: str | None,
    capabilities: frozenset[str] = frozenset(),
    forwarded: tuple[str, ...] = (),
    direct_loopback: bool = False,
    deployment_mode: DeploymentMode = DeploymentMode.DESKTOP,
) -> AccessContext:
    return AccessContext(
        deployment_mode=deployment_mode,
        authentication_kind=kind,
        transport_peer="127.0.0.1",
        effective_client="127.0.0.1",
        forwarding_headers=forwarded,
        trusted_proxy=False,
        trusted_proxy_peer=None,
        proxy_chain=(),
        scheme="http",
        host="localhost:8080",
        origin="http://localhost:8080",
        profile=profile,
        capabilities=capabilities,
        presentation=PresentationMode.DESKTOP,
        direct_loopback=direct_loopback,
    )


def _scope(path: str, *, method: str = "GET", scope_type: str = "http") -> dict:
    return {
        "type": scope_type,
        "method": method,
        "path": path,
        "headers": [(b"accept", b"application/json")],
    }


def test_profile_capabilities_keep_companion_below_owner() -> None:
    policy = AccessPolicy()

    owner = policy.capabilities_for_profile("owner")
    companion = policy.capabilities_for_profile("companion")

    assert Capability.FULL_UI.value in owner
    assert Capability.ACCESS_ADMIN.value in owner
    assert Capability.DEVELOPER_STUDIO.value in owner
    assert Capability.COMPACT_UI.value in companion
    assert Capability.CHAT.value in companion
    assert Capability.ACCESS_ADMIN.value not in companion
    assert Capability.SETTINGS.value not in companion
    assert Capability.SHELL.value not in companion


def test_access_management_routes_require_owner_capability() -> None:
    policy = AccessPolicy()
    route = policy.classify(_scope("/api/access/devices/device/revoke", method="POST"))
    companion = _context(
        kind=AuthenticationKind.SESSION,
        profile="companion",
        capabilities=policy.companion_capabilities,
    )
    owner = _context(
        kind=AuthenticationKind.SESSION,
        profile="owner",
        capabilities=policy.owner_capabilities,
    )

    assert route.kind is RouteKind.OWNER
    assert route.require_same_origin is True
    assert policy.authorize(companion, route).status_code == 403
    assert policy.authorize(owner, route).allowed is True


def test_unpaired_browser_navigation_and_api_are_classified_separately() -> None:
    policy = AccessPolicy()
    browser_scope = _scope("/")
    browser_scope["headers"] = [(b"accept", b"text/html")]

    browser = policy.classify(browser_scope)
    api = policy.classify(_scope("/api/private"))

    assert browser.browser_navigation is True
    assert api.browser_navigation is False
    assert (
        policy.authorize(
            _context(kind=AuthenticationKind.UNAUTHENTICATED, profile=None), browser
        ).status_code
        == 401
    )
    assert (
        policy.authorize(
            _context(kind=AuthenticationKind.UNAUTHENTICATED, profile=None), api
        ).status_code
        == 401
    )


def test_websocket_requires_authentication_and_origin() -> None:
    policy = AccessPolicy()
    route = policy.classify(_scope("/_nicegui_ws/socket.io", scope_type="websocket"))

    assert route.kind is RouteKind.AUTHENTICATED
    assert route.require_same_origin is True
    assert not policy.authorize(
        _context(kind=AuthenticationKind.UNAUTHENTICATED, profile=None), route
    ).allowed


def test_launcher_operations_are_never_authorized_to_remote_sessions() -> None:
    policy = AccessPolicy()
    route = policy.classify(_scope("/api/launcher-shutdown", method="POST"))
    remote_owner = _context(
        kind=AuthenticationKind.SESSION,
        profile="owner",
        capabilities=policy.owner_capabilities,
    )
    direct_local = _context(
        kind=AuthenticationKind.LOCAL_OWNER,
        profile="owner",
        capabilities=policy.owner_capabilities,
        direct_loopback=True,
    )

    assert route.kind is RouteKind.LAUNCHER
    assert policy.authorize(remote_owner, route).status_code == 403
    assert policy.authorize(direct_local, route).allowed is True


def test_server_mode_direct_loopback_reaches_launcher_handler_boundary() -> None:
    policy = AccessPolicy()
    route = policy.classify(_scope("/api/launcher-ping"))
    direct_server = _context(
        kind=AuthenticationKind.UNAUTHENTICATED,
        profile=None,
        direct_loopback=True,
        deployment_mode=DeploymentMode.SERVER,
    )

    assert policy.authorize(direct_server, route).allowed is True


def test_forwarded_loopback_cannot_authorize_launcher_operation() -> None:
    policy = AccessPolicy()
    route = policy.classify(_scope("/api/launcher-restart", method="POST"))
    forged_local = _context(
        kind=AuthenticationKind.LOCAL_OWNER,
        profile="owner",
        capabilities=policy.owner_capabilities,
        forwarded=("x-forwarded-for",),
    )

    assert policy.authorize(forged_local, route).reason == "launcher_local_only"


def test_webhook_preserves_route_owned_secret_semantics() -> None:
    policy = AccessPolicy()
    route = policy.classify(_scope("/api/webhook/task-id", method="POST"))

    assert route.kind is RouteKind.DELEGATED
    assert policy.authorize(
        _context(kind=AuthenticationKind.UNAUTHENTICATED, profile=None), route
    ).allowed


def test_require_capability_guards_server_side_handlers() -> None:
    companion = _context(
        kind=AuthenticationKind.SESSION,
        profile="companion",
        capabilities=AccessPolicy.companion_capabilities,
    )

    require_capability(companion, Capability.CHAT)
    with pytest.raises(PermissionError, match="capability_required"):
        require_capability(companion, Capability.ACCESS_ADMIN)
