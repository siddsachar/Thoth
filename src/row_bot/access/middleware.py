"""ASGI authentication and authorization middleware for Row-Bot access."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import suppress
import inspect
import json
import logging
import time
from typing import Awaitable, Callable, Mapping, Protocol
from urllib.parse import quote

from row_bot.access.config import AccessConfig
from row_bot.access.policy import AccessPolicy, RouteClassification, RouteKind
from row_bot.access.request_context import (
    ACCESS_CONTEXT_SCOPE_KEY,
    PROVENANCE_SCOPE_KEY,
    AccessContext,
    RequestContextError,
    RequestContextResolver,
    RequestProvenance,
    SessionIdentity,
    request_origin_matches,
    safe_relative_next,
)
from row_bot.access.runtime_policy import RuntimeAccessPolicy

ASGIReceive = Callable[[], Awaitable[dict]]
ASGISend = Callable[[dict], Awaitable[None]]
ASGIApp = Callable[[dict, ASGIReceive, ASGISend], Awaitable[None]]
SessionAuthenticator = Callable[
    [Mapping[str, object], RequestProvenance],
    SessionIdentity | object | Awaitable[SessionIdentity | object | None] | None,
]

logger = logging.getLogger(__name__)

_REJECTION_CACHE_MAX = 128
_REJECTION_WINDOW_SECONDS = 30.0


class SupportsSessionAuthentication(Protocol):
    def authenticate_scope(
        self,
        scope: Mapping[str, object],
        provenance: RequestProvenance,
    ) -> (
        SessionIdentity | object | Awaitable[SessionIdentity | object | None] | None
    ): ...


def _method(scope: Mapping[str, object]) -> str:
    return str(scope.get("method") or "GET").upper()[:16]


def _path(scope: Mapping[str, object]) -> str:
    path = str(scope.get("path") or "/").split("?", 1)[0] or "/"
    return path.replace("\r", "_").replace("\n", "_").replace("\t", "_")[:256]


def _coerce_session_identity(value: object) -> SessionIdentity | None:
    if value is None:
        return None
    if isinstance(value, SessionIdentity):
        return value
    if isinstance(value, tuple) and len(value) == 2:
        session, device = value
        device_id = getattr(device, "id", None) or getattr(session, "device_id", None)
        session_id = getattr(session, "id", None)
    elif isinstance(value, Mapping):
        device_id = value.get("device_id")
        session_id = value.get("session_id")
    else:
        authenticated_device = getattr(value, "device", None)
        authenticated_session = getattr(value, "session", None)
        device_id = getattr(value, "device_id", None) or getattr(
            authenticated_device, "id", None
        )
        session_id = (
            getattr(value, "session_id", None)
            or getattr(authenticated_session, "id", None)
            or getattr(value, "id", None)
        )
    if not device_id or not session_id:
        return None
    return SessionIdentity(
        device_id=str(device_id),
        session_id=str(session_id),
    )


async def _send_http(
    send: ASGISend,
    status: int,
    body: bytes,
    *,
    content_type: bytes = b"application/json; charset=utf-8",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    response_headers = [
        (b"content-type", content_type),
        (b"cache-control", b"no-store"),
        (b"pragma", b"no-cache"),
    ]
    response_headers.extend(headers or [])
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": response_headers,
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _json_error(
    send: ASGISend,
    status: int,
    code: str,
) -> None:
    body = json.dumps(
        {"ok": False, "error": code},
        separators=(",", ":"),
    ).encode("utf-8")
    await _send_http(send, status, body)


async def _redirect_connect(scope: Mapping[str, object], send: ASGISend) -> None:
    raw_path = str(scope.get("path") or "/")
    query = bytes(scope.get("query_string") or b"").decode("latin-1", errors="ignore")
    target = raw_path if not query else f"{raw_path}?{query}"
    next_path = safe_relative_next(target)
    location = f"/connect?next={quote(next_path, safe='')}".encode("ascii")
    await _send_http(
        send,
        303,
        b"",
        content_type=b"text/plain; charset=utf-8",
        headers=[(b"location", location), (b"x-robots-tag", b"noindex")],
    )


class AccessMiddleware:
    """Resolve and enforce access policy for both HTTP and WebSocket scopes."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        config: AccessConfig | None = None,
        runtime_policy: RuntimeAccessPolicy | None = None,
        policy: AccessPolicy | None = None,
        session_authenticator: (
            SessionAuthenticator | SupportsSessionAuthentication | None
        ) = None,
        websocket_revalidation_seconds: float = 5.0,
    ) -> None:
        self.app = app
        if runtime_policy is not None and config is not None:
            raise ValueError("pass config or runtime_policy, not both")
        self.runtime_policy = runtime_policy
        self.config = (
            runtime_policy.base_config
            if runtime_policy is not None
            else config or AccessConfig.from_env()
        )
        self.policy = policy or AccessPolicy()
        # Retained for compatibility with callers that intentionally resolve
        # against the immutable base policy outside middleware admission.
        self.resolver = RequestContextResolver(self.config)
        self.session_authenticator = session_authenticator
        self.websocket_revalidation_seconds = max(
            0.01, min(float(websocket_revalidation_seconds), 5.0)
        )
        self._rejection_log_times: OrderedDict[tuple[object, ...], float] = (
            OrderedDict()
        )

    async def _authenticate(
        self,
        scope: Mapping[str, object],
        provenance: RequestProvenance,
    ) -> SessionIdentity | None:
        authenticator = self.session_authenticator
        if authenticator is None:
            return None
        if callable(authenticator):
            result = authenticator(scope, provenance)
        else:
            result = authenticator.authenticate_scope(scope, provenance)
        if inspect.isawaitable(result):
            result = await result
        return _coerce_session_identity(result)

    def _log_rejection(
        self,
        scope: Mapping[str, object],
        code: str,
        *,
        context: AccessContext | None = None,
    ) -> None:
        peer = context.transport_peer if context is not None else "invalid"
        key = (
            str(scope.get("type") or "")[:16],
            peer,
            _method(scope),
            _path(scope),
            code,
        )
        now = time.monotonic()
        cutoff = now - _REJECTION_WINDOW_SECONDS
        while self._rejection_log_times:
            _oldest_key, oldest = next(iter(self._rejection_log_times.items()))
            if oldest > cutoff:
                break
            self._rejection_log_times.popitem(last=False)
        if key in self._rejection_log_times:
            return
        if len(self._rejection_log_times) >= _REJECTION_CACHE_MAX:
            return
        self._rejection_log_times[key] = now
        logger.warning(
            "access rejected scope=%s peer=%s method=%s path=%s decision=%s",
            key[0],
            peer,
            key[2],
            key[3],
            code,
        )

    async def _reject_context_error(
        self,
        scope: Mapping[str, object],
        send: ASGISend,
        exc: RequestContextError,
    ) -> None:
        self._log_rejection(scope, exc.code)
        if scope.get("type") == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await _json_error(send, exc.status_code, exc.code)

    async def _reject_authorization(
        self,
        scope: Mapping[str, object],
        send: ASGISend,
        classification: RouteClassification,
        context: AccessContext,
        *,
        status_code: int,
        reason: str,
    ) -> None:
        self._log_rejection(scope, reason, context=context)
        if scope.get("type") == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if status_code == 401 and classification.browser_navigation:
            await _redirect_connect(scope, send)
            return
        await _json_error(send, status_code, reason)

    async def __call__(
        self,
        scope: dict,
        receive: ASGIReceive,
        send: ASGISend,
    ) -> None:
        if scope.get("type") not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        try:
            snapshot = (
                self.runtime_policy.snapshot()
                if self.runtime_policy is not None
                else None
            )
            resolver = (
                RequestContextResolver(snapshot.config_for_scope(scope))
                if snapshot is not None
                else self.resolver
            )
            provenance = resolver.resolve_provenance(scope)
            session = await self._authenticate(scope, provenance)
            context = resolver.resolve(
                scope,
                session=session,
            )
        except RequestContextError as exc:
            await self._reject_context_error(scope, send, exc)
            return
        except Exception:
            logger.warning(
                "access session authentication failed closed path=%s", _path(scope)
            )
            if scope.get("type") == "websocket":
                await send({"type": "websocket.close", "code": 1011})
            else:
                await _json_error(send, 401, "authentication_required")
            return

        forwarded_scope = dict(scope)
        forwarded_scope[PROVENANCE_SCOPE_KEY] = provenance
        forwarded_scope[ACCESS_CONTEXT_SCOPE_KEY] = context
        state = dict(scope.get("state") or {})
        state["row_bot_access_context"] = context
        state["row_bot_request_provenance"] = provenance
        forwarded_scope["state"] = state

        classification = self.policy.classify(forwarded_scope)
        decision = self.policy.authorize(context, classification)
        if not decision.allowed:
            await self._reject_authorization(
                forwarded_scope,
                send,
                classification,
                context,
                status_code=decision.status_code,
                reason=decision.reason,
            )
            return
        if (
            classification.require_same_origin
            and classification.kind is not RouteKind.DELEGATED
            and not request_origin_matches(context, forwarded_scope)
        ):
            await self._reject_authorization(
                forwarded_scope,
                send,
                classification,
                context,
                status_code=403,
                reason="origin_required",
            )
            return
        if scope.get("type") != "websocket" or context.session_id is None:
            await self.app(forwarded_scope, receive, send)
            return

        app_task = asyncio.create_task(
            self.app(forwarded_scope, receive, send),
            name="row-bot-access-websocket",
        )
        try:
            while not app_task.done():
                done, _pending = await asyncio.wait(
                    {app_task},
                    timeout=self.websocket_revalidation_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if done:
                    break
                try:
                    current_session = await self._authenticate(
                        forwarded_scope, provenance
                    )
                except Exception:
                    logger.warning(
                        "access websocket revalidation failed closed path=%s",
                        _path(forwarded_scope),
                    )
                    current_session = None
                if (
                    current_session is None
                    or current_session.session_id != context.session_id
                    or current_session.device_id != context.device_id
                ):
                    self._log_rejection(
                        forwarded_scope,
                        "session_revoked",
                        context=context,
                    )
                    await send({"type": "websocket.close", "code": 1008})
                    app_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await app_task
                    return
            await app_task
        finally:
            if not app_task.done():
                app_task.cancel()
                with suppress(asyncio.CancelledError):
                    await app_task


AccessControlMiddleware = AccessMiddleware
