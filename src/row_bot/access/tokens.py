"""Versioned access tokens with salted PBKDF2 verifiers."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import re
import secrets

from row_bot.access.models import TokenFormat

TOKEN_HASH_ITERATIONS = 200_000
INVITATION_TOKEN_PREFIX = "rbi"
SESSION_TOKEN_PREFIX = "rbs"
LEGACY_DEVICE_TOKEN_PREFIX = "rbd"

_SECRET_BYTES = 32
_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{40,}$")
_LEGACY_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")
_ID_RE = re.compile(r"^[a-f0-9]{32}$")


@dataclass(frozen=True)
class SecretHash:
    secret_hash: str
    salt: str


@dataclass(frozen=True)
class IssuedToken:
    """A raw token returned once plus fields safe to persist."""

    token: str
    record_id: str
    secret_hash: str
    secret_salt: str
    token_format: TokenFormat


def hash_secret(secret: str, *, salt: str | None = None) -> SecretHash:
    """Hash a secret with the reviewed stdlib PBKDF2 primitive."""
    if not secret:
        raise ValueError("secret is required")
    salt_hex = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        secret.encode("utf-8"),
        bytes.fromhex(salt_hex),
        TOKEN_HASH_ITERATIONS,
    )
    return SecretHash(secret_hash=digest.hex(), salt=salt_hex)


def verify_secret(secret: str, *, salt: str, expected_hash: str) -> bool:
    """Compare a candidate secret with a stored verifier in constant time."""
    try:
        candidate = hash_secret(secret, salt=salt).secret_hash
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(candidate, expected_hash)


def _join(prefix: str, record_id: str, secret: str) -> str:
    return f"{prefix}_{record_id}.{secret}"


def _split(
    value: str,
    prefix: str,
    *,
    allow_legacy_secret_length: bool = False,
) -> tuple[str, str] | None:
    text = str(value or "").strip()
    expected = f"{prefix}_"
    if not text.startswith(expected) or "." not in text:
        return None
    record_id, secret = text[len(expected) :].split(".", 1)
    secret_pattern = _LEGACY_SECRET_RE if allow_legacy_secret_length else _SECRET_RE
    if not _ID_RE.fullmatch(record_id) or not secret_pattern.fullmatch(secret):
        return None
    return record_id, secret


def issue_invitation_token(invitation_id: str) -> IssuedToken:
    return _issue(INVITATION_TOKEN_PREFIX, invitation_id, TokenFormat.INVITATION_V1)


def issue_session_token(session_id: str) -> IssuedToken:
    return _issue(SESSION_TOKEN_PREFIX, session_id, TokenFormat.SESSION_V1)


def _issue(prefix: str, record_id: str, token_format: TokenFormat) -> IssuedToken:
    if not _ID_RE.fullmatch(record_id):
        raise ValueError("record ID must be 32 lowercase hexadecimal characters")
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    verifier = hash_secret(secret)
    return IssuedToken(
        token=_join(prefix, record_id, secret),
        record_id=record_id,
        secret_hash=verifier.secret_hash,
        secret_salt=verifier.salt,
        token_format=token_format,
    )


def parse_invitation_token(token: str) -> tuple[str, str] | None:
    return _split(token, INVITATION_TOKEN_PREFIX)


def parse_session_token(token: str) -> tuple[str, str] | None:
    return _split(token, SESSION_TOKEN_PREFIX)


def parse_legacy_device_token(token: str) -> tuple[str, str] | None:
    return _split(
        token,
        LEGACY_DEVICE_TOKEN_PREFIX,
        allow_legacy_secret_length=True,
    )
