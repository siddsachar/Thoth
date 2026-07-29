"""HTTP routes for Row-Bot device invitations and access sessions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
import json
from typing import Any, Mapping
from urllib.parse import parse_qs

from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from row_bot.access.config import AccessConfig
from row_bot.access.cookies import AccessCookieManager
from row_bot.access.models import AccessCapability, AccessProfile, SessionLifetime
from row_bot.access.request_context import (
    ACCESS_CONTEXT_SCOPE_KEY,
    AccessContext,
    RequestProvenance,
    request_origin_matches,
    safe_relative_next,
)
from row_bot.access.service import AccessService, InvitationClaimError

ACCESS_RESPONSE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "X-Robots-Tag": "noindex",
    "Referrer-Policy": "no-referrer",
}
CONNECT_PAGE_HEADERS = {
    **ACCESS_RESPONSE_HEADERS,
    # Same-origin POST navigations must retain an Origin header in Chromium.
    # The invitation is removed from the visible URL before the form renders,
    # and cross-origin referrers remain suppressed by this policy.
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; "
        "script-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
}
_TERMINAL_MESSAGES = {
    "already_claimed": (
        "This invitation was already used.",
        "Ask the owner to create a new invitation.",
    ),
    "cancelled": (
        "This invitation was cancelled.",
        "Ask the owner to create a new invitation.",
    ),
    "expired": (
        "This invitation expired.",
        "Invitations last 10 minutes. Ask the owner to create a new one.",
    ),
    "locked": (
        "This invitation is temporarily locked.",
        "Ask the owner to cancel it and create a new invitation.",
    ),
    "invalid_invitation": (
        "This invitation is invalid.",
        "Check the complete link or ask the owner to create a new invitation.",
    ),
    "origin_mismatch": (
        "This invitation is for a different Row-Bot address.",
        "Open the exact address shown by the owner.",
    ),
    "immutable_mismatch": (
        "This invitation could not be used.",
        "Ask the owner to create a new invitation.",
    ),
}


def _context(request: Request) -> AccessContext | None:
    context = request.scope.get(ACCESS_CONTEXT_SCOPE_KEY)
    if isinstance(context, AccessContext):
        return context
    state_context = getattr(request.state, "row_bot_access_context", None)
    return state_context if isinstance(state_context, AccessContext) else None


def _json(
    payload: Mapping[str, Any],
    *,
    status_code: int = 200,
) -> JSONResponse:
    return JSONResponse(
        dict(payload),
        status_code=status_code,
        headers=ACCESS_RESPONSE_HEADERS,
    )


def _error(status_code: int, code: str, detail: str) -> JSONResponse:
    return _json(
        {"ok": False, "error": code, "detail": detail},
        status_code=status_code,
    )


def _owner_context(request: Request) -> AccessContext | None:
    context = _context(request)
    if (
        context is None
        or not context.authenticated
        or not context.has_capability(AccessCapability.ACCESS_ADMIN)
    ):
        return None
    return context


def _origin_ok(request: Request, context: AccessContext | None) -> bool:
    return context is not None and request_origin_matches(context, request.scope)


def _is_json_request(request: Request) -> bool:
    return (
        request.headers.get("content-type", "").lower().startswith("application/json")
    )


async def _payload(request: Request) -> dict[str, Any]:
    if _is_json_request(request):
        try:
            value = await request.json()
        except Exception:
            return {}
        return dict(value) if isinstance(value, Mapping) else {}
    try:
        body = (await request.body()).decode("utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError):
        return {}
    parsed = parse_qs(body, keep_blank_values=True)
    return {name: values[-1] if values else "" for name, values in parsed.items()}


def _safe_script_string(value: str) -> str:
    return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e")


def _page_shell(title: str, content: str, *, script: str = "") -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{ color-scheme: dark; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }}
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: #111719; color: #edf5f5; }}
    main {{ width: min(92vw, 460px); padding: 28px; }}
    h1 {{ margin: 0 0 12px; font-size: 1.8rem; }}
    p {{ color: #b3c1c3; line-height: 1.5; }}
    .detail {{ color: #e5eeee; }}
    .warning {{ border-left: 3px solid #f6bf54; padding-left: 12px; }}
    label {{ display: block; margin: 20px 0 8px; }}
    input {{ box-sizing: border-box; width: 100%; padding: 12px; border: 1px solid #3a555a; border-radius: 8px; background: #172225; color: inherit; }}
    button {{ width: 100%; margin-top: 18px; padding: 13px; border: 0; border-radius: 8px; background: #00b6c7; color: #061114; font-weight: 700; }}
    code {{ color: #c6f5f8; }}
  </style>
  {script}
</head>
<body><main>{content}</main></body>
</html>"""


