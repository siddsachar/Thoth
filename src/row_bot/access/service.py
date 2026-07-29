"""High-level invitation, device, and session operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit
import uuid

from row_bot.access.models import (
    AccessDevice,
    AccessInvitation,
    AccessProfile,
    AccessSession,
    AuthenticatedSession,
    SessionLifetime,
    TokenFormat,
)
from row_bot.access.store import (
    AccessStore,
    InvitationClaimError,
    normalize_datetime,
)
from row_bot.access.tokens import (
    issue_invitation_token,
    issue_session_token,
    parse_invitation_token,
    parse_legacy_device_token,
    parse_session_token,
    verify_secret,
)

INVITATION_TTL = timedelta(minutes=10)
TRUSTED_SESSION_TTL = timedelta(days=30)
TEMPORARY_SESSION_TTL = timedelta(hours=12)


@dataclass(frozen=True)
class CreatedInvitation:
    invitation: AccessInvitation
    token: str

    def invitation_url(self, origin: str | None = None) -> str:
        selected_origin = canonicalize_origin(origin or self.invitation.intended_origin)
        return f"{selected_origin}/connect?invitation={self.token}"


@dataclass(frozen=True)
class InvitationInspection:
    invitation: AccessInvitation
    status: str


@dataclass(frozen=True)
class InvitationClaim:
    """Successful claim result; ``session_token`` is returned only once."""

    invitation: AccessInvitation
    device: AccessDevice
    session: AccessSession
    session_token: str

    @property
    def profile(self) -> AccessProfile:
        return self.invitation.profile

    @property
    def session_lifetime(self) -> SessionLifetime:
        return self.invitation.session_lifetime

    @property
    def intended_origin(self) -> str:
        return self.invitation.intended_origin


class AccessService:
    """Security-sensitive access operations over an :class:`AccessStore`."""

    def __init__(self, store: AccessStore | None = None) -> None:
        self.store = store or AccessStore()

    @property
    def instance_id(self) -> str:
        return self.store.instance_id

    def create_invitation(
        self,
        *,
        profile: AccessProfile | str,
        intended_origin: str,
        session_lifetime: SessionLifetime | str = SessionLifetime.TRUSTED,
        ttl: timedelta = INVITATION_TTL,
        created_by: str | None = None,
        access_route: str | None = None,
        now: datetime | None = None,
    ) -> CreatedInvitation:
        current = normalize_datetime(now)
        normalized_profile = AccessProfile(profile)
        normalized_lifetime = SessionLifetime(session_lifetime)
        if normalized_lifetime is SessionLifetime.MIGRATED:
            raise ValueError("migrated is reserved for legacy compatibility sessions")
        if ttl <= timedelta(0) or ttl > INVITATION_TTL:
            raise ValueError(
                "invitation lifetime must be positive and at most 10 minutes"
            )
        origin = canonicalize_origin(intended_origin)
        invitation_id = uuid.uuid4().hex
        issued = issue_invitation_token(invitation_id)
        invitation = self.store.create_invitation_record(
            invitation_id=invitation_id,
            secret_hash=issued.secret_hash,
            secret_salt=issued.secret_salt,
            profile=normalized_profile,
            session_lifetime=normalized_lifetime,
            intended_origin=origin,
            expires_at=current + ttl,
            created_by=created_by,
            access_route=access_route,
            now=current,
        )
        self.store.log_event(
            "invitation_created",
            invitation_id=invitation.id,
            detail={
                "profile": invitation.profile.value,
                "session_lifetime": invitation.session_lifetime.value,
            },
            now=current,
        )
        return CreatedInvitation(invitation=invitation, token=issued.token)

    def inspect_invitation(
        self,
        token: str,
        *,
        effective_client: str | None = None,
        now: datetime | None = None,
    ) -> InvitationInspection:
        """Inspect invitation state without claiming or changing it."""
        invitation, secret = self._load_token_invitation(token)
        if not verify_secret(
            secret,
            salt=invitation.secret_salt,
            expected_hash=invitation.secret_hash,
        ):
            raise InvitationClaimError("invalid_invitation")
        current = normalize_datetime(now)
        if invitation.claimed_at is not None:
            status = "already_claimed"
        elif invitation.cancelled_at is not None:
            status = "cancelled"
        elif invitation.expires_at <= current:
            status = "expired"
        elif (
            invitation.locked_until is not None and invitation.locked_until > current
        ) or self.store.invitation_failure_locked(
            invitation.id,
            effective_client=effective_client,
            now=current,
        ):
            status = "locked"
        else:
            status = "available"
        return InvitationInspection(invitation=invitation, status=status)

    def claim_invitation(
        self,
        token: str,
        *,
        intended_origin: str,
        display_name: str,
        user_agent: str | None = None,
        effective_client: str | None = None,
        access_route: str | None = None,
        profile: AccessProfile | str | None = None,
        session_lifetime: SessionLifetime | str | None = None,
        now: datetime | None = None,
    ) -> InvitationClaim:
        current = normalize_datetime(now)
        parsed = parse_invitation_token(token)
        if parsed is None:
            raise InvitationClaimError("invalid_invitation")
        invitation_id, invitation_secret = parsed
        invitation = self.store.get_invitation(invitation_id)
        if invitation is None:
            raise InvitationClaimError("invalid_invitation")
        requested_profile = AccessProfile(profile) if profile is not None else None
        requested_lifetime = (
            SessionLifetime(session_lifetime) if session_lifetime is not None else None
        )

        device_id = uuid.uuid4().hex
        session_id = uuid.uuid4().hex
        issued_session = issue_session_token(session_id)
        session_ttl = session_ttl_for(invitation.session_lifetime)
        device = AccessDevice(
            id=device_id,
            display_name=(str(display_name or "").strip() or "Connected device")[:80],
            profile=invitation.profile,
            created_at=current,
            last_seen_at=current,
            revoked_at=None,
            user_agent=(str(user_agent)[:256] if user_agent else None),
            paired_from=(str(effective_client)[:128] if effective_client else None),
            access_route=(
                str(access_route)[:80] if access_route else invitation.access_route
            ),
            legacy_source_id=None,
        )
        session = AccessSession(
            id=session_id,
            device_id=device_id,
            token_hash=issued_session.secret_hash,
            token_salt=issued_session.secret_salt,
            token_format=TokenFormat.SESSION_V1,
            created_at=current,
            last_seen_at=current,
            expires_at=current + session_ttl,
            revoked_at=None,
            lifetime=invitation.session_lifetime,
            replaced_by_session_id=None,
        )
        device, session, claimed_invitation = self.store.claim_invitation_atomic(
            invitation_id=invitation_id,
            invitation_secret=invitation_secret,
            expected_origin=canonicalize_origin(intended_origin),
            device=device,
            session=session,
            requested_profile=requested_profile,
            requested_lifetime=requested_lifetime,
            effective_client=effective_client,
            user_agent=user_agent,
            now=current,
        )
        return InvitationClaim(
            invitation=claimed_invitation,
            device=device,
            session=session,
            session_token=issued_session.token,
        )

    def validate_session(
        self,
        token: str,
        *,
        now: datetime | None = None,
        touch: bool = True,
    ) -> AuthenticatedSession | None:
        parsed = parse_session_token(token)
        if parsed is None:
            return None
        session_id, secret = parsed
        current = normalize_datetime(now)
        session = self.store.get_session(session_id)
        if (
            session is None
            or session.token_format is not TokenFormat.SESSION_V1
            or not session.is_active(current)
            or not verify_secret(
                secret,
                salt=session.token_salt,
                expected_hash=session.token_hash,
            )
        ):
            return None
        device = self.store.get_device(session.device_id)
        if device is None or device.revoked_at is not None:
            return None
        if touch:
            self.store.touch_session(session.id, now=current)
            session = self.store.get_session(session.id) or session
            device = self.store.get_device(device.id) or device
        return AuthenticatedSession(device=device, session=session)

    def validate_legacy_session(
        self,
        token: str,
        *,
        now: datetime | None = None,
        touch: bool = True,
    ) -> AuthenticatedSession | None:
        """Validate only an explicitly migrated companion ``rbd`` session."""
        parsed = parse_legacy_device_token(token)
        if parsed is None:
            return None
        legacy_device_id, secret = parsed
        current = normalize_datetime(now)
        session = self.store.get_legacy_session_for_device(legacy_device_id)
        if (
            session is None
            or session.token_format is not TokenFormat.LEGACY_RBD
            or not session.is_active(current)
            or not verify_secret(
                secret,
                salt=session.token_salt,
                expected_hash=session.token_hash,
            )
        ):
            return None
        device = self.store.get_device(session.device_id)
        if (
            device is None
            or device.profile is not AccessProfile.COMPANION
            or device.legacy_source_id is None
            or device.revoked_at is not None
        ):
            return None
        if touch:
            self.store.touch_session(session.id, now=current)
            session = self.store.get_session(session.id) or session
            device = self.store.get_device(device.id) or device
        return AuthenticatedSession(device=device, session=session)

    def list_devices(self, *, include_revoked: bool = True) -> list[AccessDevice]:
        return self.store.list_devices(include_revoked=include_revoked)

    def list_sessions(
        self,
        *,
        device_id: str | None = None,
        include_revoked: bool = True,
    ) -> list[AccessSession]:
        return self.store.list_sessions(
            device_id=device_id,
            include_revoked=include_revoked,
        )

    def list_invitations(self, *, limit: int = 100) -> list[AccessInvitation]:
        return self.store.list_invitations(limit=limit)

    def cancel_invitation(
        self,
        invitation_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        return self.store.cancel_invitation(invitation_id, now=now)

    def revoke_device(
        self,
        device_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        return self.store.revoke_device(device_id, now=now)

    def revoke_session(
        self,
        session_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        return self.store.revoke_session(session_id, now=now)

    def revoke_all(
        self,
        *,
        except_session_id: str | None = None,
        now: datetime | None = None,
    ) -> int:
        return self.store.revoke_all(
            except_session_id=except_session_id,
            now=now,
        )

    def prune(
        self,
        *,
        now: datetime | None = None,
        retention: timedelta = timedelta(days=30),
        max_events: int = 2000,
    ) -> dict[str, int]:
        return self.store.prune(
            now=now,
            retention=retention,
            max_events=max_events,
        )

    def _load_token_invitation(self, token: str) -> tuple[AccessInvitation, str]:
        parsed = parse_invitation_token(token)
        if parsed is None:
            raise InvitationClaimError("invalid_invitation")
        invitation_id, secret = parsed
        invitation = self.store.get_invitation(invitation_id)
        if invitation is None:
            raise InvitationClaimError("invalid_invitation")
        return invitation, secret


def session_ttl_for(lifetime: SessionLifetime | str) -> timedelta:
    normalized = SessionLifetime(lifetime)
    if normalized is SessionLifetime.TRUSTED:
        return TRUSTED_SESSION_TTL
    if normalized is SessionLifetime.TEMPORARY:
        return TEMPORARY_SESSION_TTL
    raise ValueError("migrated session expiry is established during migration")


def canonicalize_origin(origin: str) -> str:
    """Validate and normalize an exact HTTP(S) origin."""
    text = str(origin or "").strip()
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid intended origin") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("intended origin must use http or https")
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("intended origin must contain only scheme, host, and port")
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("invalid intended origin host") from exc
    if ":" in host:
        host = f"[{host}]"
    scheme = parsed.scheme.lower()
    default_port = 80 if scheme == "http" else 443
    authority = host if port in {None, default_port} else f"{host}:{port}"
    return f"{scheme}://{authority}"


__all__ = [
    "AccessService",
    "CreatedInvitation",
    "InvitationClaim",
    "InvitationClaimError",
    "InvitationInspection",
    "canonicalize_origin",
    "session_ttl_for",
]
