"""Versioned SQLite persistence for devices, sessions, and invitations."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from typing import Any
import uuid

from row_bot.access.models import (
    AccessDevice,
    AccessEvent,
    AccessInvitation,
    AccessSession,
    SessionLifetime,
    TokenFormat,
)
from row_bot.access.tokens import verify_secret
from row_bot.data_paths import get_access_db_path

SCHEMA_VERSION = 3
LEGACY_MIGRATION_GRACE = timedelta(days=30)
RECOVERY_COPY_MAX_BYTES = 128 * 1024 * 1024
CLAIM_FAILURE_LIMIT = 5
CLAIM_LOCK_DURATION = timedelta(minutes=5)
MAX_EVENT_DETAIL_BYTES = 2048
MAX_EVENT_ROWS = 2000

_SENSITIVE_DETAIL_KEYS = {
    "authorization",
    "code",
    "cookie",
    "credential",
    "invitation",
    "secret",
    "session_token",
    "token",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_datetime(value: datetime | None = None) -> datetime:
    result = value or utc_now()
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def to_iso(value: datetime | None = None) -> str:
    return normalize_datetime(value).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return normalize_datetime(parsed)


def _claim_client_key(effective_client: str | None) -> str:
    """Return a non-reversible key for one effective claim client."""

    normalized = str(effective_client or "unidentified")[:128]
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _row_value(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    return row[key] if key in row.keys() else default


class AccessStoreError(RuntimeError):
    """Base class for durable access-store failures."""


class InvitationClaimError(AccessStoreError):
    """Raised when an invitation cannot be claimed."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AccessStore:
    """Versioned access store using the existing physical ``mobile.db``."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        migration_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else get_access_db_path()
        self._migration_hook = migration_hook

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _immediate_transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def ensure_schema(self) -> None:
        """Create or migrate the schema under a cross-process write lock."""
        with self._immediate_transaction() as connection:
            current_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            if current_version > SCHEMA_VERSION:
                raise AccessStoreError(
                    f"access database schema {current_version} is newer than supported "
                    f"version {SCHEMA_VERSION}"
                )
            if current_version == SCHEMA_VERSION and self._required_tables_exist(
                connection
            ):
                return
            if current_version in {1, 2}:
                self._make_recovery_copy()
                self._run_migration_hook("after_backup")
                self._create_schema(connection)
                self._run_migration_hook("after_schema")
                self._migrate_single_owner(connection)
                self._run_migration_hook("after_semantic_normalization")
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                self._run_migration_hook("before_commit")
                return
            if current_version != 0:
                raise AccessStoreError(
                    f"unsupported access database schema {current_version}"
                )

            legacy_exists = self._table_exists(connection, "mobile_devices")
            if legacy_exists:
                self._make_recovery_copy()
            self._run_migration_hook("after_backup")
            self._create_schema(connection)
            self._run_migration_hook("after_schema")
            self._migrate_legacy(connection)
            self._run_migration_hook("after_legacy")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._run_migration_hook("before_commit")

    def _run_migration_hook(self, phase: str) -> None:
        if self._migration_hook is not None:
            self._migration_hook(phase)

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        return row is not None

    def _required_tables_exist(self, connection: sqlite3.Connection) -> bool:
        return all(
            self._table_exists(connection, table)
            for table in (
                "access_meta",
                "access_devices",
                "access_sessions",
                "access_invitations",
                "access_invitation_failures",
                "access_events",
            )
        )

    def recovery_copy_paths(self) -> tuple[Path, ...]:
        """Return existing pre-migration recovery files for diagnostics/tests."""
        return tuple(
            destination
            for _source, destination in self._recovery_copy_candidates()
            if destination.exists()
        )

    def _recovery_copy_candidates(self) -> tuple[tuple[Path, Path], ...]:
        sources = (
            self.db_path,
            Path(f"{self.db_path}-wal"),
            Path(f"{self.db_path}-shm"),
            Path(f"{self.db_path}-journal"),
        )
        return tuple(
            (source, source.with_name(f"{source.name}.pre-access-v{SCHEMA_VERSION}"))
            for source in sources
        )

    def _make_recovery_copy(self) -> None:
        candidates = [
            (source, destination)
            for source, destination in self._recovery_copy_candidates()
            if source.exists()
        ]
        total_size = sum(source.stat().st_size for source, _destination in candidates)
        if total_size > RECOVERY_COPY_MAX_BYTES:
            raise AccessStoreError(
                "legacy access database exceeds recovery-copy safety limit"
            )
        for source, destination in candidates:
            if not destination.exists():
                shutil.copy2(source, destination)

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS access_meta(
              key TEXT PRIMARY KEY,
              value_json TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS access_devices(
              id TEXT PRIMARY KEY,
              display_name TEXT NOT NULL,
              profile TEXT NOT NULL CHECK(profile IN ('owner', 'companion')),
              created_at TEXT NOT NULL,
              last_seen_at TEXT,
              revoked_at TEXT,
              user_agent TEXT,
              paired_from TEXT,
              access_route TEXT,
              legacy_source_id TEXT UNIQUE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS access_sessions(
              id TEXT PRIMARY KEY,
              device_id TEXT NOT NULL REFERENCES access_devices(id),
              token_hash TEXT NOT NULL,
              token_salt TEXT NOT NULL,
              token_format TEXT NOT NULL,
              created_at TEXT NOT NULL,
              last_seen_at TEXT,
              expires_at TEXT NOT NULL,
              revoked_at TEXT,
              lifetime TEXT NOT NULL,
              replaced_by_session_id TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS access_invitations(
              id TEXT PRIMARY KEY,
              secret_hash TEXT NOT NULL,
              secret_salt TEXT NOT NULL,
              token_format TEXT NOT NULL,
              profile TEXT NOT NULL CHECK(profile IN ('owner', 'companion')),
              session_lifetime TEXT NOT NULL,
              intended_origin TEXT NOT NULL,
              created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              claimed_at TEXT,
              cancelled_at TEXT,
              created_by TEXT,
              failed_attempts INTEGER NOT NULL DEFAULT 0,
              locked_until TEXT,
              access_route TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS access_invitation_failures(
              invitation_id TEXT NOT NULL REFERENCES access_invitations(id),
              client_key TEXT NOT NULL,
              failed_attempts INTEGER NOT NULL DEFAULT 0,
              locked_until TEXT,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(invitation_id, client_key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS access_events(
              id TEXT PRIMARY KEY,
              event_type TEXT NOT NULL,
              device_id TEXT,
              session_id TEXT,
              invitation_id TEXT,
              effective_client TEXT,
              user_agent TEXT,
              created_at TEXT NOT NULL,
              detail_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_access_devices_revoked
              ON access_devices(revoked_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_access_sessions_device
              ON access_sessions(device_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_access_sessions_expiry
              ON access_sessions(expires_at, revoked_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_access_invitations_expiry
              ON access_invitations(expires_at, claimed_at, cancelled_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_access_invitation_failures_expiry
              ON access_invitation_failures(locked_until, updated_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_access_events_created
              ON access_events(created_at)
            """,
        )
        for statement in statements:
            connection.execute(statement)

    def _migrate_legacy(self, connection: sqlite3.Connection) -> None:
        migration_time = utc_now()
        marker = connection.execute(
            "SELECT value_json FROM access_meta WHERE key = 'legacy_mobile_migrated'"
        ).fetchone()
        if marker is not None:
            self._ensure_instance_id(connection, migration_time)
            return

        migrated_count = 0
        if self._table_exists(connection, "mobile_devices"):
            legacy_rows = connection.execute("SELECT * FROM mobile_devices").fetchall()
            for row in legacy_rows:
                source_id = str(row["id"])
                device_id = source_id if len(source_id) <= 128 else uuid.uuid4().hex
                created_at = str(
                    _row_value(row, "created_at") or to_iso(migration_time)
                )
                last_seen_at = _row_value(row, "last_seen_at")
                revoked_at = _row_value(row, "revoked_at")
                connection.execute(
                    """
                    INSERT OR IGNORE INTO access_devices(
                        id, display_name, profile, created_at, last_seen_at,
                        revoked_at, user_agent, paired_from, access_route,
                        legacy_source_id
                    )
                    VALUES (?, ?, 'owner', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        device_id,
                        str(_row_value(row, "display_name") or "Mobile device")[:80],
                        created_at,
                        last_seen_at,
                        revoked_at,
                        _bounded(_row_value(row, "user_agent"), 256),
                        _bounded(_row_value(row, "paired_from"), 128),
                        _bounded(_row_value(row, "access_mode"), 80),
                        source_id,
                    ),
                )
                session_id = uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"row-bot:legacy-mobile-session:{source_id}",
                ).hex
                expires_at = to_iso(migration_time + LEGACY_MIGRATION_GRACE)
                connection.execute(
                    """
                    INSERT OR IGNORE INTO access_sessions(
                        id, device_id, token_hash, token_salt, token_format,
                        created_at, last_seen_at, expires_at, revoked_at,
                        lifetime
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'migrated')
                    """,
                    (
                        session_id,
                        device_id,
                        str(_row_value(row, "token_hash") or ""),
                        str(_row_value(row, "token_salt") or ""),
                        TokenFormat.LEGACY_RBD.value,
                        created_at,
                        last_seen_at,
                        expires_at,
                        revoked_at,
                    ),
                )
                migrated_count += 1

        self._set_meta_on_connection(
            connection,
            "legacy_mobile_migrated",
            {"at": to_iso(migration_time), "device_count": migrated_count},
            migration_time,
        )
        self._ensure_instance_id(connection, migration_time)

    def _migrate_single_owner(self, connection: sqlite3.Connection) -> None:
        """Normalize version-2 roles without elevating restricted credentials."""

        migration_time = utc_now()
        timestamp = to_iso(migration_time)
        explicit_devices = connection.execute(
            """
            SELECT id
              FROM access_devices
             WHERE profile = 'companion'
               AND legacy_source_id IS NULL
               AND revoked_at IS NULL
            """
        ).fetchall()
        for row in explicit_devices:
            device_id = str(row["id"])
            connection.execute(
                """
                UPDATE access_devices
                   SET revoked_at = COALESCE(revoked_at, ?)
                 WHERE id = ?
                """,
                (timestamp, device_id),
            )
            connection.execute(
                """
                UPDATE access_sessions
                   SET revoked_at = COALESCE(revoked_at, ?)
                 WHERE device_id = ?
                """,
                (timestamp, device_id),
            )
            self._insert_event_on_connection(
                connection,
                "owner_repair_required",
                device_id=device_id,
                detail={"reason": "single_owner_access_model"},
                now=migration_time,
            )

        pending_invitations = connection.execute(
            """
            SELECT id
              FROM access_invitations
             WHERE profile = 'companion'
               AND claimed_at IS NULL
               AND cancelled_at IS NULL
            """
        ).fetchall()
        for row in pending_invitations:
            invitation_id = str(row["id"])
            connection.execute(
                """
                UPDATE access_invitations
                   SET cancelled_at = COALESCE(cancelled_at, ?)
                 WHERE id = ?
                """,
                (timestamp, invitation_id),
            )
            self._insert_event_on_connection(
                connection,
                "invitation_cancelled_for_owner_migration",
                invitation_id=invitation_id,
                detail={"reason": "single_owner_access_model"},
                now=migration_time,
            )

        connection.execute(
            "UPDATE access_devices SET profile = 'owner' WHERE profile != 'owner'"
        )
        connection.execute(
            "UPDATE access_invitations SET profile = 'owner' WHERE profile != 'owner'"
        )

    def _ensure_instance_id(
        self,
        connection: sqlite3.Connection,
        now: datetime,
    ) -> str:
        row = connection.execute(
            "SELECT value_json FROM access_meta WHERE key = 'instance_id'"
        ).fetchone()
        if row is not None:
            value = json.loads(row["value_json"])
            if isinstance(value, str) and value:
                return value
        instance_id = uuid.uuid4().hex
        self._set_meta_on_connection(connection, "instance_id", instance_id, now)
        return instance_id

    @staticmethod
    def _set_meta_on_connection(
        connection: sqlite3.Connection,
        key: str,
        value: Any,
        now: datetime,
    ) -> None:
        connection.execute(
            """
            INSERT INTO access_meta(key, value_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (key, json.dumps(value, sort_keys=True), to_iso(now)),
        )

    @property
    def schema_version(self) -> int:
        self.ensure_schema()
        with self._read_connection() as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    @property
    def instance_id(self) -> str:
        value = self.get_meta("instance_id")
        if not isinstance(value, str) or not value:
            raise AccessStoreError("access instance ID is missing")
        return value

    def get_meta(self, key: str, default: Any = None) -> Any:
        self.ensure_schema()
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT value_json FROM access_meta WHERE key = ?",
                (key,),
            ).fetchone()
        return default if row is None else json.loads(row["value_json"])

    def set_meta(self, key: str, value: Any, *, now: datetime | None = None) -> None:
        self.ensure_schema()
        current = normalize_datetime(now)
        with self._immediate_transaction() as connection:
            self._set_meta_on_connection(connection, str(key)[:128], value, current)

    def create_invitation_record(
        self,
        *,
        invitation_id: str,
        secret_hash: str,
        secret_salt: str,
        session_lifetime: SessionLifetime,
        intended_origin: str,
        expires_at: datetime,
        created_by: str | None = None,
        access_route: str | None = None,
        now: datetime | None = None,
    ) -> AccessInvitation:
        self.ensure_schema()
        current = normalize_datetime(now)
        with self._immediate_transaction() as connection:
            connection.execute(
                """
                INSERT INTO access_invitations(
                    id, secret_hash, secret_salt, token_format, profile,
                    session_lifetime, intended_origin, created_at, expires_at,
                    created_by, access_route
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    invitation_id,
                    secret_hash,
                    secret_salt,
                    TokenFormat.INVITATION_V1.value,
                    "owner",
                    SessionLifetime(session_lifetime).value,
                    intended_origin,
                    to_iso(current),
                    to_iso(expires_at),
                    _bounded(created_by, 128),
                    _bounded(access_route, 80),
                ),
            )
        invitation = self.get_invitation(invitation_id)
        assert invitation is not None
        return invitation

    def get_invitation(self, invitation_id: str) -> AccessInvitation | None:
        self.ensure_schema()
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM access_invitations WHERE id = ?",
                (invitation_id,),
            ).fetchone()
        return _invitation_from_row(row) if row else None

    def list_invitations(self, *, limit: int = 100) -> list[AccessInvitation]:
        self.ensure_schema()
        safe_limit = max(1, min(int(limit), 200))
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM access_invitations ORDER BY created_at DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        return [_invitation_from_row(row) for row in rows]

    def cancel_invitation(
        self,
        invitation_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        self.ensure_schema()
        with self._immediate_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE access_invitations
                   SET cancelled_at = ?
                 WHERE id = ?
                   AND cancelled_at IS NULL
                   AND claimed_at IS NULL
                """,
                (to_iso(now), invitation_id),
            )
            return cursor.rowcount == 1

    def record_invitation_failure(
        self,
        invitation_id: str,
        *,
        effective_client: str | None = None,
        now: datetime | None = None,
    ) -> AccessInvitation | None:
        self.ensure_schema()
        current = normalize_datetime(now)
        with self._immediate_transaction() as connection:
            invitation = connection.execute(
                "SELECT id FROM access_invitations WHERE id = ?",
                (invitation_id,),
            ).fetchone()
            if invitation is None:
                return None
            self._record_invitation_failure_on_connection(
                connection,
                invitation_id,
                effective_client=effective_client,
                now=current,
            )
            connection.execute(
                """
                UPDATE access_invitations
                   SET failed_attempts = MIN(failed_attempts + 1, ?)
                 WHERE id = ?
                """,
                (CLAIM_FAILURE_LIMIT, invitation_id),
            )
        return self.get_invitation(invitation_id)

    def invitation_failure_locked(
        self,
        invitation_id: str,
        *,
        effective_client: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Return whether this invitation/client pair is temporarily locked."""

        self.ensure_schema()
        current = normalize_datetime(now)
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT locked_until
                  FROM access_invitation_failures
                 WHERE invitation_id = ? AND client_key = ?
                """,
                (invitation_id, _claim_client_key(effective_client)),
            ).fetchone()
        locked_until = parse_iso(row["locked_until"]) if row else None
        return locked_until is not None and locked_until > current

    @staticmethod
    def _record_invitation_failure_on_connection(
        connection: sqlite3.Connection,
        invitation_id: str,
        *,
        effective_client: str | None,
        now: datetime,
    ) -> None:
        client_key = _claim_client_key(effective_client)
        row = connection.execute(
            """
            SELECT failed_attempts, locked_until
              FROM access_invitation_failures
             WHERE invitation_id = ? AND client_key = ?
            """,
            (invitation_id, client_key),
        ).fetchone()
        existing_lock = parse_iso(row["locked_until"]) if row else None
        previous_attempts = 0
        if row is not None and (existing_lock is None or existing_lock > now):
            previous_attempts = int(row["failed_attempts"])
        failed_attempts = min(previous_attempts + 1, CLAIM_FAILURE_LIMIT)
        locked_until = (
            to_iso(now + CLAIM_LOCK_DURATION)
            if failed_attempts >= CLAIM_FAILURE_LIMIT
            else None
        )
        connection.execute(
            """
            INSERT INTO access_invitation_failures(
                invitation_id, client_key, failed_attempts, locked_until,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(invitation_id, client_key) DO UPDATE SET
                failed_attempts = excluded.failed_attempts,
                locked_until = excluded.locked_until,
                updated_at = excluded.updated_at
            """,
            (
                invitation_id,
                client_key,
                failed_attempts,
                locked_until,
                to_iso(now),
            ),
        )

    def claim_invitation_atomic(
        self,
        *,
        invitation_id: str,
        invitation_secret: str,
        expected_origin: str,
        device: AccessDevice,
        session: AccessSession,
        requested_lifetime: SessionLifetime | None = None,
        effective_client: str | None = None,
        user_agent: str | None = None,
        now: datetime | None = None,
    ) -> tuple[AccessDevice, AccessSession, AccessInvitation]:
        """Claim once, atomically inserting the resulting device and session."""
        self.ensure_schema()
        current = normalize_datetime(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM access_invitations WHERE id = ?",
                (invitation_id,),
            ).fetchone()
            if row is None:
                raise InvitationClaimError("invalid_invitation")
            invitation = _invitation_from_row(row)
            reason = _invitation_terminal_reason(invitation, current)
            if reason:
                raise InvitationClaimError(reason)
            if self._invitation_failure_locked_on_connection(
                connection,
                invitation_id,
                effective_client=effective_client,
                now=current,
            ):
                raise InvitationClaimError("locked")
            if (
                requested_lifetime is not None
                and SessionLifetime(requested_lifetime) != invitation.session_lifetime
            ):
                raise InvitationClaimError("immutable_mismatch")
            if expected_origin != invitation.intended_origin:
                raise InvitationClaimError("origin_mismatch")
            if not verify_secret(
                invitation_secret,
                salt=invitation.secret_salt,
                expected_hash=invitation.secret_hash,
            ):
                self._record_invitation_failure_on_connection(
                    connection,
                    invitation_id,
                    effective_client=effective_client,
                    now=current,
                )
                connection.execute(
                    """
                    UPDATE access_invitations
                       SET failed_attempts = MIN(failed_attempts + 1, ?)
                     WHERE id = ?
                    """,
                    (CLAIM_FAILURE_LIMIT, invitation_id),
                )
                self._insert_event_on_connection(
                    connection,
                    "invitation_claim_failed",
                    invitation_id=invitation_id,
                    effective_client=effective_client,
                    user_agent=user_agent,
                    detail={"reason": "invalid_invitation"},
                    now=current,
                )
                connection.commit()
                raise InvitationClaimError("invalid_invitation")

            connection.execute(
                """
                INSERT INTO access_devices(
                    id, display_name, profile, created_at, last_seen_at,
                    revoked_at, user_agent, paired_from, access_route,
                    legacy_source_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _device_values(device),
            )
            connection.execute(
                """
                INSERT INTO access_sessions(
                    id, device_id, token_hash, token_salt, token_format,
                    created_at, last_seen_at, expires_at, revoked_at, lifetime,
                    replaced_by_session_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _session_values(session),
            )
            claimed = connection.execute(
                """
                UPDATE access_invitations
                   SET claimed_at = ?
                 WHERE id = ?
                   AND claimed_at IS NULL
                   AND cancelled_at IS NULL
                """,
                (to_iso(current), invitation_id),
            )
            if claimed.rowcount != 1:
                raise InvitationClaimError("already_claimed")
            connection.execute(
                "DELETE FROM access_invitation_failures WHERE invitation_id = ?",
                (invitation_id,),
            )
            self._insert_event_on_connection(
                connection,
                "invitation_claimed",
                device_id=device.id,
                session_id=session.id,
                invitation_id=invitation_id,
                effective_client=effective_client,
                user_agent=user_agent,
                detail={"authority": "owner"},
                now=current,
            )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

        claimed_invitation = self.get_invitation(invitation_id)
        assert claimed_invitation is not None
        return device, session, claimed_invitation

    @staticmethod
    def _invitation_failure_locked_on_connection(
        connection: sqlite3.Connection,
        invitation_id: str,
        *,
        effective_client: str | None,
        now: datetime,
    ) -> bool:
        row = connection.execute(
            """
            SELECT locked_until
              FROM access_invitation_failures
             WHERE invitation_id = ? AND client_key = ?
            """,
            (invitation_id, _claim_client_key(effective_client)),
        ).fetchone()
        locked_until = parse_iso(row["locked_until"]) if row else None
        return locked_until is not None and locked_until > now

    def create_device_record(self, device: AccessDevice) -> AccessDevice:
        self.ensure_schema()
        with self._immediate_transaction() as connection:
            connection.execute(
                """
                INSERT INTO access_devices(
                    id, display_name, profile, created_at, last_seen_at,
                    revoked_at, user_agent, paired_from, access_route,
                    legacy_source_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _device_values(device),
            )
        created = self.get_device(device.id)
        assert created is not None
        return created

    def create_session_record(self, session: AccessSession) -> AccessSession:
        self.ensure_schema()
        with self._immediate_transaction() as connection:
            connection.execute(
                """
                INSERT INTO access_sessions(
                    id, device_id, token_hash, token_salt, token_format,
                    created_at, last_seen_at, expires_at, revoked_at, lifetime,
                    replaced_by_session_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _session_values(session),
            )
        created = self.get_session(session.id)
        assert created is not None
        return created

    def get_device(self, device_id: str) -> AccessDevice | None:
        self.ensure_schema()
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM access_devices WHERE id = ?",
                (device_id,),
            ).fetchone()
        return _device_from_row(row) if row else None

    def list_devices(self, *, include_revoked: bool = True) -> list[AccessDevice]:
        self.ensure_schema()
        query = "SELECT * FROM access_devices"
        if not include_revoked:
            query += " WHERE revoked_at IS NULL"
        query += " ORDER BY created_at DESC"
        with self._read_connection() as connection:
            rows = connection.execute(query).fetchall()
        return [_device_from_row(row) for row in rows]

    def get_session(self, session_id: str) -> AccessSession | None:
        self.ensure_schema()
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM access_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        return _session_from_row(row) if row else None

    def get_legacy_session_for_device(self, device_id: str) -> AccessSession | None:
        self.ensure_schema()
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT s.*
                  FROM access_sessions AS s
                  JOIN access_devices AS d ON d.id = s.device_id
                 WHERE (d.id = ? OR d.legacy_source_id = ?)
                   AND s.token_format = ?
                 ORDER BY s.created_at DESC
                 LIMIT 1
                """,
                (device_id, device_id, TokenFormat.LEGACY_RBD.value),
            ).fetchone()
        return _session_from_row(row) if row else None

    def list_sessions(
        self,
        *,
        device_id: str | None = None,
        include_revoked: bool = True,
    ) -> list[AccessSession]:
        self.ensure_schema()
        clauses: list[str] = []
        parameters: list[Any] = []
        if device_id is not None:
            clauses.append("device_id = ?")
            parameters.append(device_id)
        if not include_revoked:
            clauses.append("revoked_at IS NULL")
        query = "SELECT * FROM access_sessions"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC"
        with self._read_connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_session_from_row(row) for row in rows]

    def touch_session(
        self,
        session_id: str,
        *,
        now: datetime | None = None,
    ) -> None:
        self.ensure_schema()
        timestamp = to_iso(now)
        with self._immediate_transaction() as connection:
            row = connection.execute(
                """
                UPDATE access_sessions
                   SET last_seen_at = ?
                 WHERE id = ?
                   AND revoked_at IS NULL
                """,
                (timestamp, session_id),
            )
            if row.rowcount:
                connection.execute(
                    """
                    UPDATE access_devices
                       SET last_seen_at = ?
                     WHERE id = (
                         SELECT device_id FROM access_sessions WHERE id = ?
                     )
                       AND revoked_at IS NULL
                    """,
                    (timestamp, session_id),
                )

    def revoke_session(
        self,
        session_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        self.ensure_schema()
        with self._immediate_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE access_sessions
                   SET revoked_at = COALESCE(revoked_at, ?)
                 WHERE id = ?
                   AND revoked_at IS NULL
                """,
                (to_iso(now), session_id),
            )
            return cursor.rowcount == 1

    def revoke_device(
        self,
        device_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        self.ensure_schema()
        timestamp = to_iso(now)
        with self._immediate_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE access_devices
                   SET revoked_at = COALESCE(revoked_at, ?)
                 WHERE id = ?
                   AND revoked_at IS NULL
                """,
                (timestamp, device_id),
            )
            connection.execute(
                """
                UPDATE access_sessions
                   SET revoked_at = COALESCE(revoked_at, ?)
                 WHERE device_id = ?
                """,
                (timestamp, device_id),
            )
            return cursor.rowcount == 1

    def revoke_all(
        self,
        *,
        except_session_id: str | None = None,
        now: datetime | None = None,
    ) -> int:
        self.ensure_schema()
        timestamp = to_iso(now)
        with self._immediate_transaction() as connection:
            if except_session_id is None:
                cursor = connection.execute(
                    """
                    UPDATE access_sessions
                       SET revoked_at = COALESCE(revoked_at, ?)
                     WHERE revoked_at IS NULL
                    """,
                    (timestamp,),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE access_sessions
                       SET revoked_at = COALESCE(revoked_at, ?)
                     WHERE revoked_at IS NULL
                       AND id != ?
                    """,
                    (timestamp, except_session_id),
                )
            return int(cursor.rowcount or 0)

    def log_event(
        self,
        event_type: str,
        *,
        device_id: str | None = None,
        session_id: str | None = None,
        invitation_id: str | None = None,
        effective_client: str | None = None,
        user_agent: str | None = None,
        detail: Mapping[str, Any] | None = None,
        now: datetime | None = None,
        event_id: str | None = None,
    ) -> AccessEvent:
        self.ensure_schema()
        current = normalize_datetime(now)
        row_id = event_id or uuid.uuid4().hex
        with self._immediate_transaction() as connection:
            self._insert_event_on_connection(
                connection,
                event_type,
                device_id=device_id,
                session_id=session_id,
                invitation_id=invitation_id,
                effective_client=effective_client,
                user_agent=user_agent,
                detail=detail,
                now=current,
                event_id=row_id,
            )
        event = self.get_event(row_id)
        assert event is not None
        return event

    @staticmethod
    def _insert_event_on_connection(
        connection: sqlite3.Connection,
        event_type: str,
        *,
        device_id: str | None = None,
        session_id: str | None = None,
        invitation_id: str | None = None,
        effective_client: str | None = None,
        user_agent: str | None = None,
        detail: Mapping[str, Any] | None = None,
        now: datetime,
        event_id: str | None = None,
    ) -> str:
        row_id = event_id or uuid.uuid4().hex
        safe_detail = _safe_event_detail(detail)
        connection.execute(
            """
            INSERT INTO access_events(
                id, event_type, device_id, session_id, invitation_id,
                effective_client, user_agent, created_at, detail_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id,
                _bounded(event_type, 80) or "unknown",
                _bounded(device_id, 128),
                _bounded(session_id, 128),
                _bounded(invitation_id, 128),
                _bounded(effective_client, 128),
                _bounded(user_agent, 256),
                to_iso(now),
                json.dumps(safe_detail, sort_keys=True),
            ),
        )
        return row_id

    def get_event(self, event_id: str) -> AccessEvent | None:
        self.ensure_schema()
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM access_events WHERE id = ?",
                (event_id,),
            ).fetchone()
        return _event_from_row(row) if row else None

    def recent_events(self, *, limit: int = 50) -> list[AccessEvent]:
        self.ensure_schema()
        safe_limit = max(1, min(int(limit), 200))
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM access_events ORDER BY created_at DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def prune(
        self,
        *,
        now: datetime | None = None,
        retention: timedelta = timedelta(days=30),
        max_events: int = MAX_EVENT_ROWS,
    ) -> dict[str, int]:
        """Remove terminal credentials/events older than the retention window."""
        self.ensure_schema()
        current = normalize_datetime(now)
        cutoff = to_iso(current - retention)
        current_text = to_iso(current)
        safe_max_events = max(100, min(int(max_events), 100_000))
        with self._immediate_transaction() as connection:
            invitations = connection.execute(
                """
                DELETE FROM access_invitations
                 WHERE (expires_at <= ? OR claimed_at IS NOT NULL OR cancelled_at IS NOT NULL)
                   AND created_at <= ?
                """,
                (current_text, cutoff),
            ).rowcount
            sessions = connection.execute(
                """
                DELETE FROM access_sessions
                 WHERE (expires_at <= ? OR revoked_at IS NOT NULL)
                   AND created_at <= ?
                """,
                (current_text, cutoff),
            ).rowcount
            old_events = connection.execute(
                "DELETE FROM access_events WHERE created_at <= ?",
                (cutoff,),
            ).rowcount
            overflow_events = connection.execute(
                """
                DELETE FROM access_events
                 WHERE id IN (
                    SELECT id FROM access_events
                     ORDER BY created_at DESC
                     LIMIT -1 OFFSET ?
                 )
                """,
                (safe_max_events,),
            ).rowcount
        return {
            "invitations": int(invitations or 0),
            "sessions": int(sessions or 0),
            "events": int(old_events or 0) + int(overflow_events or 0),
        }


def _bounded(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    return str(value)[:limit]


def _safe_event_detail(detail: Mapping[str, Any] | None) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for raw_key, raw_value in (detail or {}).items():
        key = str(raw_key)[:80]
        if key.lower() in _SENSITIVE_DETAIL_KEYS:
            continue
        if isinstance(raw_value, (str, int, float, bool)) or raw_value is None:
            safe[key] = raw_value if not isinstance(raw_value, str) else raw_value[:512]
    while (
        len(json.dumps(safe, sort_keys=True).encode("utf-8")) > MAX_EVENT_DETAIL_BYTES
    ):
        if not safe:
            break
        safe.pop(next(reversed(safe)))
    return safe


def _device_values(device: AccessDevice) -> tuple[Any, ...]:
    return (
        device.id,
        device.display_name[:80],
        "owner",
        to_iso(device.created_at),
        to_iso(device.last_seen_at) if device.last_seen_at else None,
        to_iso(device.revoked_at) if device.revoked_at else None,
        _bounded(device.user_agent, 256),
        _bounded(device.paired_from, 128),
        _bounded(device.access_route, 80),
        _bounded(device.legacy_source_id, 128),
    )


def _session_values(session: AccessSession) -> tuple[Any, ...]:
    return (
        session.id,
        session.device_id,
        session.token_hash,
        session.token_salt,
        session.token_format.value,
        to_iso(session.created_at),
        to_iso(session.last_seen_at) if session.last_seen_at else None,
        to_iso(session.expires_at),
        to_iso(session.revoked_at) if session.revoked_at else None,
        session.lifetime.value,
        session.replaced_by_session_id,
    )


def _device_from_row(row: sqlite3.Row) -> AccessDevice:
    return AccessDevice(
        id=str(row["id"]),
        display_name=str(row["display_name"]),
        created_at=parse_iso(row["created_at"]) or utc_now(),
        last_seen_at=parse_iso(row["last_seen_at"]),
        revoked_at=parse_iso(row["revoked_at"]),
        user_agent=row["user_agent"],
        paired_from=row["paired_from"],
        access_route=row["access_route"],
        legacy_source_id=row["legacy_source_id"],
    )


def _session_from_row(row: sqlite3.Row) -> AccessSession:
    return AccessSession(
        id=str(row["id"]),
        device_id=str(row["device_id"]),
        token_hash=str(row["token_hash"]),
        token_salt=str(row["token_salt"]),
        token_format=TokenFormat(row["token_format"]),
        created_at=parse_iso(row["created_at"]) or utc_now(),
        last_seen_at=parse_iso(row["last_seen_at"]),
        expires_at=parse_iso(row["expires_at"]) or utc_now(),
        revoked_at=parse_iso(row["revoked_at"]),
        lifetime=SessionLifetime(row["lifetime"]),
        replaced_by_session_id=row["replaced_by_session_id"],
    )


def _invitation_from_row(row: sqlite3.Row) -> AccessInvitation:
    return AccessInvitation(
        id=str(row["id"]),
        secret_hash=str(row["secret_hash"]),
        secret_salt=str(row["secret_salt"]),
        token_format=TokenFormat(row["token_format"]),
        session_lifetime=SessionLifetime(row["session_lifetime"]),
        intended_origin=str(row["intended_origin"]),
        created_at=parse_iso(row["created_at"]) or utc_now(),
        expires_at=parse_iso(row["expires_at"]) or utc_now(),
        claimed_at=parse_iso(row["claimed_at"]),
        cancelled_at=parse_iso(row["cancelled_at"]),
        created_by=row["created_by"],
        failed_attempts=int(row["failed_attempts"]),
        locked_until=parse_iso(row["locked_until"]),
        access_route=row["access_route"],
    )


def _event_from_row(row: sqlite3.Row) -> AccessEvent:
    return AccessEvent(
        id=str(row["id"]),
        event_type=str(row["event_type"]),
        device_id=row["device_id"],
        session_id=row["session_id"],
        invitation_id=row["invitation_id"],
        effective_client=row["effective_client"],
        user_agent=row["user_agent"],
        created_at=parse_iso(row["created_at"]) or utc_now(),
        detail=json.loads(row["detail_json"] or "{}"),
    )


def _invitation_terminal_reason(
    invitation: AccessInvitation,
    now: datetime,
) -> str | None:
    if invitation.claimed_at is not None:
        return "already_claimed"
    if invitation.cancelled_at is not None:
        return "cancelled"
    if invitation.expires_at <= now:
        return "expired"
    if invitation.locked_until is not None and invitation.locked_until > now:
        return "locked"
    return None
