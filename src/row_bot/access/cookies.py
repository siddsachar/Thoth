"""Instance-scoped cookie handling for Row-Bot access sessions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from http.cookies import SimpleCookie

from starlette.responses import Response

from row_bot.access.request_context import AccessContext, RequestProvenance

LEGACY_HTTPS_COOKIE_NAME = "__Host-row_bot_mobile"
LEGACY_HTTP_COOKIE_NAME = "row_bot_mobile_lan"
_COOKIE_DIGEST_LENGTH = 12


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def cookie_suffix_for_instance(instance_id: str) -> str:
    """Return a stable, non-secret cookie namespace for one Row-Bot instance."""

    value = str(instance_id or "").strip()
    if not value:
        raise ValueError("instance_id is required")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:_COOKIE_DIGEST_LENGTH]


@dataclass(frozen=True, slots=True)
class AccessCookieNames:
    https: str
    http: str


def cookie_names_for_instance(instance_id: str) -> AccessCookieNames:
    suffix = cookie_suffix_for_instance(instance_id)
    return AccessCookieNames(
        https=f"__Host-row_bot_access_{suffix}",
        http=f"row_bot_access_{suffix}",
    )


def _scheme_from_context(context: AccessContext | RequestProvenance | str) -> str:
    if isinstance(context, str):
        scheme = context
    else:
        scheme = context.scheme
    return "https" if str(scheme).lower() == "https" else "http"


def _cookie_header(scope: dict) -> str:
    values = [
        bytes(value).decode("latin-1", errors="ignore")
        for name, value in scope.get("headers", [])
        if bytes(name).lower() == b"cookie"
    ]
    return "; ".join(values)


def _parse_cookie_header(value: str | bytes | None) -> SimpleCookie:
    parsed = SimpleCookie()
    if not value:
        return parsed
    text = (
        value.decode("latin-1", errors="ignore")
        if isinstance(value, bytes)
        else str(value)
    )
    try:
        parsed.load(text)
    except Exception:
        return SimpleCookie()
    return parsed


class AccessCookieManager:
    """Issue, read, and clear one instance's access-session cookies."""

    def __init__(self, instance_id: str) -> None:
        self.names = cookie_names_for_instance(instance_id)

    def name_for(self, context: AccessContext | RequestProvenance | str) -> str:
        return (
            self.names.https
            if _scheme_from_context(context) == "https"
            else self.names.http
        )

    def extract(
        self,
        cookie_header: str | bytes | None,
        *,
        context: AccessContext | RequestProvenance | str,
    ) -> str:
        parsed = _parse_cookie_header(cookie_header)
        preferred = self.name_for(context)
        alternate = (
            self.names.http if preferred == self.names.https else self.names.https
        )
        for name in (preferred, alternate):
            morsel = parsed.get(name)
            if morsel is not None and morsel.value:
                return morsel.value
        return ""

    def extract_from_scope(
        self,
        scope: dict,
        *,
        context: AccessContext | RequestProvenance | str,
    ) -> str:
        return self.extract(_cookie_header(scope), context=context)

    def extract_legacy(
        self,
        cookie_header: str | bytes | None,
        *,
        context: AccessContext | RequestProvenance | str,
    ) -> str:
        parsed = _parse_cookie_header(cookie_header)
        preferred = (
            LEGACY_HTTPS_COOKIE_NAME
            if _scheme_from_context(context) == "https"
            else LEGACY_HTTP_COOKIE_NAME
        )
        alternate = (
            LEGACY_HTTP_COOKIE_NAME
            if preferred == LEGACY_HTTPS_COOKIE_NAME
            else LEGACY_HTTPS_COOKIE_NAME
        )
        for name in (preferred, alternate):
            morsel = parsed.get(name)
            if morsel is not None and morsel.value:
                return morsel.value
        return ""

    def extract_legacy_from_scope(
        self,
        scope: dict,
        *,
        context: AccessContext | RequestProvenance | str,
    ) -> str:
        return self.extract_legacy(_cookie_header(scope), context=context)

    def set_session(
        self,
        response: Response,
        token: str,
        *,
        context: AccessContext | RequestProvenance | str,
        expires_at: datetime,
        now: datetime | None = None,
    ) -> None:
        current = _utc(now)
        expiry = _utc(expires_at)
        max_age = max(0, int((expiry - current).total_seconds()))
        secure = _scheme_from_context(context) == "https"
        response.set_cookie(
            key=self.names.https if secure else self.names.http,
            value=token,
            max_age=max_age,
            expires=expiry,
            path="/",
            secure=secure,
            httponly=True,
            samesite="lax",
        )

    def clear(self, response: Response) -> None:
        """Clear current-instance cookies plus compatible legacy names."""

        for name, secure in (
            (self.names.https, True),
            (self.names.http, False),
            (LEGACY_HTTPS_COOKIE_NAME, True),
            (LEGACY_HTTP_COOKIE_NAME, False),
        ):
            response.delete_cookie(
                key=name,
                path="/",
                secure=secure,
                httponly=True,
                samesite="lax",
            )


__all__ = [
    "AccessCookieManager",
    "AccessCookieNames",
    "LEGACY_HTTP_COOKIE_NAME",
    "LEGACY_HTTPS_COOKIE_NAME",
    "cookie_names_for_instance",
    "cookie_suffix_for_instance",
]
