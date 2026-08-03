from __future__ import annotations

from types import SimpleNamespace

from row_bot.access.config import AccessConfig
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


def _session(name: str = "browser") -> SessionIdentity:
    return SessionIdentity(
        device_id=f"{name}-device",
        session_id=f"{name}-session",
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
    desktop = resolver.resolve(_scope(), session=_session())
    compact = resolver.resolve(
        _scope(query=b"mobile=1"),
        session=_session(),
    )

    assert desktop.authentication_kind == compact.authentication_kind
    assert desktop.device_id == compact.device_id
    assert desktop.presentation is PresentationMode.DESKTOP
    assert compact.presentation is PresentationMode.COMPACT
    assert is_mobile_client(_client(desktop)) is False
    assert is_mobile_client(_client(compact)) is True


def test_authenticated_owner_can_switch_back_to_desktop_layout() -> None:
    resolver = RequestContextResolver(
        AccessConfig.build(deployment_mode="server", allowed_hosts=("localhost",))
    )
    desktop = resolver.resolve(
        _scope(query=b"mobile=0"),
        session=_session("phone"),
    )
    client = _client(desktop)

    assert access_context_from_client(client) is desktop
    assert desktop.authenticated
    assert desktop.presentation is PresentationMode.DESKTOP
    assert is_mobile_client(client) is False


def test_desktop_loopback_is_owner_but_server_loopback_is_not_implicit() -> None:
    desktop = RequestContextResolver(
        AccessConfig.build(deployment_mode="desktop", allowed_hosts=("localhost",))
    ).resolve(
        _scope(peer="127.0.0.1"),
    )
    server = RequestContextResolver(
        AccessConfig.build(deployment_mode="server", allowed_hosts=("localhost",))
    ).resolve(_scope(peer="127.0.0.1"))

    assert desktop.authenticated
    assert desktop.presentation is PresentationMode.DESKTOP
    assert not server.authenticated