def _neutral_connect_page() -> str:
    return _page_shell(
        "Connect to Row-Bot",
        """
<h1>Connect to this Row-Bot</h1>
<p>This Row-Bot requires approval from its owner.</p>
<p class="detail">Open a one-time invitation link in this browser.</p>
<p>Owner of this server? Run <code>row-bot access invite --profile computer --origin &lt;address&gt;</code>.</p>
""",
    )


def _terminal_connect_page(reason: str) -> str:
    heading, detail = _TERMINAL_MESSAGES.get(
        reason, _TERMINAL_MESSAGES["invalid_invitation"]
    )
    return _page_shell(
        "Invitation unavailable",
        f"""
<h1>{escape(heading)}</h1>
<p>{escape(detail)}</p>
<p><a href="/connect">Return to the connection page</a></p>
""",
    )


def _available_connect_page(
    *,
    token: str,
    profile: AccessProfile,
    lifetime: SessionLifetime,
    next_path: str,
) -> str:
    full_access = profile is AccessProfile.OWNER
    access_label = "Full Row-Bot" if full_access else "Companion"
    duration = "30 days" if lifetime is SessionLifetime.TRUSTED else "12 hours"
    warning = (
        '<p class="warning">Full access can use the same files, tools, '
        "providers, and settings as the owner.</p>"
        if full_access
        else "<p>The companion has limited settings and tools.</p>"
    )
    script = f"""<script>
(() => {{
  const invitation = {_safe_script_string(token)};
  const clean = new URL(window.location.href);
  clean.searchParams.delete('invitation');
  window.history.replaceState(null, '', clean.pathname + clean.search + clean.hash);
  window.addEventListener('DOMContentLoaded', () => {{
    document.getElementById('invitation').value = invitation;
  }});
}})();
</script>"""
    return _page_shell(
        "Connect to Row-Bot",
        f"""
<h1>Connect this browser to Row-Bot?</h1>
<p class="detail">Access: {access_label}<br>Duration: {duration}</p>
{warning}
<form method="post" action="/api/access/invitations/claim">
  <input id="invitation" name="invitation" type="hidden" value="">
  <input name="next" type="hidden" value="{escape(next_path, quote=True)}">
  <label for="display_name">Device name</label>
  <input id="display_name" name="display_name" maxlength="80" autocomplete="nickname" placeholder="This browser">
  <button type="submit">Connect</button>
</form>
""",
        script=script,
    )


def _claim_status(reason: str) -> int:
    if reason == "origin_mismatch":
        return 403
    if reason in {"already_claimed", "immutable_mismatch"}:
        return 409
    if reason in {"expired", "cancelled"}:
        return 410
    if reason == "locked":
        return 429
    return 400


def _invitation_public(invitation) -> dict[str, Any]:
    return invitation.to_public_dict()


def _device_public(service: AccessService, device) -> dict[str, Any]:
    sessions = [
        session.to_public_dict()
        for session in service.list_sessions(
            device_id=device.id,
            include_revoked=True,
        )
    ]
    return {**device.to_public_dict(), "sessions": sessions}


class AccessSessionAuthenticator:
    """Adapt access cookies and :class:`AccessService` to access middleware."""

    def __init__(
        self,
        service: AccessService,
        cookies: AccessCookieManager,
    ) -> None:
        self.service = service
        self.cookies = cookies

    def authenticate_scope(
        self,
        scope: Mapping[str, object],
        provenance: RequestProvenance,
    ):
        mutable_scope = dict(scope)
        token = self.cookies.extract_from_scope(
            mutable_scope,
            context=provenance,
        )
        if token:
            session = self.service.validate_session(token)
            if session is not None:
                return session
        legacy = self.cookies.extract_legacy_from_scope(
            mutable_scope,
            context=provenance,
        )
        if legacy:
            return self.service.validate_legacy_session(legacy)
        return None


@dataclass(frozen=True, slots=True)
class AccessRouteRegistration:
    service: AccessService
    config: AccessConfig
    cookies: AccessCookieManager
    authenticator: AccessSessionAuthenticator


