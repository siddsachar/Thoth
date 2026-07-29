from __future__ import annotations

from pathlib import Path

import pytest

from row_bot.access.config import AccessConfig
from row_bot.access.models import (
    AccessCapability,
    AccessProfile,
    capabilities_for_profile,
)
from row_bot.access.request_context import RequestContextResolver, SessionIdentity
from row_bot.ui.access_context import require_ui_capability


def _context(profile: AccessProfile):
    resolver = RequestContextResolver(
        AccessConfig.build(deployment_mode="server", allowed_hosts=("localhost",))
    )
    capabilities = capabilities_for_profile(profile)
    return resolver.resolve(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": b"",
            "scheme": "http",
            "client": ("192.168.1.40", 51000),
            "headers": [(b"host", b"localhost:8080")],
        },
        session=SessionIdentity(
            profile=profile.value,
            device_id=f"{profile.value}-device",
            session_id=f"{profile.value}-session",
            capabilities=frozenset(item.value for item in capabilities),
        ),
    )


def test_companion_profile_has_curated_capabilities_only() -> None:
    companion = capabilities_for_profile(AccessProfile.COMPANION)

    assert AccessCapability.CHAT in companion
    assert AccessCapability.ATTACHMENTS in companion
    assert AccessCapability.WORKFLOWS_VIEW in companion
    assert AccessCapability.APPROVALS in companion
    for forbidden in (
        AccessCapability.SETTINGS,
        AccessCapability.ACCESS_ADMIN,
        AccessCapability.PROVIDER_ADMIN,
        AccessCapability.PLUGIN_ADMIN,
        AccessCapability.MCP_ADMIN,
        AccessCapability.DEVELOPER_STUDIO,
        AccessCapability.DESIGNER_STUDIO,
        AccessCapability.SHELL,
        AccessCapability.TERMINAL,
    ):
        assert forbidden not in companion


def test_server_side_ui_capability_guard_rejects_companion() -> None:
    companion = _context(AccessProfile.COMPANION)
    owner = _context(AccessProfile.OWNER)

    with pytest.raises(PermissionError, match="capability_required"):
        require_ui_capability(AccessCapability.SETTINGS, companion)
    assert require_ui_capability(AccessCapability.SETTINGS, owner) is owner


def test_companion_settings_surface_contains_no_access_admin_actions() -> None:
    mobile_source = Path("src/row_bot/ui/mobile.py").read_text(encoding="utf-8")
    settings_body = mobile_source.split("def _build_settings(", 1)[1].split(
        "def _build_desktop_only_notice(", 1
    )[0]
    chat_source = Path("src/row_bot/ui/mobile_chat.py").read_text(encoding="utf-8")

    assert "create_pairing_ticket" not in settings_body
    assert "detect_tailscale" not in settings_body
    assert "revoke_device" not in settings_body
    assert "Companion sessions always use the restricted compact surface." in settings_body
    assert "owner_controls = has_capability(AccessCapability.SETTINGS)" in chat_source
    assert "if not owner_controls:" in chat_source
