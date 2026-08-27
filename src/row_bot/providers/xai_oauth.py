from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import row_bot.secret_store as secret_store

from row_bot.providers.models import AuthMethod, ModelInfo, ModelModality, ModelTask, ProviderHealth, TransportMode
from row_bot.providers.oauth import OAuthToken, expiry_from_seconds
from row_bot.providers.xai_catalog import (
    is_hidden_xai_model,
    merge_xai_curated_chat_extras,
    merged_xai_model_entries,
    xai_generation_parameters_from_item,
)

XAI_OAUTH_PROVIDER_ID = "xai_oauth"
XAI_OAUTH_BASE_URL = "https://api.x.ai/v1"
XAI_OAUTH_ISSUER = "https://auth.x.ai"
XAI_OAUTH_DISCOVERY_URL = "https://auth.x.ai/.well-known/openid-configuration"
XAI_OAUTH_REDIRECT_HOST = "127.0.0.1"
XAI_OAUTH_REDIRECT_PORT = 56121
XAI_OAUTH_REDIRECT_PATH = "/callback"
XAI_OAUTH_TIMEOUT_SECONDS = 15 * 60

ROW_BOT_XAI_OAUTH_CLIENT_ID_ENV = "ROW_BOT_XAI_OAUTH_CLIENT_ID"
ROW_BOT_XAI_OAUTH_SCOPES_ENV = "ROW_BOT_XAI_OAUTH_SCOPES"
ROW_BOT_XAI_OAUTH_REDIRECT_PORT_ENV = "ROW_BOT_XAI_OAUTH_REDIRECT_PORT"
DEFAULT_XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
DEFAULT_XAI_OAUTH_SCOPES = ("openid", "profile", "email", "offline_access", "grok-cli:access", "api:access")
XAI_OAUTH_AUTHORIZE_PLAN = "generic"
XAI_OAUTH_REFERRER = "row-bot"
XAI_OAUTH_VISION_PROBE_VERSION = "responses-json-probe-all-models-v5"
XAI_OAUTH_CLIENT_ID_PLACEHOLDERS = frozenset({
    "",
    "row-bot",
    "row_bot",
    "rowbot",
    "client-id",
    "example-client-id",
    "your-client-id",
    "your-xai-client-id",
})

XAI_OAUTH_PROVIDER_ALIASES = {
    "xai_oauth",
    "xai-oauth",
    "grok-oauth",
    "grok_oauth",
    "x-ai-oauth",
    "x_ai_oauth",
    "xai-grok-oauth",
    "xai_grok_oauth",
}

_SENSITIVE_MARKERS = (
    "access_token",
    "authorization",
    "bearer",
    "code=",
    "id_token",
    "refresh_token",
    "token",
)


class XAIOAuthError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 0, kind: str = "") -> None:
        super().__init__(message)
        self.status_code = int(status_code or 0)
        self.kind = kind


@dataclass(frozen=True)
class XAIOAuthFlow:
    authorization_url: str
    code_verifier: str
    code_challenge: str
    state: str
    nonce: str
    redirect_uri: str
    expires_at: str
    authorization_endpoint: str
    token_endpoint: str
    client_id: str
    scopes: tuple[str, ...] = DEFAULT_XAI_OAUTH_SCOPES


@dataclass(frozen=True)
class XAIOAuthCallback:
    code: str = ""
    state: str = ""
    error: str = ""
    error_description: str = ""


@dataclass(frozen=True)
class XAIOAuthAuthorization:
    authorization_code: str
    code_verifier: str
    redirect_uri: str
    token_endpoint: str
    client_id: str
    code_challenge: str = ""
    code_challenge_method: str = "S256"
    state: str = ""
    nonce: str = ""


@dataclass(frozen=True)
class XAIOAuthTokenSet:
    access_token: str
    refresh_token: str = ""
    id_token: str = ""
    expires_at: str = ""
    user_id: str = ""
    account_id: str = ""
    email_hash: str = ""
    scopes: tuple[str, ...] = ()

    def oauth_token(self) -> OAuthToken:
        return OAuthToken(
            access_token=self.access_token,
            refresh_token=self.refresh_token,
            expires_at=self.expires_at,
            scopes=self.scopes,
        )


@dataclass(frozen=True)
class XAIOAuthTokenHealth:
    status: str
    detail: str
    credentials: XAIOAuthTokenSet = XAIOAuthTokenSet(access_token="")

    @property
    def runnable(self) -> bool:
        return self.status in {"valid", "refreshed"} and bool(self.credentials.access_token)


def normalize_xai_oauth_provider_id(value: str) -> str:
    raw = str(value or "").strip()
    normalized = raw.lower().replace("-", "_")
    if raw.lower() in XAI_OAUTH_PROVIDER_ALIASES or normalized in XAI_OAUTH_PROVIDER_ALIASES:
        return XAI_OAUTH_PROVIDER_ID
    return raw


def xai_oauth_redirect_uri(port: int | None = None) -> str:
    resolved_port = int(port or _env_int(ROW_BOT_XAI_OAUTH_REDIRECT_PORT_ENV, XAI_OAUTH_REDIRECT_PORT))
    return f"http://{XAI_OAUTH_REDIRECT_HOST}:{resolved_port}{XAI_OAUTH_REDIRECT_PATH}"


def xai_oauth_user_agent() -> str:
    try:
        from row_bot.version import __version__
    except Exception:
        __version__ = "0"
    return f"Row-Bot/{__version__}"


def xai_oauth_configured_client_id(value: str | None = None) -> str:
    """Return the configured xAI OAuth client id, or an empty string when absent."""
    return _resolve_xai_oauth_client_id(value, require=False)[0]


def xai_oauth_default_client_id() -> str:
    """Return Row-Bot's built-in shared xAI OAuth client id, when available."""
    default_value = str(DEFAULT_XAI_OAUTH_CLIENT_ID or "").strip()
    if default_value and not _is_placeholder_xai_oauth_client_id(default_value):
        return default_value
    return ""


def xai_oauth_saved_client_id_override() -> str:
    """Return a saved user override for the xAI OAuth client id, if one exists."""
    try:
        from row_bot.providers.config import load_provider_config

        entry = load_provider_config().get("providers", {}).get(XAI_OAUTH_PROVIDER_ID, {})
    except Exception:
        return ""
    if not isinstance(entry, dict):
        return ""
    return _client_id_override_from_entry(entry)


def xai_oauth_client_id_status(value: str | None = None) -> dict[str, Any]:
    client_id, source, detail = _resolve_xai_oauth_client_id(value, require=False)
    override = xai_oauth_saved_client_id_override()
    default_client_id = xai_oauth_default_client_id()
    return {
        "configured": bool(client_id),
        "source": source,
        "fingerprint": secret_store.fingerprint(client_id) if client_id else "",
        "detail": detail,
        "env_var": ROW_BOT_XAI_OAUTH_CLIENT_ID_ENV,
        "default_configured": bool(default_client_id),
        "default_fingerprint": secret_store.fingerprint(default_client_id) if default_client_id else "",
        "override_configured": bool(override),
        "override_fingerprint": secret_store.fingerprint(override) if override else "",
    }


def save_xai_oauth_client_id(client_id: str) -> dict[str, Any]:
    """Persist a user-provided, non-secret xAI OAuth client id in provider config."""
    from row_bot.providers.config import update_provider_config

    resolved_client_id, _, _ = _resolve_xai_oauth_client_id(client_id, require=True)
    now = _utcnow().isoformat()
    fingerprint = secret_store.fingerprint(resolved_client_id)

    def _update(cfg: dict[str, Any]) -> None:
        entry = cfg.setdefault("providers", {}).setdefault(XAI_OAUTH_PROVIDER_ID, {})
        entry.update({
            "provider_id": XAI_OAUTH_PROVIDER_ID,
            "auth_method": AuthMethod.OAUTH_PKCE.value,
            "oauth_client_id": resolved_client_id,
            "oauth_client_id_configured": True,
            "oauth_client_id_source": "override",
            "oauth_client_id_fingerprint": fingerprint,
            "oauth_client_id_updated_at": now,
            "last_error": "",
        })
        entry.setdefault("configured", False)

    cfg = update_provider_config(_update)
    return dict(cfg.get("providers", {}).get(XAI_OAUTH_PROVIDER_ID, {}))


def clear_xai_oauth_client_id_override() -> dict[str, Any]:
    """Clear the saved xAI OAuth client id override and return to the built-in default."""
    from row_bot.providers.config import update_provider_config

    now = _utcnow().isoformat()

    def _update(cfg: dict[str, Any]) -> None:
        entry = cfg.setdefault("providers", {}).setdefault(XAI_OAUTH_PROVIDER_ID, {})
        entry["provider_id"] = XAI_OAUTH_PROVIDER_ID
        entry["auth_method"] = AuthMethod.OAUTH_PKCE.value
        for key in (
            "oauth_client_id",
            "client_id",
            "oauth_client_id_source",
            "oauth_client_id_fingerprint",
            "oauth_client_id_updated_at",
        ):
            entry.pop(key, None)
        entry["oauth_client_id_override_cleared_at"] = now
        entry.setdefault("configured", False)

    cfg = update_provider_config(_update)
    return dict(cfg.get("providers", {}).get(XAI_OAUTH_PROVIDER_ID, {}))