def build_access_router(
    *,
    service: AccessService,
    cookies: AccessCookieManager,
) -> APIRouter:
    router = APIRouter()

    async def connect_page(request: Request) -> HTMLResponse:
        token = str(request.query_params.get("invitation") or "").strip()
        if not token:
            return HTMLResponse(
                _neutral_connect_page(),
                headers=CONNECT_PAGE_HEADERS,
            )
        try:
            context = _context(request)
            inspection = service.inspect_invitation(
                token,
                effective_client=(
                    context.effective_client if context is not None else None
                ),
            )
        except InvitationClaimError:
            return HTMLResponse(
                _terminal_connect_page("invalid_invitation"),
                status_code=400,
                headers=CONNECT_PAGE_HEADERS,
            )
        if inspection.status != "available":
            return HTMLResponse(
                _terminal_connect_page(inspection.status),
                status_code=_claim_status(inspection.status),
                headers=CONNECT_PAGE_HEADERS,
            )
        next_path = safe_relative_next(request.query_params.get("next"))
        return HTMLResponse(
            _available_connect_page(
                token=token,
                profile=inspection.invitation.profile,
                lifetime=inspection.invitation.session_lifetime,
                next_path=next_path,
            ),
            headers=CONNECT_PAGE_HEADERS,
        )

    async def claim_invitation(request: Request) -> Response:
        context = _context(request)
        if context is None:
            return _error(
                503,
                "access_context_unavailable",
                "Access context is unavailable.",
            )
        if not _origin_ok(request, context):
            return _error(403, "origin_required", "Exact same origin is required.")
        payload = await _payload(request)
        token = str(payload.get("invitation") or "").strip()
        display_name = (
            str(payload.get("display_name") or "").strip() or "Connected browser"
        )
        next_path = safe_relative_next(payload.get("next"))
        try:
            claim = service.claim_invitation(
                token,
                intended_origin=context.origin,
                display_name=display_name,
                user_agent=request.headers.get("user-agent"),
                effective_client=context.effective_client,
            )
        except InvitationClaimError as exc:
            status = _claim_status(exc.reason)
            if _is_json_request(request):
                heading, detail = _TERMINAL_MESSAGES.get(
                    exc.reason,
                    _TERMINAL_MESSAGES["invalid_invitation"],
                )
                return _error(status, exc.reason, f"{heading} {detail}")
            return HTMLResponse(
                _terminal_connect_page(exc.reason),
                status_code=status,
                headers=CONNECT_PAGE_HEADERS,
            )
        if _is_json_request(request):
            response: Response = _json(
                {
                    "ok": True,
                    "authenticated": True,
                    "device": claim.device.to_public_dict(),
                    "session": claim.session.to_public_dict(),
                    "next": next_path,
                }
            )
        else:
            response = RedirectResponse(
                next_path,
                status_code=303,
                headers=ACCESS_RESPONSE_HEADERS,
            )
        cookies.set_session(
            response,
            claim.session_token,
            context=context,
            expires_at=claim.session.expires_at,
        )
        return response

    async def current_session(request: Request) -> JSONResponse:
        context = _context(request)
        if context is None or not context.authenticated:
            return _json(
                {
                    "ok": True,
                    "authenticated": False,
                    "profile": None,
                    "device_id": None,
                    "session_id": None,
                }
            )
        return _json(
            {
                "ok": True,
                "authenticated": True,
                "authentication_kind": context.authentication_kind.value,
                "profile": context.profile,
                "device_id": context.device_id,
                "session_id": context.session_id,
                "presentation": context.presentation.value,
            }
        )

    async def logout(request: Request) -> JSONResponse:
        context = _context(request)
        if context is None or not context.authenticated:
            response = _error(401, "authentication_required", "Sign in required.")
        else:
            revoked = bool(
                context.session_id and service.revoke_session(context.session_id)
            )
            response = _json({"ok": True, "revoked": revoked})
        cookies.clear(response)
        return response

    async def status(request: Request) -> JSONResponse:
        context = _owner_context(request)
        if context is None:
            return _error(403, "forbidden", "Owner access is required.")
        now = datetime.now(timezone.utc)
        devices = [
            device
            for device in service.list_devices(include_revoked=False)
            if device.revoked_at is None
        ]
        sessions = [
            session
            for session in service.list_sessions(include_revoked=False)
            if session.revoked_at is None and session.expires_at > now
        ]
        invitations = [
            invitation
            for invitation in service.list_invitations()
            if invitation.claimed_at is None
            and invitation.cancelled_at is None
            and invitation.expires_at > now
        ]
        return _json(
            {
                "ok": True,
                "deployment_mode": context.deployment_mode.value,
                "origin": context.origin,
                "devices": len(devices),
                "sessions": len(sessions),
                "active_invitations": len(invitations),
            }
        )

    async def create_invitation(request: Request) -> JSONResponse:
        context = _owner_context(request)
        if context is None:
            return _error(403, "forbidden", "Owner access is required.")
        if not _origin_ok(request, context):
            return _error(403, "origin_required", "Exact same origin is required.")
        payload = await _payload(request)
        profile_text = str(payload.get("profile") or "computer").strip().lower()
        if profile_text == "computer":
            profile_text = AccessProfile.OWNER.value
        lifetime_text = (
            str(payload.get("session_lifetime") or SessionLifetime.TRUSTED.value)
            .strip()
            .lower()
        )
        try:
            profile = AccessProfile(profile_text)
            lifetime = SessionLifetime(lifetime_text)
            if lifetime is SessionLifetime.MIGRATED:
                raise ValueError
        except ValueError:
            return _error(
                400,
                "invalid_invitation_options",
                "Choose computer or companion and a trusted or temporary session.",
            )
        intended_origin = str(payload.get("origin") or context.origin).strip()
        try:
            created = service.create_invitation(
                profile=profile,
                intended_origin=intended_origin,
                session_lifetime=lifetime,
                created_by=context.device_id or "local_owner",
                access_route=str(payload.get("access_route") or "")[:80] or None,
            )
        except ValueError:
            return _error(
                400,
                "invalid_origin",
                "Choose an exact HTTP or HTTPS Row-Bot origin.",
            )
        return _json(
            {
                "ok": True,
                "invitation": _invitation_public(created.invitation),
                "invitation_url": created.invitation_url(),
            },
            status_code=201,
        )

    async def list_invitations(request: Request) -> JSONResponse:
        if _owner_context(request) is None:
            return _error(403, "forbidden", "Owner access is required.")
        return _json(
            {
                "ok": True,
                "invitations": [
                    _invitation_public(invitation)
                    for invitation in service.list_invitations()
                ],
            }
        )

    async def cancel_invitation(request: Request) -> JSONResponse:
        context = _owner_context(request)
        if context is None:
            return _error(403, "forbidden", "Owner access is required.")
        if not _origin_ok(request, context):
            return _error(403, "origin_required", "Exact same origin is required.")
        invitation_id = str(request.path_params.get("invitation_id") or "")
        cancelled = service.cancel_invitation(invitation_id)
        return _json(
            {"ok": cancelled, "cancelled": cancelled},
            status_code=200 if cancelled else 404,
        )

    async def list_devices(request: Request) -> JSONResponse:
        if _owner_context(request) is None:
            return _error(403, "forbidden", "Owner access is required.")
        return _json(
            {
                "ok": True,
                "devices": [
                    _device_public(service, device)
                    for device in service.list_devices(include_revoked=True)
                ],
            }
        )

    async def revoke_device(request: Request) -> JSONResponse:
        context = _owner_context(request)
        if context is None:
            return _error(403, "forbidden", "Owner access is required.")
        if not _origin_ok(request, context):
            return _error(403, "origin_required", "Exact same origin is required.")
        device_id = str(request.path_params.get("device_id") or "")
        revoked = service.revoke_device(device_id)
        response = _json(
            {"ok": revoked, "revoked": revoked},
            status_code=200 if revoked else 404,
        )
        if context.device_id == device_id:
            cookies.clear(response)
        return response

    router.add_api_route("/connect", connect_page, methods=["GET"])
    router.add_api_route(
        "/api/access/invitations/claim",
        claim_invitation,
        methods=["POST"],
    )
    router.add_api_route("/api/access/session", current_session, methods=["GET"])
    router.add_api_route("/api/access/logout", logout, methods=["POST"])
    router.add_api_route("/api/access/status", status, methods=["GET"])
    router.add_api_route(
        "/api/access/invitations",
        create_invitation,
        methods=["POST"],
    )
    router.add_api_route(
        "/api/access/invitations",
        list_invitations,
        methods=["GET"],
    )
    router.add_api_route(
        "/api/access/invitations/{invitation_id}/cancel",
        cancel_invitation,
        methods=["POST"],
    )
    router.add_api_route("/api/access/devices", list_devices, methods=["GET"])
    router.add_api_route(
        "/api/access/devices/{device_id}/revoke",
        revoke_device,
        methods=["POST"],
    )
    return router


def register_access_routes(
    app,
    *,
    service: AccessService | None = None,
    config: AccessConfig | None = None,
    cookies: AccessCookieManager | None = None,
) -> AccessRouteRegistration:
    """Register access routes and return middleware-ready dependencies."""

    selected_service = service or AccessService()
    selected_config = config or AccessConfig.from_env()
    selected_cookies = cookies or AccessCookieManager(selected_service.instance_id)
    registration = AccessRouteRegistration(
        service=selected_service,
        config=selected_config,
        cookies=selected_cookies,
        authenticator=AccessSessionAuthenticator(
            selected_service,
            selected_cookies,
        ),
    )
    app.state.row_bot_access = registration
    app.state.row_bot_access_service = selected_service
    app.state.row_bot_access_cookie_manager = selected_cookies
    app.include_router(
        build_access_router(
            service=selected_service,
            cookies=selected_cookies,
        )
    )
    return registration


__all__ = [
    "ACCESS_RESPONSE_HEADERS",
    "AccessRouteRegistration",
    "AccessSessionAuthenticator",
    "build_access_router",
    "register_access_routes",
]
