"""HTTP routes for Row-Bot mobile pairing and session management."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from row_bot.access.models import SessionLifetime
from row_bot.access.request_context import ACCESS_CONTEXT_SCOPE_KEY, AccessContext
from row_bot.access.service import AccessService
from row_bot.brand import APP_BRAND_ACCENT, APP_DISPLAY_NAME
from row_bot.mobile.store import MobileAuthStore

FORWARDED_HEADERS = {
    "forwarded",
    "x-forwarded-for",
    "x-real-ip",
    "x-client-ip",
    "x-forwarded-host",
    "x-forwarded-proto",
}

PAIRING_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "X-Robots-Tag": "noindex",
}


def _access_context(request: Request) -> AccessContext | None:
    context = request.scope.get(ACCESS_CONTEXT_SCOPE_KEY)
    if isinstance(context, AccessContext):
        return context
    state_context = getattr(request.state, "row_bot_access_context", None)
    return state_context if isinstance(state_context, AccessContext) else None


def _client_ip(request: Request) -> str:
    context = _access_context(request)
    return context.effective_client if context is not None else ""


def is_true_local_request(request: Request) -> bool:
    """Compatibility helper backed by the central transport decision."""
    context = _access_context(request)
    return bool(context and context.direct_loopback and not context.forwarding_headers)


def _safe_device(device) -> dict[str, Any]:
    return device.to_public_dict()


def _json_error(status_code: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": code, "detail": detail}, status_code=status_code
    )


def _request_origin(request: Request) -> str:
    context = _access_context(request)
    return context.origin if context is not None else ""


def _mobile_store(request: Request) -> MobileAuthStore:
    store = getattr(request.app.state, "row_bot_mobile_store", None)
    if store is None:
        store = MobileAuthStore()
        request.app.state.row_bot_mobile_store = store
    return store


def _access_service(request: Request) -> AccessService:
    service = getattr(request.app.state, "row_bot_access_service", None)
    if isinstance(service, AccessService):
        return service
    return AccessService(_mobile_store(request).access_store)


def _current_device(request: Request) -> Any | None:
    context = _access_context(request)
    if context is None or context.device_id is None:
        return None
    return _access_service(request).store.get_device(context.device_id)


def _can_manage_mobile_access(request: Request) -> bool:
    context = _access_context(request)
    return bool(context and context.authenticated)


async def mobile_pair_page(request: Request) -> RedirectResponse:
    """Redirect legacy links to the neutral, POST-to-claim connect flow."""
    token = str(request.query_params.get("code") or "").strip()
    if token.startswith("rbp_"):
        token = f"rbi_{token[4:]}"
    location = "/connect"
    if token:
        location = f"{location}?invitation={quote(token, safe='')}"
        next_path = str(request.query_params.get("next") or "").strip()
        if next_path:
            location = f"{location}&next={quote(next_path, safe='')}"
    return RedirectResponse(
        location,
        status_code=303,
        headers=PAIRING_PAGE_HEADERS,
    )


async def mobile_manifest(request: Request) -> JSONResponse:  # noqa: ARG001
    return JSONResponse(
        {
            "name": f"{APP_DISPLAY_NAME} Mobile",
            "short_name": APP_DISPLAY_NAME,
            "description": "Local-first compact owner access to your running Row-Bot host.",
            "start_url": "/?mobile=1",
            "scope": "/",
            "display": "standalone",
            "background_color": "#111719",
            "theme_color": APP_BRAND_ACCENT,
            "icons": [
                {
                    "src": "/static/row_bot_glyph_256.png",
                    "sizes": "256x256",
                    "type": "image/png",
                    "purpose": "any maskable",
                }
            ],
        },
        media_type="application/manifest+json",
    )


async def mobile_offline(request: Request) -> HTMLResponse:  # noqa: ARG001
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{APP_DISPLAY_NAME} unavailable</title>
  <style>
    :root {{ color-scheme: dark; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: #111719; color: #edf5f5; }}
    main {{ width: min(90vw, 440px); padding: 24px; text-align: center; }}
    img {{ width: 96px; height: 96px; object-fit: contain; }}
    h1 {{ font-size: 1.6rem; }}
    p {{ color: #a9b8ba; line-height: 1.45; }}
  </style>
</head>
<body>
  <main>
    <img src="/static/row_bot_glyph_256.png" alt="">
    <h1>{APP_DISPLAY_NAME} is not reachable</h1>
    <p>Your desktop Row-Bot host needs to be awake, running, and reachable from this network or private access path.</p>
  </main>
</body>
</html>"""
    )


