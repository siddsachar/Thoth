"""Compatibility adapter for the central Row-Bot access middleware.

The historical mobile gate remains importable for downstream integrations, but
all request provenance, authentication, and authorization decisions are made
by :mod:`row_bot.access`.
"""

from __future__ import annotations

from ipaddress import ip_address
from typing import Awaitable, Callable

from row_bot.access.config import AccessConfig
from row_bot.access.cookies import AccessCookieManager
from row_bot.access.middleware import AccessMiddleware
from row_bot.access.request_context import RECOGNIZED_FORWARDING_HEADERS
from row_bot.access.routes import AccessSessionAuthenticator
from row_bot.access.service import AccessService
from row_bot.mobile.store import MobileAuthStore

ASGIApp = Callable[[dict, Callable, Callable], Awaitable[None]]

# Kept for callers and inventory checks that imported the legacy constant.
FORWARDED_HEADER_NAMES = frozenset(
    name.encode("ascii") for name in RECOGNIZED_FORWARDING_HEADERS
)

# The central middleware owns rejection throttling.  These aliases preserve the
# old public constants without creating a second authorization implementation.
REJECTION_LOG_CACHE_MAX = 128
REJECTION_LOG_WINDOW_SECONDS = 30.0


def recognized_forwarding_header_names(scope: dict) -> tuple[str, ...]:
    """Return recognized forwarding header names, never their values."""

    present = {
        bytes(name).decode("latin-1", errors="ignore").lower()
        for name, _value in scope.get("headers", [])
    }
    return tuple(sorted(present & RECOGNIZED_FORWARDING_HEADERS))


def has_forwarded_headers(scope: dict) -> bool:
    return bool(recognized_forwarding_header_names(scope))


def is_true_local_scope(scope: dict) -> bool:
    """Compatibility predicate matching central direct-loopback semantics."""

    client = scope.get("client") or ("", 0)
    try:
        address = ip_address(str(client[0] or "").strip("[]"))
    except (IndexError, TypeError, ValueError):
        return False
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback and not has_forwarded_headers(scope)


def authenticated_mobile_device(scope: dict, store: MobileAuthStore):
    """Validate a compatible access cookie through the canonical service."""

    service = AccessService(store.access_store)
    cookies = AccessCookieManager(service.instance_id)
    authenticator = AccessSessionAuthenticator(service, cookies)
    try:
        provenance = AccessMiddleware(
            lambda _scope, _receive, _send: None,
            config=AccessConfig.from_env(),
        ).resolver.resolve_provenance(scope)
    except Exception:
        return None
    return authenticator.authenticate_scope(scope, provenance)


class MobileAccessGate:
    """Deprecated constructor-compatible wrapper around ``AccessMiddleware``."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        store: MobileAuthStore | None = None,
        config: AccessConfig | None = None,
    ) -> None:
        self.store = store or MobileAuthStore()
        service = AccessService(self.store.access_store)
        cookies = AccessCookieManager(service.instance_id)
        self._middleware = AccessMiddleware(
            app,
            config=config or AccessConfig.from_env(),
            session_authenticator=AccessSessionAuthenticator(service, cookies),
        )
        # Compatibility for tests and diagnostics; this is the central cache.
        self._rejection_log_times = self._middleware._rejection_log_times

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        await self._middleware(scope, receive, send)


__all__ = [
    "FORWARDED_HEADER_NAMES",
    "MobileAccessGate",
    "REJECTION_LOG_CACHE_MAX",
    "REJECTION_LOG_WINDOW_SECONDS",
    "authenticated_mobile_device",
    "has_forwarded_headers",
    "is_true_local_scope",
    "recognized_forwarding_header_names",
]
