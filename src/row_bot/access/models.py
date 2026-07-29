"""Typed records and fixed capability presets for Row-Bot access."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class AccessProfile(StrEnum):
    """Fixed authorization profiles for the single-owner application."""

    OWNER = "owner"
    COMPANION = "companion"


class AccessCapability(StrEnum):
    """Capabilities enforced by the central access policy."""

    FULL_UI = "full_ui"
    COMPACT_UI = "compact_ui"
    CHAT = "chat"
    ATTACHMENTS = "attachments"
    WORKFLOWS_VIEW = "workflows_view"
    WORKFLOWS_ACTION = "workflows_action"
    APPROVALS = "approvals"
    STATUS_VIEW = "status_view"
    COMPANION_PREFERENCES = "companion_preferences"
    SETTINGS = "settings"
    ACCESS_ADMIN = "access_admin"
    PROVIDER_ADMIN = "provider_admin"
    CHANNEL_ADMIN = "channel_admin"
    PLUGIN_ADMIN = "plugin_admin"
    MCP_ADMIN = "mcp_admin"
    DEVELOPER_STUDIO = "developer_studio"
    DESIGNER_STUDIO = "designer_studio"
    SHELL = "shell"
    TERMINAL = "terminal"
    TOOLS = "tools"
    SCHEDULES = "schedules"


OWNER_CAPABILITIES = frozenset(AccessCapability)
COMPANION_CAPABILITIES = frozenset(
    {
        AccessCapability.COMPACT_UI,
        AccessCapability.CHAT,
        AccessCapability.ATTACHMENTS,
        AccessCapability.WORKFLOWS_VIEW,
        AccessCapability.WORKFLOWS_ACTION,
        AccessCapability.APPROVALS,
        AccessCapability.STATUS_VIEW,
        AccessCapability.COMPANION_PREFERENCES,
    }
)


def capabilities_for_profile(
    profile: AccessProfile | str,
) -> frozenset[AccessCapability]:
    """Return the immutable capability preset for a profile."""
    normalized = AccessProfile(profile)
    if normalized is AccessProfile.OWNER:
        return OWNER_CAPABILITIES
    return COMPANION_CAPABILITIES


class SessionLifetime(StrEnum):
    """Named session lifetimes exposed by invitation creation."""

    TRUSTED = "trusted"
    TEMPORARY = "temporary"
    MIGRATED = "migrated"


class TokenFormat(StrEnum):
    """Supported verifier formats stored in the access database."""

    SESSION_V1 = "rbs_v1"
    INVITATION_V1 = "rbi_v1"
    LEGACY_RBD = "legacy_rbd"


@dataclass(frozen=True)
class AccessDevice:
    id: str
    display_name: str
    profile: AccessProfile
    created_at: datetime
    last_seen_at: datetime | None
    revoked_at: datetime | None
    user_agent: str | None
    paired_from: str | None
    access_route: str | None
    legacy_source_id: str | None

    @property
    def capabilities(self) -> frozenset[AccessCapability]:
        return capabilities_for_profile(self.profile)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "profile": self.profile.value,
            "created_at": self.created_at.isoformat(),
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "user_agent": self.user_agent,
            "paired_from": self.paired_from,
            "access_route": self.access_route,
        }


@dataclass(frozen=True)
class AccessSession:
    id: str
    device_id: str
    token_hash: str
    token_salt: str
    token_format: TokenFormat
    created_at: datetime
    last_seen_at: datetime | None
    expires_at: datetime
    revoked_at: datetime | None
    lifetime: SessionLifetime
    replaced_by_session_id: str | None

    def is_active(self, now: datetime) -> bool:
        return self.revoked_at is None and self.expires_at > now

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "device_id": self.device_id,
            "created_at": self.created_at.isoformat(),
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "expires_at": self.expires_at.isoformat(),
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "lifetime": self.lifetime.value,
        }


@dataclass(frozen=True)
class AccessInvitation:
    id: str
    secret_hash: str
    secret_salt: str
    token_format: TokenFormat
    profile: AccessProfile
    session_lifetime: SessionLifetime
    intended_origin: str
    created_at: datetime
    expires_at: datetime
    claimed_at: datetime | None
    cancelled_at: datetime | None
    created_by: str | None
    failed_attempts: int
    locked_until: datetime | None
    access_route: str | None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "profile": self.profile.value,
            "session_lifetime": self.session_lifetime.value,
            "intended_origin": self.intended_origin,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "claimed_at": self.claimed_at.isoformat() if self.claimed_at else None,
            "cancelled_at": self.cancelled_at.isoformat() if self.cancelled_at else None,
            "failed_attempts": self.failed_attempts,
            "locked_until": self.locked_until.isoformat() if self.locked_until else None,
            "access_route": self.access_route,
        }


@dataclass(frozen=True)
class AccessEvent:
    id: str
    event_type: str
    device_id: str | None
    session_id: str | None
    invitation_id: str | None
    effective_client: str | None
    user_agent: str | None
    created_at: datetime
    detail: dict[str, Any]

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "event_type": self.event_type,
            "device_id": self.device_id,
            "session_id": self.session_id,
            "invitation_id": self.invitation_id,
            "effective_client": self.effective_client,
            "user_agent": self.user_agent,
            "created_at": self.created_at.isoformat(),
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class AuthenticatedSession:
    """A validated session together with its active device."""

    device: AccessDevice
    session: AccessSession

    @property
    def profile(self) -> AccessProfile:
        return self.device.profile

    @property
    def capabilities(self) -> frozenset[AccessCapability]:
        return self.device.capabilities
