from __future__ import annotations

from types import SimpleNamespace

from row_bot.access.config import AccessConfig
from row_bot.access.models import (
    AccessProfile,
    capabilities_for_profile,
)
from row_bot.access.request_context import (
    PresentationMode,
    RequestContextResolver,
    SessionIdentity,
)
from row_bot.ui.access_context import access_context_from_client
from row_bot.ui.mobile import is_mobile_client


def _scope(*, query: bytes = b"", peer: str = "192.168.1.25") -> dict:
    return {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": query,
        "scheme": "http",
        "client": (peer, 51000),
        "headers": [(b"host", b"localhost:8080")],
    }


def _session(profile: AccessProfile) -> SessionIdentity:
    return SessionIdentity(
        profile=profile.value,
        device_id=f"{profile.value}-device",
        session_id=f"{profile.value}-session",
        capabilities=frozenset(
            capability.value
            for capability in capabilities_for_profile(profile)
        ),
    )


def _client(context) -> SimpleNamespace:
    request = SimpleNamespace(
        state=SimpleNamespace(row_bot_access_context=context),
        query_params={},
    )
    return SimpleNamespace(request=request)


def test_remote_owner_keeps_full_authority_in_desktop_and_compact_layouts() -> None:
    resolver = RequestContextResolver(
        AccessConfig.build(deployment_mode="server", allowed_hosts=("localhost",))
    )
    desktop = resolver.resolve(_scope(), session=_session(AccessProfile.OWNER))
    compact = resolver.resolve(
        _scope(query=b"mobile=1"),
        session=_session(AccessProfile.OWNER),
    )

    assert desktop.profile == compact.profile == "owner"
    assert desktop.capabilities == compact.capabilities
    assert desktop.presentation is PresentationMode.DESKTOP
    assert compact.presentation is PresentationMode.COMPACT
    assert is_mobile_client(_client(desktop)) is False
    assert is_mobile_client(_client(compact)) is True


def test_companion_cannot_escape_compact_layout_with_query_or_viewport() -> None:
    resolver = RequestContextResolver(
        AccessConfig.build(deployment_mode="server", allowed_hosts=("localhost",))
    )
    companion = resolver.resolve(
        _scope(query=b"mobile=0"),
        session=_session(AccessProfile.COMPANION),
    )
    client = _client(companion)

    assert access_context_from_client(client) is companion
    assert companion.profile == "companion"
    assert companion.presentation is PresentationMode.COMPACT
    assert is_mobile_client(client) is True


def test_desktop_loopback_is_owner_but_server_loopback_is_not_implicit() -> None:
    desktop = RequestContextResolver(
        AccessConfig.build(deployment_mode="desktop", allowed_hosts=("localhost",))
    ).resolve(
        _scope(peer="127.0.0.1"),
        owner_capabilities=capabilities_for_profile(AccessProfile.OWNER),
    )
    server = RequestContextResolver(
        AccessConfig.build(deployment_mode="server", allowed_hosts=("localhost",))
    ).resolve(_scope(peer="127.0.0.1"))

    assert desktop.profile == "owner"
    assert desktop.presentation is PresentationMode.DESKTOP
    assert server.profile is None
    assert not server.authenticated