def start_xai_oauth_flow(
    *,
    http_client: Any | None = None,
    client_id: str | None = None,
    scopes: tuple[str, ...] | list[str] | str | None = None,
    redirect_uri: str | None = None,
    discovery_url: str = XAI_OAUTH_DISCOVERY_URL,
) -> XAIOAuthFlow:
    resolved_client_id = _xai_oauth_client_id(client_id)
    discovery = fetch_xai_oauth_discovery(http_client=http_client, discovery_url=discovery_url)
    verifier, challenge = _new_pkce_pair()
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(24)
    resolved_scopes = _xai_oauth_scopes(scopes)
    resolved_redirect = redirect_uri or xai_oauth_redirect_uri()
    query = {
        "response_type": "code",
        "client_id": resolved_client_id,
        "redirect_uri": resolved_redirect,
        "scope": " ".join(resolved_scopes),
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "plan": XAI_OAUTH_AUTHORIZE_PLAN,
        "referrer": XAI_OAUTH_REFERRER,
    }
    authorization_endpoint = str(discovery["authorization_endpoint"])
    return XAIOAuthFlow(
        authorization_url=f"{authorization_endpoint}?{urlencode(query)}",
        code_verifier=verifier,
        code_challenge=challenge,
        state=state,
        nonce=nonce,
        redirect_uri=resolved_redirect,
        expires_at=expiry_from_seconds(XAI_OAUTH_TIMEOUT_SECONDS),
        authorization_endpoint=authorization_endpoint,
        token_endpoint=str(discovery["token_endpoint"]),
        client_id=resolved_client_id,
        scopes=resolved_scopes,
    )


def parse_xai_oauth_callback(
    value: str,
    *,
    expected_state: str = "",
    allow_bare_code: bool = True,
) -> XAIOAuthCallback:
    raw = str(value or "").strip()
    if not raw:
        raise XAIOAuthError("xAI OAuth callback was empty.")

    query_values: dict[str, list[str]] = {}
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc:
        query_values = parse_qs(parsed.query)
        if parsed.fragment:
            fragment_values = parse_qs(parsed.fragment)
            for key, values in fragment_values.items():
                query_values.setdefault(key, values)
    elif raw.startswith("?") or raw.startswith("#") or "=" in raw:
        query_values = parse_qs(raw.lstrip("?#"))
    elif allow_bare_code:
        return XAIOAuthCallback(code=raw)

    code = _first_query_value(query_values, "code")
    state = _first_query_value(query_values, "state")
    error = _first_query_value(query_values, "error")
    error_description = _first_query_value(query_values, "error_description")
    if expected_state and state and state != expected_state:
        raise XAIOAuthError("xAI OAuth callback state did not match the active login request.")
    if expected_state and code and not state:
        raise XAIOAuthError("xAI OAuth callback did not include state.")
    if error:
        return XAIOAuthCallback(state=state, error=error, error_description=error_description)
    if not code:
        raise XAIOAuthError("xAI OAuth callback did not include a code or error.")
    return XAIOAuthCallback(code=code, state=state)


def authorization_from_xai_oauth_callback(flow: XAIOAuthFlow, callback_value: str) -> XAIOAuthAuthorization:
    callback = parse_xai_oauth_callback(callback_value, expected_state=flow.state)
    if callback.error:
        detail = f": {callback.error_description}" if callback.error_description else ""
        raise XAIOAuthError(f"xAI OAuth authorization failed: {callback.error}{detail}", kind="authorization_failed")
    return XAIOAuthAuthorization(
        authorization_code=callback.code,
        code_verifier=flow.code_verifier,
        redirect_uri=flow.redirect_uri,
        token_endpoint=flow.token_endpoint,
        client_id=flow.client_id,
        code_challenge=flow.code_challenge,
        code_challenge_method="S256",
        state=flow.state,
        nonce=flow.nonce,
    )


