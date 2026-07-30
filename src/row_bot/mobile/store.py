"""Compatibility adapters for the generalized Row-Bot access store."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
import uuid

from row_bot.access.models import (
    AccessDevice,
    AccessEvent,
    AccessSession,
    SessionLifetime,
    TokenFormat,
)
from row_bot.access.store import AccessStore, normalize_datetime, parse_iso, to_iso, utc_now


@dataclass(frozen=True)
class MobileDevice:
    """Legacy mobile-facing view of an access device and one session."""

    id: str
    display_name: str
    token_hash: str
    token_salt: str
    created_at: str
    last_seen_at: str | None
    revoked_at: str | None
    user_agent: str | None
    paired_from: str | None
    access_mode: str | None
    scopes: tuple[str, ...]

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
            "revoked_at": self.revoked_at,
            "user_agent": self.user_agent,
            "paired_from": self.paired_from,
            "access_mode": self.access_mode,
            "scopes": list(self.scopes),
        }


@dataclass(frozen=True)
class PairingCode:
    id: str
    code_hash: str
    code_salt: str
    created_at: str
    expires_at: str
    claimed_at: str | None
    intended_origin: str | None
    access_mode: str | None
    failed_attempts: int
    locked_until: str | None


@dataclass(frozen=True)
class MobileAccessEvent:
    id: str
    device_id: str | None
    event_type: str
    ip: str | None
    user_agent: str | None
    created_at: str
    detail: dict[str, Any]

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "device_id": self.device_id,
            "event_type": self.event_type,
            "ip": self.ip,
            "user_agent": self.user_agent,
            "created_at": self.created_at,
            "detail": self.detail,
        }


class MobileAuthStore:
    """Legacy call surface backed by :class:`row_bot.access.store.AccessStore`."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.access_store = AccessStore(db_path)
        self.db_path = self.access_store.db_path

    def ensure_schema(self) -> None:
        self.access_store.ensure_schema()

    def create_pairing_code(
        self,
        *,
        code_hash: str,
        code_salt: str,
        expires_at: datetime,
        intended_origin: str | None = None,
        access_mode: str | None = None,
        code_id: str | None = None,
        now: datetime | None = None,
    ) -> PairingCode:
        invitation = self.access_store.create_invitation_record(
            invitation_id=code_id or uuid.uuid4().hex,
            secret_hash=code_hash,
            secret_salt=code_salt,
            session_lifetime=SessionLifetime.TRUSTED,
            intended_origin=intended_origin or "http://localhost",
            expires_at=expires_at,
            access_route=access_mode,
            now=now,
        )
        return _pairing_code(invitation)

    def get_pairing_code(self, code_id: str) -> PairingCode | None:
        invitation = self.access_store.get_invitation(code_id)
        return _pairing_code(invitation) if invitation else None

    def record_pairing_failure(
        self,
        code_id: str,
        *,
        locked_until: datetime | None = None,
    ) -> PairingCode | None:
        invitation = self.access_store.record_invitation_failure(code_id)
        if invitation is not None and locked_until is not None:
            with self.access_store._immediate_transaction() as connection:
                connection.execute(
                    "UPDATE access_invitations SET locked_until = ? WHERE id = ?",
                    (to_iso(locked_until), code_id),
                )
            invitation = self.access_store.get_invitation(code_id)
        return _pairing_code(invitation) if invitation else None

    def mark_pairing_claimed(
        self,
        code_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        self.ensure_schema()
        with self.access_store._immediate_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE access_invitations
                   SET claimed_at = ?
                 WHERE id = ?
                   AND claimed_at IS NULL
                   AND cancelled_at IS NULL
                """,
                (to_iso(now), code_id),
            )
            return cursor.rowcount == 1

    def clear_expired_pairing_codes(self, *, now: datetime | None = None) -> int:
        self.ensure_schema()
        with self.access_store._immediate_transaction() as connection:
            cursor = connection.execute(
                """
                DELETE FROM access_invitations
                 WHERE expires_at <= ?
                   AND claimed_at IS NULL
                """,
                (to_iso(now),),
            )
            return int(cursor.rowcount or 0)

    def create_device(
        self,
        *,
        display_name: str,
        token_hash: str,
        token_salt: str,
        user_agent: str | None = None,
        paired_from: str | None = None,
        access_mode: str | None = None,
        scopes: tuple[str, ...] | list[str] | None = None,
        device_id: str | None = None,
        now: datetime | None = None,
    ) -> MobileDevice:
        del scopes  # Legacy scopes are metadata, never an entitlement boundary.
        current = normalize_datetime(now)
        row_id = device_id or uuid.uuid4().hex
        device = self.access_store.create_device_record(
            AccessDevice(
                id=row_id,
                display_name=(str(display_name or "").strip() or "Mobile device")[:80],
                created_at=current,
                last_seen_at=None,
                revoked_at=None,
                user_agent=user_agent,
                paired_from=paired_from,
                access_route=access_mode,
                legacy_source_id=None,
            )
        )
        session = self.access_store.create_session_record(
            AccessSession(
                id=row_id,
                device_id=row_id,
                token_hash=token_hash,
                token_salt=token_salt,
                token_format=TokenFormat.SESSION_V1,
                created_at=current,
                last_seen_at=None,
                expires_at=current + timedelta(days=30),
                revoked_at=None,
                lifetime=SessionLifetime.TRUSTED,
                replaced_by_session_id=None,
            )
        )
        return _mobile_device(device, session)

    def get_device(self, device_id: str) -> MobileDevice | None:
        device = self.access_store.get_device(device_id)
        if device is None:
            session = self.access_store.get_session(device_id)
            if session is not None:
                device = self.access_store.get_device(session.device_id)
        if device is None:
            return None
        sessions = self.access_store.list_sessions(device_id=device.id)
        return _mobile_device(device, sessions[0] if sessions else None)

    def list_devices(self, *, include_revoked: bool = True) -> list[MobileDevice]:
        devices = self.access_store.list_devices(include_revoked=include_revoked)
        return [
            _mobile_device(
                device,
                next(iter(self.access_store.list_sessions(device_id=device.id)), None),
            )
            for device in devices
        ]

    def touch_device(self, device_id: str, *, now: datetime | None = None) -> None:
        for session in self.access_store.list_sessions(
            device_id=device_id,
            include_revoked=False,
        ):
            self.access_store.touch_session(session.id, now=now)

    def revoke_device(self, device_id: str, *, now: datetime | None = None) -> bool:
        return self.access_store.revoke_device(device_id, now=now)

    def log_event(
        self,
        event_type: str,
        *,
        device_id: str | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
        detail: dict[str, Any] | None = None,
        now: datetime | None = None,
        event_id: str | None = None,
    ) -> MobileAccessEvent:
        event = self.access_store.log_event(
            event_type,
            device_id=device_id,
            effective_client=ip,
            user_agent=user_agent,
            detail=detail,
            now=now,
            event_id=event_id,
        )
        return _mobile_event(event)

    def get_event(self, event_id: str) -> MobileAccessEvent | None:
        event = self.access_store.get_event(event_id)
        return _mobile_event(event) if event else None

    def recent_events(self, *, limit: int = 50) -> list[MobileAccessEvent]:
        return [
            _mobile_event(event)
            for event in self.access_store.recent_events(limit=limit)
        ]

    def set_kv(
        self,
        key: str,
        value: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> None:
        self.access_store.set_meta(f"mobile_kv:{key}", value, now=now)

    def get_kv(
        self,
        key: str,
        default: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        value = self.access_store.get_meta(f"mobile_kv:{key}", default or {})
        return dict(value) if isinstance(value, dict) else dict(default or {})


def _mobile_device(
    device: AccessDevice,
    session: AccessSession | None,
) -> MobileDevice:
    return MobileDevice(
        id=device.id,
        display_name=device.display_name,
        token_hash=session.token_hash if session else "",
        token_salt=session.token_salt if session else "",
        created_at=to_iso(device.created_at),
        last_seen_at=to_iso(device.last_seen_at) if device.last_seen_at else None,
        revoked_at=to_iso(device.revoked_at) if device.revoked_at else None,
        user_agent=device.user_agent,
        paired_from=device.paired_from,
        access_mode=device.access_route,
        scopes=(),
    )


def _pairing_code(invitation) -> PairingCode:
    return PairingCode(
        id=invitation.id,
        code_hash=invitation.secret_hash,
        code_salt=invitation.secret_salt,
        created_at=to_iso(invitation.created_at),
        expires_at=to_iso(invitation.expires_at),
        claimed_at=to_iso(invitation.claimed_at) if invitation.claimed_at else None,
        intended_origin=invitation.intended_origin,
        access_mode=invitation.access_route,
        failed_attempts=invitation.failed_attempts,
        locked_until=to_iso(invitation.locked_until) if invitation.locked_until else None,
    )


def _mobile_event(event: AccessEvent) -> MobileAccessEvent:
    return MobileAccessEvent(
        id=event.id,
        device_id=event.device_id,
        event_type=event.event_type,
        ip=event.effective_client,
        user_agent=event.user_agent,
        created_at=to_iso(event.created_at),
        detail=dict(event.detail),
    )


__all__ = [
    "MobileAccessEvent",
    "MobileAuthStore",
    "MobileDevice",
    "PairingCode",
    "parse_iso",
    "to_iso",
    "utc_now",
]