async def mobile_service_worker(request: Request) -> PlainTextResponse:  # noqa: ARG001
    body = """const CACHE_NAME = 'row-bot-mobile-shell-v2';
const SHELL_ASSETS = ['/mobile/offline', '/static/row_bot_glyph_256.png'];
const PRIVATE_PREFIXES = ['/api/', '/_media', '/published', '/_buddy', '/_nicegui_ws', '/_nicegui/'];
const BYPASS_PATHS = ['/mobile/pair', '/mobile/service-worker.js'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS)));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))))
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (BYPASS_PATHS.includes(url.pathname)) return;
  if (PRIVATE_PREFIXES.some((prefix) => url.pathname.startsWith(prefix))) return;
  if (request.mode === 'navigate') {
    event.respondWith(fetch(request).catch(() => caches.match('/mobile/offline')));
    return;
  }
  if (SHELL_ASSETS.includes(url.pathname)) {
    event.respondWith(caches.match(request).then((cached) => cached || fetch(request)));
  }
});
"""
    return PlainTextResponse(
        body,
        media_type="text/javascript; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


async def mobile_pair_start(request: Request) -> JSONResponse:
    context = _access_context(request)
    if not _can_manage_mobile_access(request) or context is None:
        return _json_error(
            403,
            "forbidden",
            "Owner access is required to create an invitation.",
        )
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    origin = str(payload.get("intended_origin") or _request_origin(request)).rstrip("/")
    access_mode = str(payload.get("access_mode") or "localhost").strip()[:80]
    try:
        created = _access_service(request).create_invitation(
            intended_origin=origin,
            session_lifetime=SessionLifetime.TRUSTED,
            next_path="/?mobile=1",
            created_by=context.device_id or "local_owner",
            access_route=access_mode,
        )
    except ValueError:
        return _json_error(400, "invalid_origin", "Choose a canonical origin.")
    return JSONResponse(
        {
            "ok": True,
            "pairing": {
                "id": created.invitation.id,
                "expires_at": created.invitation.expires_at.isoformat(),
                "pairing_url": created.invitation_url(),
                "intended_origin": created.invitation.intended_origin,
                "access_mode": created.invitation.access_route,
            },
        }
    )


async def mobile_pair_confirm(request: Request) -> Response:
    """Never claim through the legacy endpoint; continue via `/connect`."""
    content_type = request.headers.get("content-type", "")
    is_json_request = content_type.startswith("application/json")
    if is_json_request:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
    else:
        try:
            form = await request.form()
            payload = dict(form)
        except Exception:
            payload = {}
    code = str(payload.get("code") or request.query_params.get("code") or "")
    if code.startswith("rbp_"):
        code = f"rbi_{code[4:]}"
    location = f"/connect?invitation={quote(code, safe='')}" if code else "/connect"
    if is_json_request:
        return JSONResponse(
            {
                "ok": False,
                "error": "connect_flow_required",
                "connect_url": location,
            },
            status_code=409,
            headers=PAIRING_PAGE_HEADERS,
        )
    return RedirectResponse(location, status_code=303, headers=PAIRING_PAGE_HEADERS)


async def mobile_session(request: Request) -> JSONResponse:
    context = _access_context(request)
    device = _current_device(request)
    if context is None or not context.authenticated:
        return JSONResponse({"ok": True, "authenticated": False, "device": None})
    return JSONResponse(
        {
            "ok": True,
            "authenticated": True,
            "presentation": context.presentation.value,
            "device": _safe_device(device) if device is not None else None,
        }
    )


async def mobile_devices(request: Request) -> JSONResponse:
    if not _can_manage_mobile_access(request):
        return _json_error(
            403,
            "forbidden",
            "Mobile device management requires localhost or a paired settings session.",
        )
    devices = [
        device.to_public_dict()
        for device in _access_service(request).list_devices(include_revoked=True)
    ]
    return JSONResponse({"ok": True, "devices": devices})


async def mobile_revoke_device(request: Request) -> JSONResponse:
    if not _can_manage_mobile_access(request):
        return _json_error(
            403,
            "forbidden",
            "Mobile device management requires localhost or a paired settings session.",
        )
    device_id = request.path_params.get("device_id", "")
    service = _access_service(request)
    revoked = service.revoke_device(device_id)
    response = JSONResponse({"ok": bool(revoked), "revoked": bool(revoked)})
    context = _access_context(request)
    if context and context.device_id == device_id:
        cookies = getattr(
            request.app.state,
            "row_bot_access_cookie_manager",
            None,
        )
        if cookies is not None:
            cookies.clear(response)
    return response


async def mobile_access_events(request: Request) -> JSONResponse:
    if not _can_manage_mobile_access(request):
        return _json_error(
            403,
            "forbidden",
            "Mobile access events require localhost or a paired settings session.",
        )
    events = [
        event.to_public_dict()
        for event in _access_service(request).store.recent_events(limit=50)
    ]
    return JSONResponse({"ok": True, "events": events})


def build_mobile_router() -> APIRouter:
    router = APIRouter()
    router.add_api_route("/mobile/pair", mobile_pair_page, methods=["GET"])
    router.add_api_route(
        "/mobile/manifest.webmanifest", mobile_manifest, methods=["GET"]
    )
    router.add_api_route("/mobile/offline", mobile_offline, methods=["GET"])
    router.add_api_route(
        "/mobile/service-worker.js", mobile_service_worker, methods=["GET"]
    )
    router.add_api_route("/api/mobile/pair/start", mobile_pair_start, methods=["POST"])
    router.add_api_route(
        "/api/mobile/pair/confirm", mobile_pair_confirm, methods=["POST"]
    )
    router.add_api_route("/api/mobile/session", mobile_session, methods=["GET"])
    router.add_api_route("/api/mobile/devices", mobile_devices, methods=["GET"])
    router.add_api_route(
        "/api/mobile/devices/{device_id}/revoke", mobile_revoke_device, methods=["POST"]
    )
    router.add_api_route(
        "/api/mobile/access-events", mobile_access_events, methods=["GET"]
    )
    return router


def register_mobile_routes(app, *, store: MobileAuthStore | None = None) -> None:
    """Register mobile routes on a FastAPI/NiceGUI app."""
    if store is not None:
        app.state.row_bot_mobile_store = store
    app.include_router(build_mobile_router())
