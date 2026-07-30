from __future__ import annotations

import pytest

from row_bot.access.config import AccessConfig
from row_bot.access.request_context import RequestContextResolver, SessionIdentity
from row_bot.ui.access_context import require_ui_owner


def _context(*, mobile: bool = False):
    resolver = RequestContextResolver(
        AccessConfig.build(deployment_mode="server", allowed_hosts=("localhost",))
    )
    return resolver.resolve(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": b"mobile=1" if mobile else b"",
            "scheme": "http",
            "client": ("192.168.1.40", 51000),
            "headers": [(b"host", b"localhost:8080")],
        },
        session=SessionIdentity(
            device_id="owner-device",
            session_id="owner-session",
        ),
    )


def test_compact_and_desktop_sessions_have_the_same_owner_authority() -> None:
    desktop = _context()
    compact = _context(mobile=True)

    assert require_ui_owner(desktop) is desktop
    assert require_ui_owner(compact) is compact
    assert desktop.authentication_kind == compact.authentication_kind
    assert desktop.device_id == compact.device_id


def test_ui_owner_guard_rejects_unauthenticated_context() -> None:
    unauthenticated = RequestContextResolver(
        AccessConfig.build(deployment_mode="server", allowed_hosts=("localhost",))
    ).resolve(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": b"mobile=1",
            "scheme": "http",
            "client": ("192.168.1.40", 51000),
            "headers": [(b"host", b"localhost:8080")],
        }
    )

    with pytest.raises(PermissionError, match="authentication_required"):
        require_ui_owner(unauthenticated)
