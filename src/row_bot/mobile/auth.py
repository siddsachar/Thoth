"""Compatibility helpers for the former mobile pairing API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import uuid

from row_bot.access.models import SessionLifetime
from row_bot.access.service import AccessService
from row_bot.access.store import InvitationClaimError, normalize_datetime, parse_iso, utc_now
from row_bot.access.tokens import (
    LEGACY_DEVICE_TOKEN_PREFIX,
    SESSION_TOKEN_PREFIX,
    SecretHash,
    hash_secret,
    issue_invitation_token,
    parse_invitation_token,
    parse_legacy_device_token,
    parse_session_token,
    verify_secret,
)
from row_bot.mobile.store import MobileAuthStore, MobileDevice, PairingCode

TOKEN_HASH_ITERATIONS = 200_000
PAIRING_CODE_TTL = timedelta(minutes=10)
PAIRING_FAILURE_LIMIT = 5
PAIRING_LOCK_DURATION = timedelta(minutes=5)
DEVICE_TOKEN_PREFIX = SESSION_TOKEN_PREFIX
PAIRING_CODE_PREFIX = "rbp"


@dataclass(frozen=True)
class PairingTicket:
    id: str
    code: str
    expires_at: str
    intended_origin: str | None
    access_mode: str | None

    def pairing_url(self, origin: str | None = None) -> str:
        base = (origin or self.intended_origin or "").rstrip("/")
        path = f"/mobile/pair?code={self.code}"
        return f"{base}{path}" if base else path


@dataclass(frozen=True)
class PairingConfirmation:
    device: MobileDevice
    token: str


class PairingError(ValueError):
    """Raised when a legacy pairing code cannot be confirmed."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _now(value: datetime | None = None) -> datetime:
    return normalize_datetime(value)


def parse_pairing_code(code: str) -> tuple[str, str] | None:
    text = str(code or "").strip()
    if text.startswith(f"{PAIRING_CODE_PREFIX}_"):
        text = f"rbi_{text[len(PAIRING_CODE_PREFIX) + 1:]}"
    return parse_invitation_token(text)


def parse_device_token(token: str) -> tuple[str, str] | None:
    return parse_session_token(token) or parse_legacy_device_token(token)


def _to_access_invitation_token(code: str) -> str:
    text = str(code or "").strip()
    if text.startswith(f"{PAIRING_CODE_PREFIX}_"):
        return f"rbi_{text[len(PAIRING_CODE_PREFIX) + 1:]}"
    return text


def create_pairing_ticket(
    store: MobileAuthStore,
    *,
    intended_origin: str | None = None,
    access_mode: str | None = None,
    ttl: timedelta = PAIRING_CODE_TTL,
    now: datetime | None = None,
) -> PairingTicket:
    """Create an owner invitation while preserving the old ``rbp`` URL."""
    if ttl > PAIRING_CODE_TTL:
        raise ValueError("pairing lifetime cannot exceed 10 minutes")
    current = _now(now)
    invitation_id = uuid.uuid4().hex
    issued = issue_invitation_token(invitation_id)
    invitation = store.access_store.create_invitation_record(
        invitation_id=invitation_id,
        secret_hash=issued.secret_hash,
        secret_salt=issued.secret_salt,
        session_lifetime=SessionLifetime.TRUSTED,
        intended_origin=(str(intended_origin).rstrip("/") if intended_origin else "http://localhost"),
        expires_at=current + ttl,
        access_route=access_mode,
        now=current,
    )
    compatibility_code = (
        f"{PAIRING_CODE_PREFIX}_{issued.token.split('_', 1)[1]}"
    )
    return PairingTicket(
        id=invitation.id,
        code=compatibility_code,
        expires_at=invitation.expires_at.isoformat(),
        intended_origin=intended_origin,
        access_mode=invitation.access_route,
    )


def _pairing_is_expired(pairing: PairingCode, now: datetime) -> bool:
    expires_at = parse_iso(pairing.expires_at)
    return expires_at is None or expires_at <= now


def _pairing_is_locked(pairing: PairingCode, now: datetime) -> bool:
    locked_until = parse_iso(pairing.locked_until)
    return locked_until is not None and locked_until > now


def confirm_pairing(
    store: MobileAuthStore,
    *,
    code: str,
    display_name: str,
    user_agent: str | None = None,
    paired_from: str | None = None,
    access_mode: str | None = None,
    now: datetime | None = None,
) -> PairingConfirmation:
    """Atomically claim an owner invitation and create a session."""
    parsed = parse_pairing_code(code)
    if parsed is None:
        raise PairingError("invalid_code")
    invitation_id, _secret = parsed
    invitation = store.access_store.get_invitation(invitation_id)
    if invitation is None:
        raise PairingError("invalid_code")
    try:
        claim = AccessService(store.access_store).claim_invitation(
            _to_access_invitation_token(code),
            intended_origin=invitation.intended_origin,
            display_name=display_name,
            user_agent=user_agent,
            effective_client=paired_from,
            access_route=access_mode,
            now=now,
        )
    except InvitationClaimError as exc:
        reason = "invalid_code" if exc.reason == "invalid_invitation" else exc.reason
        raise PairingError(reason) from exc
    device = store.get_device(claim.device.id)
    assert device is not None
    return PairingConfirmation(device=device, token=claim.session_token)


def validate_device_token(
    store: MobileAuthStore,
    token: str,
    *,
    now: datetime | None = None,
    touch: bool = True,
) -> MobileDevice | None:
    """Validate a new owner session or a migrated legacy-mobile ``rbd`` token."""
    service = AccessService(store.access_store)
    authenticated = service.validate_session(token, now=now, touch=touch)
    if authenticated is None:
        authenticated = service.validate_legacy_session(token, now=now, touch=touch)
    if authenticated is None:
        return None
    return store.get_device(authenticated.device.id)


__all__ = [
    "DEVICE_TOKEN_PREFIX",
    "LEGACY_DEVICE_TOKEN_PREFIX",
    "PAIRING_CODE_PREFIX",
    "PAIRING_CODE_TTL",
    "PAIRING_FAILURE_LIMIT",
    "PAIRING_LOCK_DURATION",
    "PairingConfirmation",
    "PairingError",
    "PairingTicket",
    "SecretHash",
    "confirm_pairing",
    "create_pairing_ticket",
    "hash_secret",
    "parse_device_token",
    "parse_pairing_code",
    "utc_now",
    "validate_device_token",
    "verify_secret",
]