def wait_for_xai_oauth_loopback_authorization(
    flow: XAIOAuthFlow,
    *,
    open_browser: bool = True,
    browser_open: Any | None = None,
    ready_callback: Any | None = None,
    cancel_event: Any | None = None,
    timeout_seconds: float | None = None,
) -> XAIOAuthAuthorization:
    """Wait for one xAI OAuth loopback callback and return a validated authorization."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import TCPServer
    import html
    import webbrowser

    parsed_redirect = urlparse(flow.redirect_uri)
    host = parsed_redirect.hostname or ""
    port = int(parsed_redirect.port or 0)
    path = parsed_redirect.path or ""
    if host != XAI_OAUTH_REDIRECT_HOST or path != XAI_OAUTH_REDIRECT_PATH or not port:
        raise XAIOAuthError("xAI OAuth loopback redirect URI must use 127.0.0.1 and /callback.", kind="loopback_invalid_redirect")

    result: dict[str, Any] = {}
    redirect_base = f"http://{XAI_OAUTH_REDIRECT_HOST}:{port}{XAI_OAUTH_REDIRECT_PATH}"

    def _write_page(handler: BaseHTTPRequestHandler, status_code: int, title: str, detail: str) -> None:
        body = (
            "<!doctype html><html><head><meta charset=\"utf-8\"><title>"
            f"{html.escape(title)}</title></head><body>"
            f"<h1>{html.escape(title)}</h1><p>{html.escape(detail)}</p>"
            "</body></html>"
        ).encode("utf-8")
        handler.send_response(status_code)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    class _CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
            return

        def do_GET(self) -> None:  # noqa: N802 - stdlib callback
            request = urlparse(self.path)
            if request.path != XAI_OAUTH_REDIRECT_PATH:
                result["error"] = XAIOAuthError(
                    "xAI OAuth callback arrived on an unexpected path.",
                    kind="loopback_bad_path",
                )
                _write_page(self, 404, "xAI Grok sign-in failed", "This callback path is not accepted by Row-Bot.")
                return
            callback_value = f"{redirect_base}?{request.query}" if request.query else redirect_base
            try:
                result["authorization"] = authorization_from_xai_oauth_callback(flow, callback_value)
            except XAIOAuthError as exc:
                result["error"] = exc
                _write_page(self, 400, "xAI Grok sign-in failed", str(exc))
                return
            _write_page(self, 200, "xAI Grok connected", "You can close this browser tab and return to Row-Bot.")

    class _LoopbackHTTPServer(HTTPServer):
        def server_bind(self) -> None:
            TCPServer.server_bind(self)
            host, port = self.socket.getsockname()[:2]
            self.server_name = str(host)
            self.server_port = int(port)

    try:
        server = _LoopbackHTTPServer((XAI_OAUTH_REDIRECT_HOST, port), _CallbackHandler)
    except OSError as exc:
        raise XAIOAuthError(
            f"xAI OAuth callback port {port} is unavailable. Close the process using it and try again.",
            kind="loopback_port_unavailable",
        ) from exc

    timeout = float(timeout_seconds or XAI_OAUTH_TIMEOUT_SECONDS)
    deadline = time.monotonic() + timeout
    try:
        if callable(ready_callback):
            ready_callback()
        if open_browser:
            opener = browser_open or webbrowser.open
            try:
                opener(flow.authorization_url)
            except Exception as exc:
                raise XAIOAuthError(f"Could not open xAI Grok sign-in page: {exc}", kind="browser_open_failed") from exc
        while time.monotonic() < deadline:
            if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
                raise XAIOAuthError("xAI OAuth loopback sign-in was cancelled.", kind="loopback_cancelled")
            remaining = max(0.0, deadline - time.monotonic())
            server.timeout = min(remaining, 0.25) if cancel_event is not None else remaining
            server.handle_request()
            if result.get("authorization") is not None or result.get("error") is not None:
                break
    finally:
        server.server_close()

    if isinstance(result.get("authorization"), XAIOAuthAuthorization):
        return result["authorization"]
    if isinstance(result.get("error"), XAIOAuthError):
        raise result["error"]
    raise XAIOAuthError("Timed out waiting for xAI Grok browser sign-in callback.", kind="loopback_timeout")


def exchange_xai_oauth_authorization(
    authorization: XAIOAuthAuthorization,
    *,
    http_client: Any | None = None,
) -> XAIOAuthTokenSet:
    client = http_client or _new_http_client()
    owns_client = http_client is None
    try:
        response = client.post(
            authorization.token_endpoint,
            data={
                "grant_type": "authorization_code",
                "client_id": authorization.client_id,
                "code": authorization.authorization_code,
                "code_verifier": authorization.code_verifier,
                "code_challenge": authorization.code_challenge,
                "code_challenge_method": authorization.code_challenge_method or "S256",
                "redirect_uri": authorization.redirect_uri,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": xai_oauth_user_agent(),
            },
        )
    finally:
        if owns_client:
            client.close()

    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code < 200 or status_code >= 300:
        raise XAIOAuthError(
            f"xAI OAuth token exchange failed with HTTP {status_code}: {_safe_error_body(response)}",
            status_code=status_code,
            kind="exchange_failed",
        )
    return _token_set_from_payload(_json_response(response))


def refresh_xai_oauth_token(
    refresh_token: str,
    *,
    http_client: Any | None = None,
    token_endpoint: str | None = None,
    client_id: str | None = None,
) -> XAIOAuthTokenSet:
    token = str(refresh_token or "").strip()
    if not token:
        raise XAIOAuthError("xAI OAuth refresh token is missing.", kind="missing_refresh_token")
    endpoint = token_endpoint or _configured_token_endpoint()
    resolved_client_id = _xai_oauth_client_id(client_id)
    client = http_client or _new_http_client()
    owns_client = http_client is None
    try:
        response = client.post(
            endpoint,
            data={
                "grant_type": "refresh_token",
                "refresh_token": token,
                "client_id": resolved_client_id,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": xai_oauth_user_agent(),
            },
        )
    finally:
        if owns_client:
            client.close()

    status_code = int(getattr(response, "status_code", 0) or 0)
    safe_body = _safe_error_body(response)
    lowered = safe_body.lower()
    if status_code in {400, 401} or "invalid_grant" in lowered:
        _mark_xai_oauth_requires_reconnect(
            "xAI OAuth sign-in expired or was revoked. Reconnect xAI Grok in Settings -> Providers."
        )
        raise XAIOAuthError(
            "xAI OAuth sign-in expired or was revoked. Reconnect xAI Grok in Settings -> Providers.",
            status_code=status_code,
            kind="reconnect_required",
        )
    if status_code == 403:
        _mark_xai_oauth_entitlement_denied(
            "xAI OAuth account is connected, but it is not authorized for xAI model access."
        )
        raise XAIOAuthError(
            "xAI OAuth account is connected, but it is not authorized for xAI model access.",
            status_code=status_code,
            kind="entitlement_denied",
        )
    if status_code < 200 or status_code >= 300:
        raise XAIOAuthError(
            f"xAI OAuth token refresh failed with HTTP {status_code}: {safe_body}",
            status_code=status_code,
            kind="transient_refresh_failure",
        )
    payload = _json_response(response)
    if not payload.get("refresh_token"):
        payload["refresh_token"] = token
    return _token_set_from_payload(payload, fallback_refresh_token=token)


def save_xai_oauth_tokens(token_set: XAIOAuthTokenSet) -> dict[str, Any]:
    from row_bot.providers.auth_store import set_provider_secret
    from row_bot.providers.config import update_provider_config

    if not token_set.access_token:
        raise XAIOAuthError("xAI OAuth token response did not include an access token.", kind="missing_access_token")
    set_provider_secret(
        XAI_OAUTH_PROVIDER_ID,
        "access_token",
        token_set.access_token,
        source=AuthMethod.OAUTH_PKCE.value,
        auth_method=AuthMethod.OAUTH_PKCE,
    )
    if token_set.refresh_token:
        set_provider_secret(
            XAI_OAUTH_PROVIDER_ID,
            "refresh_token",
            token_set.refresh_token,
            source=AuthMethod.OAUTH_PKCE.value,
            auth_method=AuthMethod.OAUTH_PKCE,
        )
    if token_set.id_token:
        set_provider_secret(
            XAI_OAUTH_PROVIDER_ID,
            "id_token",
            token_set.id_token,
            source=AuthMethod.OAUTH_PKCE.value,
            auth_method=AuthMethod.OAUTH_PKCE,
        )
    if token_set.user_id:
        set_provider_secret(
            XAI_OAUTH_PROVIDER_ID,
            "user_id",
            token_set.user_id,
            source=AuthMethod.OAUTH_PKCE.value,
            auth_method=AuthMethod.OAUTH_PKCE,
        )
    if token_set.account_id:
        set_provider_secret(
            XAI_OAUTH_PROVIDER_ID,
            "account",
            token_set.account_id,
            source=AuthMethod.OAUTH_PKCE.value,
            auth_method=AuthMethod.OAUTH_PKCE,
        )

    token_metadata = xai_oauth_token_metadata(token_set.access_token, token_set.id_token)
    client_status = xai_oauth_client_id_status()
    client_override = xai_oauth_saved_client_id_override()
    fingerprint = secret_store.fingerprint(token_set.access_token)
    now = _utcnow().isoformat()
    scopes = token_set.scopes or tuple(token_metadata.get("scopes") or ())

    def _update(cfg: dict[str, Any]) -> None:
        entry = cfg.setdefault("providers", {}).setdefault(XAI_OAUTH_PROVIDER_ID, {})
        entry.update({
            "provider_id": XAI_OAUTH_PROVIDER_ID,
            "auth_method": AuthMethod.OAUTH_PKCE.value,
            "configured": True,
            "health": ProviderHealth.CONNECTED.value,
            "source": AuthMethod.OAUTH_PKCE.value,
            "fingerprint": fingerprint,
            "expires_at": token_set.expires_at or str(token_metadata.get("expires_at") or ""),
            "user_hash": token_metadata.get("user_hash") or "",
            "account_id_hash": token_metadata.get("account_id_hash") or "",
            "email_hash": token_set.email_hash or token_metadata.get("email_hash") or "",
            "scope": " ".join(scopes),
            "scopes": list(scopes),
            "base_url": xai_oauth_base_url(),
            "oauth_client_id_configured": bool(client_status.get("configured")),
            "oauth_client_id_source": str(client_status.get("source") or ""),
            "oauth_client_id_fingerprint": str(client_status.get("fingerprint") or ""),
            "updated_at": now,
            "last_error": "",
        })
        if client_override:
            entry["oauth_client_id"] = client_override
            entry["oauth_client_id_source"] = "override"
            entry["oauth_client_id_fingerprint"] = secret_store.fingerprint(client_override)
        else:
            entry.pop("oauth_client_id", None)
            entry.pop("client_id", None)
        entry.pop("last_runtime_probe", None)

    cfg = update_provider_config(_update)
    return dict(cfg.get("providers", {}).get(XAI_OAUTH_PROVIDER_ID, {}))


def disconnect_xai_oauth_metadata(*, remove_row_bot_tokens: bool = True) -> None:
    from row_bot.providers.auth_store import delete_provider_secret
    from row_bot.providers.config import update_provider_config

    if remove_row_bot_tokens:
        for credential_name in ("access_token", "refresh_token", "id_token", "user_id", "account"):
            delete_provider_secret(XAI_OAUTH_PROVIDER_ID, credential_name)

    def _update(cfg: dict[str, Any]) -> None:
        providers = cfg.setdefault("providers", {})
        existing = providers.get(XAI_OAUTH_PROVIDER_ID, {}) if isinstance(providers.get(XAI_OAUTH_PROVIDER_ID), dict) else {}
        override = _client_id_override_from_entry(existing)
        preserved = {
            key: existing.get(key)
            for key in (
                "oauth_client_id",
                "oauth_client_id_configured",
                "oauth_client_id_source",
                "oauth_client_id_fingerprint",
                "oauth_client_id_updated_at",
            )
            if existing.get(key)
        } if override else {}
        if override:
            preserved.update({
                "oauth_client_id": override,
                "oauth_client_id_configured": True,
                "oauth_client_id_source": "override",
                "oauth_client_id_fingerprint": secret_store.fingerprint(override),
            })
        providers.pop(XAI_OAUTH_PROVIDER_ID, None)
        if preserved:
            providers[XAI_OAUTH_PROVIDER_ID] = {
                "provider_id": XAI_OAUTH_PROVIDER_ID,
                "auth_method": AuthMethod.OAUTH_PKCE.value,
                "configured": False,
                "health": ProviderHealth.MISSING_AUTH.value,
                **preserved,
            }

    update_provider_config(_update)


def xai_oauth_runtime_credentials(
    *,
    refresh_if_needed: bool = True,
    http_client: Any | None = None,
) -> XAIOAuthTokenSet:
    from row_bot.providers.auth_store import get_provider_secret
    from row_bot.providers.config import load_provider_config

    provider_cfg = load_provider_config().get("providers", {}).get(XAI_OAUTH_PROVIDER_ID, {})
    if provider_cfg.get("auth_method") != AuthMethod.OAUTH_PKCE.value:
        return XAIOAuthTokenSet(access_token="")
    access_token = get_provider_secret(XAI_OAUTH_PROVIDER_ID, "access_token")
    refresh_token = get_provider_secret(XAI_OAUTH_PROVIDER_ID, "refresh_token")
    id_token = get_provider_secret(XAI_OAUTH_PROVIDER_ID, "id_token")
    user_id = get_provider_secret(XAI_OAUTH_PROVIDER_ID, "user_id")
    account_id = get_provider_secret(XAI_OAUTH_PROVIDER_ID, "account")
    expires_at = str(provider_cfg.get("expires_at") or "")
    scopes = _string_tuple(provider_cfg.get("scopes") or provider_cfg.get("scope"))

    metadata = xai_oauth_token_metadata(access_token, id_token)
    if not user_id:
        user_id = str(metadata.get("user_id") or "")
    if not account_id:
        account_id = str(metadata.get("account_id") or "")
    if not expires_at:
        expires_at = str(metadata.get("expires_at") or "")
    if not scopes:
        scopes = tuple(metadata.get("scopes") or ())

    if refresh_if_needed and refresh_token and (not access_token or _expires_soon(expires_at, skew_seconds=300)):
        refreshed = refresh_xai_oauth_token(refresh_token, http_client=http_client)
        saved = save_xai_oauth_tokens(refreshed)
        access_token = refreshed.access_token
        refresh_token = refreshed.refresh_token or refresh_token
        id_token = refreshed.id_token or id_token
        user_id = refreshed.user_id or user_id
        account_id = refreshed.account_id or account_id
        expires_at = refreshed.expires_at or str(saved.get("expires_at") or expires_at)
        scopes = refreshed.scopes or scopes

    return XAIOAuthTokenSet(
        access_token=access_token,
        refresh_token=refresh_token,
        id_token=id_token,
        expires_at=expires_at,
        user_id=user_id,
        account_id=account_id,
        email_hash=str(provider_cfg.get("email_hash") or ""),
        scopes=scopes,
    )


def xai_oauth_runtime_available(*, refresh_if_needed: bool = False) -> bool:
    try:
        health = check_xai_oauth_token_health(refresh_if_needed=refresh_if_needed)
    except Exception:
        return False
    return health.runnable


def check_xai_oauth_token_health(
    *,
    refresh_if_needed: bool = True,
    http_client: Any | None = None,
) -> XAIOAuthTokenHealth:
    try:
        credentials = xai_oauth_runtime_credentials(refresh_if_needed=False)
    except Exception as exc:
        return XAIOAuthTokenHealth("error", f"Could not read xAI OAuth credentials: {_redact_text(str(exc))}")

    if not credentials.access_token and not credentials.refresh_token:
        return XAIOAuthTokenHealth(
            "missing",
            "xAI Grok needs to be connected in Settings -> Providers before OAuth models can run.",
            credentials,
        )

    should_refresh = bool(
        refresh_if_needed
        and credentials.refresh_token
        and (not credentials.access_token or _expires_soon(credentials.expires_at, skew_seconds=300))
    )
    if should_refresh:
        try:
            refreshed = refresh_xai_oauth_token(credentials.refresh_token, http_client=http_client)
            saved = save_xai_oauth_tokens(refreshed)
            credentials = XAIOAuthTokenSet(
                access_token=refreshed.access_token,
                refresh_token=refreshed.refresh_token or credentials.refresh_token,
                id_token=refreshed.id_token or credentials.id_token,
                expires_at=refreshed.expires_at or str(saved.get("expires_at") or credentials.expires_at),
                user_id=refreshed.user_id or credentials.user_id,
                account_id=refreshed.account_id or credentials.account_id,
                email_hash=refreshed.email_hash or credentials.email_hash,
                scopes=refreshed.scopes or credentials.scopes,
            )
            return XAIOAuthTokenHealth("refreshed", "xAI OAuth token refreshed successfully.", credentials)
        except XAIOAuthError as exc:
            if exc.kind == "entitlement_denied":
                return XAIOAuthTokenHealth("entitlement_denied", str(exc), credentials)
            if exc.kind == "reconnect_required":
                return XAIOAuthTokenHealth("expired", str(exc), XAIOAuthTokenSet(access_token=""))
            return XAIOAuthTokenHealth("error", f"xAI OAuth token refresh failed: {_redact_text(str(exc))}", credentials)
        except Exception as exc:
            return XAIOAuthTokenHealth("error", f"xAI OAuth token refresh failed: {_redact_text(str(exc))}", credentials)

    if not credentials.access_token:
        return XAIOAuthTokenHealth(
            "missing",
            "xAI OAuth access token is missing and no refresh was possible. Reconnect xAI Grok in Settings -> Providers.",
            credentials,
        )
    if _expires_soon(credentials.expires_at, skew_seconds=0):
        return XAIOAuthTokenHealth(
            "expired",
            "xAI OAuth token is expired and no refresh token is available. Reconnect xAI Grok in Settings -> Providers.",
            credentials,
        )
    return XAIOAuthTokenHealth("valid", "xAI OAuth token is valid.", credentials)


def xai_oauth_reconnect_message(detail: str = "") -> str:
    suffix = f" {detail}" if detail else ""
    return (
        "xAI Grok needs to be reconnected before using this OAuth model. "
        "Open Settings -> Providers -> xAI Grok, reconnect, then try again."
        f"{suffix}"
    )


def xai_oauth_entitlement_message(detail: str = "") -> str:
    suffix = f" {detail}" if detail else ""
    return (
        "xAI Grok is connected, but this account is not authorized for the selected xAI OAuth model. "
        "Use an eligible xAI account or configure the separate xAI API key provider in Settings -> Providers."
        f"{suffix}"
    )


def xai_oauth_runtime_block_message(*, refresh_if_needed: bool = True) -> str | None:
    health = check_xai_oauth_token_health(refresh_if_needed=refresh_if_needed)
    if health.runnable:
        return None
    if health.status == "entitlement_denied":
        return xai_oauth_entitlement_message(health.detail)
    return xai_oauth_reconnect_message(health.detail)


def fetch_xai_oauth_discovery(
    *,
    http_client: Any | None = None,
    discovery_url: str = XAI_OAUTH_DISCOVERY_URL,
) -> dict[str, Any]:
    if not _is_xai_owned_https_url(discovery_url):
        raise XAIOAuthError("xAI OAuth discovery URL must be an HTTPS xAI-owned URL.", kind="unsafe_discovery_url")
    client = http_client or _new_http_client(timeout=15.0)
    owns_client = http_client is None
    try:
        response = client.get(
            discovery_url,
            headers={"Accept": "application/json", "User-Agent": xai_oauth_user_agent()},
            timeout=15.0,
        )
    finally:
        if owns_client:
            client.close()
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code < 200 or status_code >= 300:
        cached = _load_cached_discovery()
        if cached:
            return cached
        raise XAIOAuthError(
            f"xAI OAuth discovery failed with HTTP {status_code}: {_safe_error_body(response)}",
            status_code=status_code,
            kind="discovery_failed",
        )
    payload = _json_response(response)
    authorization_endpoint = _validated_xai_endpoint(payload.get("authorization_endpoint"), "authorization_endpoint")
    token_endpoint = _validated_xai_endpoint(payload.get("token_endpoint"), "token_endpoint")
    issuer = str(payload.get("issuer") or XAI_OAUTH_ISSUER).rstrip("/")
    if issuer and not _is_xai_owned_https_url(issuer):
        raise XAIOAuthError("xAI OAuth discovery issuer was not an HTTPS xAI-owned URL.", kind="unsafe_issuer")
    discovery = {
        "issuer": issuer,
        "authorization_endpoint": authorization_endpoint,
        "token_endpoint": token_endpoint,
        "fetched_at": _utcnow().isoformat(),
    }
    _save_discovery(discovery)
    return discovery


def xai_oauth_token_metadata(access_token: str = "", id_token: str = "") -> dict[str, Any]:
    claims = _decode_jwt_claims(access_token) or _decode_jwt_claims(id_token)
    user_id = claims.get("sub") or claims.get("user_id") or claims.get("userId")
    account_id = claims.get("account_id") or claims.get("accountId") or claims.get("organization_id")
    email = claims.get("email")
    exp = claims.get("exp")
    expires_at = ""
    if isinstance(exp, (int, float)):
        try:
            expires_at = datetime.fromtimestamp(float(exp), timezone.utc).isoformat()
        except Exception:
            expires_at = ""
    scopes = _string_tuple(claims.get("scope") or claims.get("scp") or claims.get("scopes"))
    return {
        "user_id": user_id if isinstance(user_id, str) else "",
        "user_hash": secret_store.fingerprint(user_id) if isinstance(user_id, str) and user_id else "",
        "account_id": account_id if isinstance(account_id, str) else "",
        "account_id_hash": secret_store.fingerprint(account_id) if isinstance(account_id, str) and account_id else "",
        "email_hash": secret_store.fingerprint(email) if isinstance(email, str) and email else "",
        "expires_at": expires_at,
        "scopes": scopes,
    }


def xai_oauth_base_url(override: str | None = None) -> str:
    from row_bot.providers.config import load_provider_config, update_provider_config

    configured = override
    if configured is None:
        configured = load_provider_config().get("providers", {}).get(XAI_OAUTH_PROVIDER_ID, {}).get("base_url")
    raw = str(configured or XAI_OAUTH_BASE_URL).strip()
    try:
        return _validated_xai_base_url(raw)
    except XAIOAuthError as exc:
        warning = str(exc)

        def _update(cfg: dict[str, Any]) -> None:
            entry = cfg.setdefault("providers", {}).setdefault(XAI_OAUTH_PROVIDER_ID, {})
            entry["base_url"] = XAI_OAUTH_BASE_URL
            entry["base_url_warning"] = warning

        update_provider_config(_update)
        return XAI_OAUTH_BASE_URL


def fetch_xai_oauth_model_infos(
    *,
    access_token: str | None = None,
    http_client: Any | None = None,
    base_url: str | None = None,
) -> list[ModelInfo]:
    token = str(access_token or "").strip()
    if not token:
        credentials = xai_oauth_runtime_credentials(refresh_if_needed=True)
        token = credentials.access_token
    if not token:
        return []

    root = xai_oauth_base_url(base_url)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": xai_oauth_user_agent(),
    }
    client = http_client or _new_http_client(timeout=15.0)
    owns_client = http_client is None
    try:
        pages: list[list[Any]] = []
        last_error = ""
        for path in ("/models", "/language-models", "/image-generation-models"):
            try:
                response = client.get(f"{root.rstrip('/')}{path}", headers=headers, timeout=15.0)
            except Exception as exc:
                if not last_error:
                    last_error = _redact_text(str(exc))
                continue
            status_code = int(getattr(response, "status_code", 0) or 0)
            if status_code == 404:
                continue
            if status_code == 403:
                _mark_xai_oauth_entitlement_denied(
                    "xAI OAuth account is connected, but it is not authorized to list xAI models."
                )
                return []
            if status_code < 200 or status_code >= 300:
                last_error = f"xAI OAuth model discovery failed with HTTP {status_code}: {_safe_error_body(response)}"
                continue
            payload = response.json()
            if not isinstance(payload, dict):
                continue
            raw_entries = payload.get("data") or payload.get("models")
            if isinstance(raw_entries, list):
                pages.append(raw_entries)
        if not pages:
            _record_xai_oauth_catalog_status(
                0,
                source="live_xai_oauth_catalog",
                status="unavailable" if last_error else "empty_verified",
                last_error=last_error,
            )
            return []
    finally:
        if owns_client:
            client.close()

    entries = merged_xai_model_entries(pages)
    verified_at = _utcnow().replace(microsecond=0).isoformat().replace("+00:00", "Z")
    infos: list[ModelInfo] = []
    seen: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            continue
        info = _model_info_from_live_item(item, verified_at=verified_at)
        if not info or info.model_id in seen:
            continue
        seen.add(info.model_id)
        infos.append(info)
    infos = merge_xai_curated_chat_extras(
        infos,
        XAI_OAUTH_PROVIDER_ID,
        transport=TransportMode.OPENAI_RESPONSES,
        source="row_bot_xai_oauth_curated_catalog",
        risk_label="subscription",
        billing_label="subscription",
        verified_at=verified_at,
    )
    _record_xai_oauth_catalog_status(len(infos), source="live_xai_oauth_catalog", status="known" if infos else "empty_verified")
    return infos


def list_xai_oauth_model_infos(
    *,
    force_refresh: bool = False,
    http_client: Any | None = None,
) -> list[ModelInfo]:
    if force_refresh or http_client is not None:
        try:
            live_infos = fetch_xai_oauth_model_infos(http_client=http_client)
        except Exception as exc:
            _record_xai_oauth_catalog_status(
                0,
                source="live_xai_oauth_catalog",
                status="unavailable",
                last_error=_redact_text(str(exc)),
            )
            live_infos = []
        if live_infos:
            live_infos = _apply_cached_vision_probe_overrides(live_infos)
            _save_catalog_cache(live_infos)
            return live_infos

    return _load_catalog_cache()


def list_xai_oauth_model_infos_for_status() -> list[ModelInfo]:
    return _load_catalog_cache()


def save_xai_oauth_runtime_probe(probe: dict[str, Any]) -> dict[str, Any]:
    from row_bot.providers.config import update_provider_config

    safe_probe = dict(probe or {})
    safe_probe["provider_id"] = XAI_OAUTH_PROVIDER_ID
    safe_probe["runtime"] = "xai_oauth_responses"
    safe_probe["probed_at"] = str(safe_probe.get("probed_at") or _utcnow().isoformat())
    errors = [
        _redact_text(str(error), limit=220)
        for error in safe_probe.get("errors", [])
        if str(error or "").strip()
    ]
    safe_probe["errors"] = errors[:5]

    def _update(cfg: dict[str, Any]) -> None:
        entry = cfg.setdefault("providers", {}).setdefault(XAI_OAUTH_PROVIDER_ID, {})
        entry["last_runtime_probe"] = dict(safe_probe)
        entry["last_error"] = "" if safe_probe.get("ok") else "; ".join(errors[:2])

    cfg = update_provider_config(_update)
    return dict(cfg.get("providers", {}).get(XAI_OAUTH_PROVIDER_ID, {}).get("last_runtime_probe", {}))


def _safe_xai_oauth_vision_probe(probe: dict[str, Any]) -> dict[str, Any]:
    model_id = str((probe or {}).get("model_id") or "").strip()
    ok = bool((probe or {}).get("ok"))
    return {
        "provider_id": XAI_OAUTH_PROVIDER_ID,
        "probe_version": XAI_OAUTH_VISION_PROBE_VERSION,
        "model_id": model_id,
        "ok": ok,
        "status": "confirmed" if ok else "failed",
        "probed_at": str((probe or {}).get("probed_at") or _utcnow().isoformat()),
        "error": _redact_text(str((probe or {}).get("error") or ""), limit=220),
    }


def _xai_oauth_vision_probe_summary(safe_probes: list[dict[str, Any]]) -> dict[str, Any]:
    if not safe_probes:
        safe_probes = [_safe_xai_oauth_vision_probe({
            "model_id": "",
            "ok": False,
            "error": "vision probe did not run",
            "probed_at": _utcnow().isoformat(),
        })]
    confirmed = [probe for probe in safe_probes if probe.get("ok")]
    failed = [probe for probe in safe_probes if not probe.get("ok")]
    chosen = confirmed[0] if confirmed else safe_probes[-1]
    summary = dict(chosen)
    summary["ok"] = bool(confirmed)
    summary["status"] = "confirmed" if confirmed else "failed"
    summary["confirmed_model_ids"] = [str(probe.get("model_id") or "") for probe in confirmed if probe.get("model_id")]
    summary["failed_model_ids"] = [str(probe.get("model_id") or "") for probe in failed if probe.get("model_id")]
    summary["probed_model_count"] = len([probe for probe in safe_probes if probe.get("model_id")])
    summary["results"] = [dict(probe) for probe in safe_probes]
    if confirmed:
        summary["error"] = ""
    return summary


def save_xai_oauth_vision_probe_results(probes: list[dict[str, Any]]) -> dict[str, Any]:
    from row_bot.providers.config import update_provider_config

    safe_probes = [
        _safe_xai_oauth_vision_probe(probe)
        for probe in probes
        if isinstance(probe, dict)
    ]
    summary = _xai_oauth_vision_probe_summary(safe_probes)

    def _update(cfg: dict[str, Any]) -> None:
        entry = cfg.setdefault("providers", {}).setdefault(XAI_OAUTH_PROVIDER_ID, {})
        entry["last_vision_probe"] = dict(summary)
        cache = entry.get("catalog_cache")
        if not isinstance(cache, dict) or not isinstance(cache.get("models"), list):
            return
        rows_by_model_id = {
            str(row.get("id") or row.get("model_id") or ""): row
            for row in cache["models"]
            if isinstance(row, dict)
        }
        for safe_probe in safe_probes:
            model_id = str(safe_probe.get("model_id") or "")
            if not model_id:
                continue
            row = rows_by_model_id.get(model_id)
            if not isinstance(row, dict):
                continue
            row["vision_probe_status"] = safe_probe["status"]
            row["vision_probe_at"] = safe_probe["probed_at"]
            row["vision_probe_version"] = safe_probe["probe_version"]
            if safe_probe.get("ok"):
                input_modalities = set(_string_tuple(row.get("input_modalities")))
                capabilities = set(_string_tuple(row.get("capabilities")))
                input_modalities.add(ModelModality.IMAGE.value)
                capabilities.add("vision")
                row["input_modalities"] = sorted(input_modalities)
                row["capabilities"] = sorted(capabilities)
                row["vision_probe_added_vision"] = True
                row.pop("vision_probe_error", None)
            else:
                row["vision_probe_error"] = safe_probe["error"]
                if row.get("vision_probe_added_vision") is True:
                    input_modalities = set(_string_tuple(row.get("input_modalities")))
                    capabilities = set(_string_tuple(row.get("capabilities")))
                    input_modalities.discard(ModelModality.IMAGE.value)
                    capabilities.discard("vision")
                    row["input_modalities"] = sorted(input_modalities)
                    row["capabilities"] = sorted(capabilities)
                    row["vision_probe_added_vision"] = False

    cfg = update_provider_config(_update)
    return dict(cfg.get("providers", {}).get(XAI_OAUTH_PROVIDER_ID, {}).get("last_vision_probe", {}))


def xai_oauth_vision_probe_needed() -> bool:
    from row_bot.providers.config import load_provider_config

    try:
        entry = load_provider_config().get("providers", {}).get(XAI_OAUTH_PROVIDER_ID, {})
    except Exception:
        return False
    if not isinstance(entry, dict):
        return False
    last_probe = entry.get("last_vision_probe")
    if not isinstance(last_probe, dict):
        return True
    if last_probe.get("probe_version") != XAI_OAUTH_VISION_PROBE_VERSION:
        return True

    probed_model_ids: set[str] = set()
    for key in ("confirmed_model_ids", "failed_model_ids"):
        values = last_probe.get(key)
        if isinstance(values, list):
            probed_model_ids.update(str(value) for value in values if str(value or "").strip())
    results = last_probe.get("results")
    if isinstance(results, list):
        for result in results:
            if isinstance(result, dict) and str(result.get("probe_version") or "") == XAI_OAUTH_VISION_PROBE_VERSION:
                model_id = str(result.get("model_id") or "").strip()
                if model_id:
                    probed_model_ids.add(model_id)

    rows_by_model_id = _cached_catalog_rows_by_model_id(entry)
    if not rows_by_model_id:
        return False
    media_tasks = {
        ModelTask.IMAGE_GENERATION.value,
        ModelTask.IMAGE_EDIT.value,
        ModelTask.VIDEO_GENERATION.value,
    }
    for model_id, row in rows_by_model_id.items():
        tasks = set(_string_tuple(row.get("tasks")))
        if tasks and tasks.issubset(media_tasks):
            continue
        if str(row.get("vision_probe_version") or "") != XAI_OAUTH_VISION_PROBE_VERSION:
            return True
        if model_id not in probed_model_ids:
            return True
    return False


def save_xai_oauth_vision_probe(probe: dict[str, Any]) -> dict[str, Any]:
    return save_xai_oauth_vision_probe_results([probe])


def run_xai_oauth_vision_probe(
    model_name: str = "",
    *,
    chat_model: Any | None = None,
) -> dict[str, Any]:
    from langchain_core.messages import HumanMessage

    model_id = str(model_name or "").strip()
    candidate_model_ids: list[str] = [model_id] if model_id else []
    if not candidate_model_ids:
        infos = list_xai_oauth_model_infos()
        media_tasks = {
            ModelTask.IMAGE_GENERATION.value,
            ModelTask.IMAGE_EDIT.value,
            ModelTask.VIDEO_GENERATION.value,
        }
        candidate_model_ids = [
            info.model_id
            for info in infos
            if info.model_id and not (set(info.tasks) and set(info.tasks).issubset(media_tasks))
        ]
    if not candidate_model_ids:
        candidate_model_ids = ["grok-4"]

    last_result: dict[str, Any] | None = None
    results: list[dict[str, Any]] = []
    probe_all_candidates = not model_id and chat_model is None
    for candidate_model_id in candidate_model_ids:
        result: dict[str, Any] = {
            "provider_id": XAI_OAUTH_PROVIDER_ID,
            "probe_version": XAI_OAUTH_VISION_PROBE_VERSION,
            "model_id": candidate_model_id,
            "ok": False,
            "error": "",
            "probed_at": _utcnow().isoformat(),
        }
        try:
            model = chat_model
            if model is None:
                from row_bot.providers.transports.xai_oauth_responses import ChatXAIOAuthResponses

                model = ChatXAIOAuthResponses(model_name=candidate_model_id, timeout=90.0)
            response = model.invoke([HumanMessage(content=[
                {"type": "text", "text": "Reply with the word image if an image input was received."},
                {"type": "image_url", "image_url": {"url": _probe_image_data_url()}},
            ])])
            text = _probe_text_content(response).lower()
            result["ok"] = "image" in text
            if not result["ok"]:
                result["error"] = f"unexpected response {text[:80] or '<empty>'}"
        except Exception as exc:
            result["error"] = _redact_text(str(exc), limit=220)
        last_result = result
        results.append(result)
        if not probe_all_candidates and (result["ok"] or chat_model is not None):
            break
    fallback_result = last_result or {
        "provider_id": XAI_OAUTH_PROVIDER_ID,
        "probe_version": XAI_OAUTH_VISION_PROBE_VERSION,
        "model_id": candidate_model_ids[0],
        "ok": False,
        "error": "vision probe did not run",
        "probed_at": _utcnow().isoformat(),
    }
    if probe_all_candidates:
        return save_xai_oauth_vision_probe_results(results or [fallback_result])
    return save_xai_oauth_vision_probe(fallback_result)


def run_xai_oauth_runtime_probe(
    model_name: str = "",
    *,
    chat_model: Any | None = None,
) -> dict[str, Any]:
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    model_id = str(model_name or "").strip()
    model_infos = [] if model_id else list_xai_oauth_model_infos(force_refresh=True)
    if not model_id and model_infos:
        model_id = model_infos[0].model_id
    if not model_id:
        model_id = "grok-4"
    vision_model_id = next(
        (info.model_id for info in model_infos if ModelModality.IMAGE.value in info.input_modalities),
        "",
    )
    result: dict[str, Any] = {
        "provider_id": XAI_OAUTH_PROVIDER_ID,
        "runtime": "xai_oauth_responses",
        "model_id": model_id,
        "ok": False,
        "chat_ok": False,
        "tool_calling": None,
        "tool_round_trip": None,
        "vision_probed": bool(vision_model_id),
        "vision_model_id": vision_model_id,
        "vision_ok": None,
        "errors": [],
        "probed_at": _utcnow().isoformat(),
    }
    try:
        model = chat_model
        if model is None:
            from row_bot.providers.transports.xai_oauth_responses import ChatXAIOAuthResponses

            model = ChatXAIOAuthResponses(model_name=model_id, timeout=90.0)

        expected = "row-bot-xai-smoke-ok"
        text_response = model.invoke([HumanMessage(content=f"Reply with exactly this text and nothing else: {expected}")])
        text = _probe_text_content(text_response).strip().strip("`").strip()
        result["chat_ok"] = expected in text
        if not result["chat_ok"]:
            result["errors"].append(f"chat: unexpected response {text[:80] or '<empty>'}")

        tool_model = model.bind_tools([_probe_calculate_tool()], tool_choice="calculate")
        tool_prompt = "Use the calculate tool for the expression 1 + 1. Do not answer in text."
        tool_response = tool_model.invoke([HumanMessage(content=tool_prompt)])
        tool_calls = [
            dict(call)
            for call in (getattr(tool_response, "tool_calls", None) or [])
            if isinstance(call, dict)
        ]
        calculate_call = next((call for call in tool_calls if call.get("name") == "calculate"), None)
        result["tool_calling"] = calculate_call is not None
        if calculate_call is None:
            names = ", ".join(str(call.get("name") or "") for call in tool_calls if call.get("name"))
            result["errors"].append(f"tools: expected calculate tool call, got {names or 'none'}")
        else:
            call_id = str(calculate_call.get("id") or "call_row_bot_xai_probe")
            replay_response = model.invoke([
                HumanMessage(content=tool_prompt),
                AIMessage(content="", tool_calls=[{
                    "name": "calculate",
                    "args": dict(calculate_call.get("args") or {"expression": "1 + 1"}),
                    "id": call_id,
                    "type": "tool_call",
                }]),
                ToolMessage(content="1 + 1 = 2", name="calculate", tool_call_id=call_id),
            ])
            result["tool_round_trip"] = replay_response is not None

        if vision_model_id:
            vision_model = chat_model
            if vision_model is None:
                from row_bot.providers.transports.xai_oauth_responses import ChatXAIOAuthResponses

                vision_model = ChatXAIOAuthResponses(model_name=vision_model_id, timeout=90.0)
            vision_response = vision_model.invoke([HumanMessage(content=[
                {"type": "text", "text": "Reply with the word image if an image input was received."},
                {"type": "image_url", "image_url": {"url": _probe_image_data_url()}},
            ])])
            vision_text = _probe_text_content(vision_response).lower()
            result["vision_ok"] = "image" in vision_text
            if result["vision_ok"] is not True:
                result["errors"].append(f"vision: unexpected response {vision_text[:80] or '<empty>'}")
    except Exception as exc:
        result["errors"].append(_redact_text(str(exc), limit=220))
        if result["tool_calling"] is None:
            result["tool_calling"] = False
        if result["tool_round_trip"] is None:
            result["tool_round_trip"] = False
        if result["vision_probed"] and result["vision_ok"] is None:
            result["vision_ok"] = False

    result["ok"] = (
        result.get("chat_ok") is True
        and result.get("tool_calling") is True
        and result.get("tool_round_trip") is True
        and (not result.get("vision_probed") or result.get("vision_ok") is True)
    )
    return save_xai_oauth_runtime_probe(result)


def seed_recommended_xai_oauth_quick_choices(*, max_choices: int = 1) -> list[dict[str, Any]]:
    from row_bot.providers.config import load_provider_config
    from row_bot.providers.runtime import provider_status
    from row_bot.providers.selection import add_quick_choice_for_model

    status = provider_status(XAI_OAUTH_PROVIDER_ID)
    if not status.get("configured") or not status.get("runtime_enabled"):
        return load_provider_config().get("quick_choices", [])
    infos = list_xai_oauth_model_infos()
    if not infos:
        infos = list_xai_oauth_model_infos(force_refresh=True)
    for model_info in infos[:max(0, max_choices)]:
        add_quick_choice_for_model(
            model_info.model_id,
            provider_id=XAI_OAUTH_PROVIDER_ID,
            display_name=model_info.display_name,
            source="xai_oauth_recommended",
            capabilities_snapshot=model_info.capability_snapshot(),
        )
    return load_provider_config().get("quick_choices", [])


def _new_http_client(timeout: float = 30.0) -> Any:
    import httpx

    return httpx.Client(timeout=timeout)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _is_pytest_running() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ


def _xai_oauth_client_id(value: str | None = None) -> str:
    return _resolve_xai_oauth_client_id(value, require=True)[0]


def _resolve_xai_oauth_client_id(value: str | None = None, *, require: bool) -> tuple[str, str, str]:
    if value is not None:
        explicit = str(value or "").strip()
        if explicit and not _is_placeholder_xai_oauth_client_id(explicit):
            return explicit, "argument", "xAI OAuth client ID was provided for this login."
        detail = _xai_oauth_missing_client_id_detail("The provided xAI OAuth client ID was empty or a placeholder.")
        if require:
            raise XAIOAuthError(detail, kind="missing_client_id")
        return "", "argument" if explicit else "", detail

    invalid_source = ""
    override_value = xai_oauth_saved_client_id_override()
    if override_value:
        if not _is_placeholder_xai_oauth_client_id(override_value):
            return override_value, "override", "xAI OAuth client ID is using a saved override from Settings -> Providers."
        invalid_source = invalid_source or "provider_config"

    default_value = xai_oauth_default_client_id()
    if default_value:
        return default_value, "default", "xAI OAuth client ID is configured by the Row-Bot default."

    env_value = str(os.environ.get(ROW_BOT_XAI_OAUTH_CLIENT_ID_ENV) or "").strip()
    if env_value:
        if not _is_placeholder_xai_oauth_client_id(env_value):
            return env_value, "environment", f"xAI OAuth client ID is configured by the development environment variable {ROW_BOT_XAI_OAUTH_CLIENT_ID_ENV}."
        invalid_source = invalid_source or "environment"

    reason = "The configured xAI OAuth client ID is empty or a placeholder." if invalid_source else "xAI OAuth client ID is not configured."
    detail = _xai_oauth_missing_client_id_detail(reason)
    if require:
        raise XAIOAuthError(detail, kind="missing_client_id")
    return "", invalid_source, detail


def _client_id_override_from_entry(entry: dict[str, Any]) -> str:
    source = str(entry.get("oauth_client_id_source") or "").strip().lower()
    raw = str(entry.get("oauth_client_id") or "").strip()
    if raw and source in {"override", "provider_config", "user_override"}:
        return raw
    legacy = str(entry.get("client_id") or "").strip()
    if legacy and not source:
        return legacy
    if raw and not source and not entry.get("oauth_client_id_configured"):
        return raw
    return ""


def _is_placeholder_xai_oauth_client_id(value: str) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized in XAI_OAUTH_CLIENT_ID_PLACEHOLDERS


def _xai_oauth_missing_client_id_detail(reason: str = "") -> str:
    prefix = f"{reason} " if reason else ""
    return (
        f"{prefix}Set an OAuth client ID override in Settings -> Providers -> xAI Grok, "
        "then start xAI Grok sign-in again."
    )


def _xai_oauth_scopes(value: tuple[str, ...] | list[str] | str | None = None) -> tuple[str, ...]:
    if value is None:
        value = os.environ.get(ROW_BOT_XAI_OAUTH_SCOPES_ENV) or DEFAULT_XAI_OAUTH_SCOPES
    scopes = _string_tuple(value)
    return scopes or DEFAULT_XAI_OAUTH_SCOPES


def _new_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(part for part in value.replace(",", " ").split() if part)
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(str(item) for item in value if str(item))
    return ()


def _first_query_value(values: dict[str, list[str]], key: str) -> str:
    raw = values.get(key) or []
    return str(raw[0] if raw else "").strip()


def _decode_jwt_claims(token: str) -> dict[str, Any]:
    if not isinstance(token, str) or not token.strip():
        return {}
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


def _parse_expires_at(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _expires_soon(expires_at: str, *, skew_seconds: int = 120) -> bool:
    parsed = _parse_expires_at(expires_at)
    if parsed is None:
        return False
    return parsed <= _utcnow() + timedelta(seconds=max(0, skew_seconds))


def _redact_text(value: str, *, limit: int = 300) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", text)
    text = re.sub(
        r"(?i)(\"?(?:access_token|refresh_token|id_token|authorization)\"?\s*[:=]\s*)\"?[^\",\s}]+",
        r"\1[redacted]",
        text,
    )
    text = re.sub(r"(?i)(code=)[^&\s]+", r"\1[redacted]", text)
    for marker in _SENSITIVE_MARKERS:
        text = text.replace(marker, "[redacted]")
    return text[:limit]


def _safe_error_body(response: Any) -> str:
    try:
        text = str(getattr(response, "text", "") or "")
    except Exception:
        text = ""
    if not text:
        try:
            payload = response.json()
            text = json.dumps(payload) if isinstance(payload, dict) else str(payload)
        except Exception:
            text = ""
    return _redact_text(text)


def _json_response(response: Any) -> dict[str, Any]:
    payload = response.json()
    if not isinstance(payload, dict):
        raise XAIOAuthError("xAI OAuth response was not a JSON object.", kind="invalid_json")
    return payload


def _token_set_from_payload(payload: dict[str, Any], *, fallback_refresh_token: str = "") -> XAIOAuthTokenSet:
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise XAIOAuthError("xAI OAuth token response did not include an access_token.", kind="missing_access_token")
    refresh_token = str(payload.get("refresh_token") or fallback_refresh_token or "").strip()
    id_token = str(payload.get("id_token") or "").strip()
    metadata = xai_oauth_token_metadata(access_token, id_token)
    expires_at = str(metadata.get("expires_at") or "")
    expires_in = payload.get("expires_in")
    if not expires_at and isinstance(expires_in, (int, float)):
        expires_at = (_utcnow() + timedelta(seconds=max(0, float(expires_in)))).isoformat()
    scopes = _string_tuple(payload.get("scope") or payload.get("scopes")) or tuple(metadata.get("scopes") or ())
    return XAIOAuthTokenSet(
        access_token=access_token,
        refresh_token=refresh_token,
        id_token=id_token,
        expires_at=expires_at,
        user_id=str(metadata.get("user_id") or ""),
        account_id=str(metadata.get("account_id") or ""),
        email_hash=str(metadata.get("email_hash") or ""),
        scopes=scopes,
    )


def _is_xai_owned_https_url(value: Any) -> bool:
    parsed = urlparse(str(value or ""))
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == "x.ai" or host.endswith(".x.ai"))


def _validated_xai_endpoint(value: Any, label: str) -> str:
    endpoint = str(value or "").strip()
    if not endpoint or not _is_xai_owned_https_url(endpoint):
        raise XAIOAuthError(f"xAI OAuth {label} must be an HTTPS xAI-owned URL.", kind="unsafe_endpoint")
    return endpoint


def _validated_xai_base_url(value: str) -> str:
    parsed = urlparse(str(value or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "x.ai" or host.endswith(".x.ai")):
        raise XAIOAuthError("xAI OAuth bearer base URL must be an HTTPS xAI-owned host.", kind="unsafe_base_url")
    path = parsed.path.rstrip("/") or ""
    return f"https://{parsed.netloc}{path}"


def _save_discovery(discovery: dict[str, Any]) -> None:
    from row_bot.providers.config import update_provider_config

    safe = {
        "issuer": str(discovery.get("issuer") or ""),
        "authorization_endpoint": str(discovery.get("authorization_endpoint") or ""),
        "token_endpoint": str(discovery.get("token_endpoint") or ""),
        "fetched_at": str(discovery.get("fetched_at") or _utcnow().isoformat()),
    }

    def _update(cfg: dict[str, Any]) -> None:
        entry = cfg.setdefault("providers", {}).setdefault(XAI_OAUTH_PROVIDER_ID, {})
        entry["oauth_discovery"] = safe

    update_provider_config(_update)


def _load_cached_discovery() -> dict[str, Any]:
    from row_bot.providers.config import load_provider_config

    cached = load_provider_config().get("providers", {}).get(XAI_OAUTH_PROVIDER_ID, {}).get("oauth_discovery")
    if not isinstance(cached, dict):
        return {}
    try:
        return {
            "issuer": _validated_xai_endpoint(cached.get("issuer") or XAI_OAUTH_ISSUER, "issuer"),
            "authorization_endpoint": _validated_xai_endpoint(cached.get("authorization_endpoint"), "authorization_endpoint"),
            "token_endpoint": _validated_xai_endpoint(cached.get("token_endpoint"), "token_endpoint"),
            "fetched_at": str(cached.get("fetched_at") or ""),
        }
    except XAIOAuthError:
        return {}


def _configured_token_endpoint() -> str:
    cached = _load_cached_discovery()
    if cached.get("token_endpoint"):
        return str(cached["token_endpoint"])
    return f"{XAI_OAUTH_ISSUER}/oauth/token"


def _mark_xai_oauth_requires_reconnect(message: str) -> None:
    from row_bot.providers.auth_store import delete_provider_secret
    from row_bot.providers.config import update_provider_config

    for credential_name in ("access_token", "refresh_token", "id_token", "user_id", "account"):
        delete_provider_secret(XAI_OAUTH_PROVIDER_ID, credential_name)

    def _update(cfg: dict[str, Any]) -> None:
        entry = cfg.setdefault("providers", {}).setdefault(XAI_OAUTH_PROVIDER_ID, {})
        entry.update({
            "provider_id": XAI_OAUTH_PROVIDER_ID,
            "configured": False,
            "health": ProviderHealth.MISSING_AUTH.value,
            "source": AuthMethod.OAUTH_PKCE.value,
            "auth_method": AuthMethod.OAUTH_PKCE.value,
            "fingerprint": "",
            "last_error": _redact_text(message),
        })

    update_provider_config(_update)


def _mark_xai_oauth_entitlement_denied(message: str) -> None:
    from row_bot.providers.config import update_provider_config

    def _update(cfg: dict[str, Any]) -> None:
        entry = cfg.setdefault("providers", {}).setdefault(XAI_OAUTH_PROVIDER_ID, {})
        entry.update({
            "provider_id": XAI_OAUTH_PROVIDER_ID,
            "health": ProviderHealth.ERROR.value,
            "last_error": _redact_text(message),
            "token_health": "entitlement_denied",
        })

    update_provider_config(_update)


def _record_xai_oauth_catalog_status(
    count: int,
    *,
    source: str,
    status: str,
    last_error: str = "",
) -> None:
    from row_bot.providers.config import update_provider_config

    def _update(cfg: dict[str, Any]) -> None:
        entry = cfg.setdefault("providers", {}).setdefault(XAI_OAUTH_PROVIDER_ID, {})
        entry["model_count"] = int(count)
        entry["model_count_source"] = source
        entry["model_count_status"] = status
        if last_error:
            entry["last_error"] = _redact_text(last_error)

    update_provider_config(_update)


def _normalize_modalities(value: Any) -> set[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = list(value)
    else:
        values = []
    normalized: set[str] = set()
    for item in values:
        text = str(item or "").strip().lower()
        if text in {"text", "image", "audio", "video"}:
            normalized.add(text)
    return normalized


def _metadata_bool(metadata: dict[str, Any], key: str, default: bool) -> bool:
    value = metadata.get(key)
    return value if isinstance(value, bool) else default


def _xai_context_window(model_id: str, metadata: dict[str, Any]) -> int:
    for key in ("context_window", "contextWindow", "context_length", "contextLength", "max_input_tokens"):
        try:
            value = int(metadata.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    lower = model_id.lower()
    if lower.startswith("grok-4"):
        return 2_000_000
    return 131_072


def _is_xai_media_model_id(model_id: str) -> bool:
    lower = model_id.lower()
    return "grok-imagine" in lower or "image-generation" in lower or "video-generation" in lower


def _model_info_from_live_item(item: dict[str, Any], *, verified_at: str) -> ModelInfo | None:
    from row_bot.providers.reasoning import reasoning_metadata_for_catalog

    model_id = str(item.get("id") or item.get("model") or item.get("name") or "").strip()
    if not model_id or is_hidden_xai_model(item, model_id):
        return None
    display_name = str(item.get("display_name") or item.get("displayName") or item.get("label") or model_id)
    generation_parameters = xai_generation_parameters_from_item(item)
    if _is_xai_media_model_id(model_id) or generation_parameters:
        from row_bot.providers.catalog import model_info_from_metadata

        metadata = dict(item)
        if generation_parameters:
            metadata["generation_parameters"] = generation_parameters
        info = model_info_from_metadata(
            XAI_OAUTH_PROVIDER_ID,
            model_id,
            metadata,
            display_name=display_name,
            context_window=0,
            risk_label="subscription",
            source="xai_oauth_live_media_catalog",
        )
        return replace(
            info,
            billing_label="subscription",
            source_confidence="live_xai_oauth_media_catalog",
            last_verified_at=verified_at,
        )
    inputs = (
        _normalize_modalities(item.get("input_modalities"))
        or _normalize_modalities(item.get("inputModalities"))
        or _normalize_modalities(item.get("modalities"))
        or {ModelModality.TEXT.value}
    )
    architecture = item.get("architecture")
    if isinstance(architecture, dict):
        inputs.update(_normalize_modalities(architecture.get("input_modalities") or architecture.get("input")))
    capabilities_payload = item.get("capabilities")
    if isinstance(capabilities_payload, dict):
        for key in ("image_input", "vision", "image"):
            value = capabilities_payload.get(key)
            if value is True or (isinstance(value, dict) and value.get("supported")):
                inputs.add(ModelModality.IMAGE.value)
    elif isinstance(capabilities_payload, list):
        if any(str(value).strip().lower() in {"vision", "image", "image_input"} for value in capabilities_payload):
            inputs.add(ModelModality.IMAGE.value)
    if item.get("vision") is True:
        inputs.add(ModelModality.IMAGE.value)

    tool_calling = _metadata_bool(item, "tool_calling", True)
    streaming = _metadata_bool(item, "streaming", True)
    capabilities = {"text", "chat"}
    if ModelModality.IMAGE.value in inputs:
        capabilities.add("vision")
    if tool_calling:
        capabilities.add("tool_calling")
    if streaming:
        capabilities.add("streaming")
    lower = model_id.lower()
    if "reasoning" in lower or lower.startswith("grok-4"):
        capabilities.add("reasoning")

    return ModelInfo(
        provider_id=XAI_OAUTH_PROVIDER_ID,
        model_id=model_id,
        display_name=display_name,
        context_window=_xai_context_window(model_id, item),
        transport=TransportMode.OPENAI_RESPONSES,
        capabilities=frozenset(capabilities),
        input_modalities=frozenset(inputs),
        output_modalities=frozenset({ModelModality.TEXT.value}),
        tasks=frozenset({ModelTask.RESPONSES.value}),
        tool_calling=tool_calling,
        streaming=streaming,
        endpoint_compatibility=frozenset({TransportMode.OPENAI_RESPONSES}),
        billing_label="subscription",
        source_confidence="live_xai_oauth_catalog",
        last_verified_at=verified_at,
        risk_label="subscription",
        source="xai_oauth_live_catalog",
        reasoning=reasoning_metadata_for_catalog(XAI_OAUTH_PROVIDER_ID, model_id, item),
    )


def _model_cache_row(model_info: ModelInfo) -> dict[str, Any]:
    return {
        "id": model_info.model_id,
        "display_name": model_info.display_name,
        "context_window": model_info.context_window,
        "input_modalities": sorted(model_info.input_modalities),
        "output_modalities": sorted(model_info.output_modalities),
        "tasks": sorted(model_info.tasks),
        "capabilities": sorted(model_info.capabilities),
        "tool_calling": model_info.tool_calling,
        "streaming": model_info.streaming,
        "source_confidence": model_info.source_confidence,
        "source": model_info.source,
        "last_verified_at": model_info.last_verified_at,
        "reasoning": dict(model_info.reasoning) if model_info.reasoning else None,
        "generation_parameters": (
            dict(model_info.generation_parameters)
            if model_info.generation_parameters
            else None
        ),
    }


def _model_info_from_cache_row(row: dict[str, Any]) -> ModelInfo | None:
    model_id = str(row.get("id") or row.get("model_id") or "").strip()
    if not model_id:
        return None
    info = ModelInfo(
        provider_id=XAI_OAUTH_PROVIDER_ID,
        model_id=model_id,
        display_name=str(row.get("display_name") or model_id),
        context_window=int(row.get("context_window") or 0),
        transport=TransportMode.OPENAI_RESPONSES,
        capabilities=frozenset(_string_tuple(row.get("capabilities")) or ("text", "chat", "streaming", "tool_calling")),
        input_modalities=frozenset(_normalize_modalities(row.get("input_modalities")) or {ModelModality.TEXT.value}),
        output_modalities=frozenset(_normalize_modalities(row.get("output_modalities")) or {ModelModality.TEXT.value}),
        tasks=frozenset(_string_tuple(row.get("tasks")) or {ModelTask.RESPONSES.value}),
        tool_calling=row.get("tool_calling") if isinstance(row.get("tool_calling"), bool) else True,
        streaming=row.get("streaming") if isinstance(row.get("streaming"), bool) else True,
        endpoint_compatibility=frozenset({TransportMode.OPENAI_RESPONSES}),
        billing_label="subscription",
        source_confidence=str(row.get("source_confidence") or "cached_xai_oauth_catalog"),
        last_verified_at=str(row.get("last_verified_at") or ""),
        risk_label="subscription",
        source=str(row.get("source") or "xai_oauth_cached_catalog"),
        reasoning=dict(row["reasoning"]) if isinstance(row.get("reasoning"), dict) else None,
        generation_parameters=(
            dict(row["generation_parameters"])
            if isinstance(row.get("generation_parameters"), dict)
            else None
        ),
    )
    return info


def _cached_catalog_rows_by_model_id(entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    cache = entry.get("catalog_cache") if isinstance(entry, dict) else {}
    rows = cache.get("models") if isinstance(cache, dict) else []
    if not isinstance(rows, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = str(row.get("id") or row.get("model_id") or "").strip()
        if model_id:
            result[model_id] = row
    return result


def _apply_cached_vision_probe_overrides(infos: list[ModelInfo]) -> list[ModelInfo]:
    if not infos:
        return infos
    try:
        from row_bot.providers.config import load_provider_config

        entry = load_provider_config().get("providers", {}).get(XAI_OAUTH_PROVIDER_ID, {})
    except Exception:
        return infos
    cached_rows = _cached_catalog_rows_by_model_id(entry)
    if not cached_rows:
        return infos

    patched: list[ModelInfo] = []
    for info in infos:
        row = cached_rows.get(info.model_id)
        if (
            isinstance(row, dict)
            and row.get("vision_probe_added_vision") is True
            and str(row.get("vision_probe_status") or "") == "confirmed"
        ):
            input_modalities = set(info.input_modalities)
            capabilities = set(info.capabilities)
            input_modalities.add(ModelModality.IMAGE.value)
            capabilities.add("vision")
            patched.append(replace(
                info,
                input_modalities=frozenset(input_modalities),
                capabilities=frozenset(capabilities),
            ))
        else:
            patched.append(info)
    return patched


def _save_catalog_cache(infos: list[ModelInfo]) -> None:
    if not infos:
        return
    from row_bot.providers.config import update_provider_config

    def _update(cfg: dict[str, Any]) -> None:
        entry = cfg.setdefault("providers", {}).setdefault(XAI_OAUTH_PROVIDER_ID, {})
        existing_rows = _cached_catalog_rows_by_model_id(entry)
        rows: list[dict[str, Any]] = []
        for info in infos:
            row = _model_cache_row(info)
            existing = existing_rows.get(info.model_id)
            if isinstance(existing, dict):
                for key in (
                    "vision_probe_added_vision",
                    "vision_probe_at",
                    "vision_probe_error",
                    "vision_probe_status",
                    "vision_probe_version",
                ):
                    if key in existing:
                        row[key] = existing[key]
            rows.append(row)
        entry["catalog_cache"] = {
            "fetched_at": _utcnow().isoformat(),
            "source": "live_xai_oauth_catalog",
            "models": rows,
        }
        entry["model_count"] = len(infos)
        entry["model_count_source"] = "xai_oauth_live_catalog"
        entry["model_count_status"] = "known"
        entry["last_error"] = ""

    update_provider_config(_update)


def _load_catalog_cache() -> list[ModelInfo]:
    from row_bot.providers.config import load_provider_config

    cache = load_provider_config().get("providers", {}).get(XAI_OAUTH_PROVIDER_ID, {}).get("catalog_cache")
    if not isinstance(cache, dict):
        return []
    rows = cache.get("models")
    if not isinstance(rows, list):
        return []
    infos: list[ModelInfo] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        info = _model_info_from_cache_row(row)
        if not info or info.model_id in seen:
            continue
        seen.add(info.model_id)
        infos.append(info)
    return infos


def _probe_text_content(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return str(content or "")


def _probe_calculate_tool() -> Any:
    from langchain_core.tools import StructuredTool

    def _calculate(expression: str) -> str:
        return "1 + 1 = 2" if str(expression or "").strip() else "2"

    return StructuredTool.from_function(
        func=_calculate,
        name="calculate",
        description="Evaluate a mathematical expression.",
    )


def _probe_image_data_url() -> str:
    return "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAN0lEQVR4nO3RwQ0AMAjDwJT9d05HMB9+vgGCZF7bXJrT9XhgwR8gEyETIRMhEyETIRMhEyEThXzH8QM9OMM6fAAAAABJRU5ErkJggg=="
