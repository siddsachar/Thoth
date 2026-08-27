from __future__ import annotations

from dataclasses import dataclass
import json as json_lib
import re
from typing import Any
from urllib.parse import urlparse

XAI_API_BASE_URL = "https://api.x.ai/v1"
XAI_MEDIA_PROVIDER_IDS = frozenset({"xai", "xai_oauth"})
XAI_MEDIA_CONNECT_TIMEOUT = 10.0
XAI_MEDIA_POOL_TIMEOUT = 10.0
XAI_MEDIA_WRITE_TIMEOUT = 120.0
XAI_IMAGE_GENERATION_READ_TIMEOUT = 600.0
XAI_MEDIA_DOWNLOAD_READ_TIMEOUT = 120.0


class XAIMediaError(RuntimeError):
    def __init__(self, message: str, *, provider_id: str = "", status_code: int = 0, kind: str = "") -> None:
        super().__init__(message)
        self.provider_id = provider_id
        self.status_code = int(status_code or 0)
        self.kind = kind


@dataclass(frozen=True)
class XAIAuthContext:
    provider_id: str
    label: str
    base_url: str
    headers: dict[str, str]
    oauth: bool = False
    refresh_token: str = ""


def is_xai_media_provider(provider_id: str) -> bool:
    return _canonical_provider_id(provider_id) in XAI_MEDIA_PROVIDER_IDS


def xai_media_label(provider_id: str) -> str:
    provider = _canonical_provider_id(provider_id)
    if provider == "xai_oauth":
        return "xAI Grok"
    if provider == "xai":
        return "xAI"
    return provider or "xAI"


def xai_media_auth_context(provider_id: str, *, refresh_if_needed: bool = True) -> XAIAuthContext:
    provider = _canonical_provider_id(provider_id)
    if provider == "xai":
        from row_bot.api_keys import get_key

        api_key = str(get_key("XAI_API_KEY") or "").strip()
        if not api_key:
            raise XAIMediaError(
                "No xAI API key configured. Please add your xAI API key in Settings -> Providers.",
                provider_id=provider,
                kind="missing_api_key",
            )
        return XAIAuthContext(
            provider_id=provider,
            label="xAI",
            base_url=XAI_API_BASE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            oauth=False,
        )
    if provider == "xai_oauth":
        from row_bot.providers import xai_oauth as xai_auth

        block_message = xai_auth.xai_oauth_runtime_block_message(refresh_if_needed=refresh_if_needed)
        if block_message:
            raise XAIMediaError(
                block_message,
                provider_id=provider,
                kind="oauth_unavailable",
            )
        credentials = xai_auth.xai_oauth_runtime_credentials(refresh_if_needed=refresh_if_needed)
        if not credentials.access_token:
            raise XAIMediaError(
                xai_auth.xai_oauth_reconnect_message("xAI OAuth access token is missing."),
                provider_id=provider,
                kind="oauth_unavailable",
            )
        return XAIAuthContext(
            provider_id=provider,
            label="xAI Grok",
            base_url=xai_auth.xai_oauth_base_url(),
            headers={
                "Authorization": f"Bearer {credentials.access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": xai_auth.xai_oauth_user_agent(),
            },
            oauth=True,
            refresh_token=credentials.refresh_token,
        )
    raise XAIMediaError(
        f"Unknown xAI media provider '{provider_id}'. Select xai or xai_oauth.",
        provider_id=provider,
        kind="unknown_provider",
    )


def xai_media_json_request(
    provider_id: str,
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    timeout: float = XAI_MEDIA_DOWNLOAD_READ_TIMEOUT,
    http_client: Any | None = None,
) -> dict[str, Any]:
    response = _xai_media_raw_request(
        provider_id,
        method,
        path,
        json=json,
        timeout=timeout,
        http_client=http_client,
        authenticated=True,
    )
    try:
        payload = response.json()
    except Exception as exc:
        raise XAIMediaError(
            f"{xai_media_label(provider_id)} media response was not JSON: {_redact_detail(str(exc))}",
            provider_id=_canonical_provider_id(provider_id),
            kind="invalid_json",
        ) from exc
    if not isinstance(payload, dict):
        raise XAIMediaError(
            f"{xai_media_label(provider_id)} media response was not a JSON object.",
            provider_id=_canonical_provider_id(provider_id),
            kind="invalid_json",
        )
    return payload


def xai_media_get(
    provider_id: str,
    path_or_url: str,
    *,
    timeout: float = XAI_MEDIA_DOWNLOAD_READ_TIMEOUT,
    follow_redirects: bool = False,
    http_client: Any | None = None,
) -> Any:
    authenticated = not _is_absolute_url(path_or_url)
    return _xai_media_raw_request(
        provider_id,
        "GET",
        path_or_url,
        timeout=timeout,
        follow_redirects=follow_redirects,
        http_client=http_client,
        authenticated=authenticated,
    )


def _xai_media_raw_request(
    provider_id: str,
    method: str,
    path_or_url: str,
    *,
    json: dict[str, Any] | None = None,
    timeout: float = XAI_MEDIA_DOWNLOAD_READ_TIMEOUT,
    follow_redirects: bool = False,
    http_client: Any | None = None,
    authenticated: bool = True,
) -> Any:
    import httpx

    provider = _canonical_provider_id(provider_id)
    if provider not in XAI_MEDIA_PROVIDER_IDS:
        raise XAIMediaError(
            f"Unknown xAI media provider '{provider_id}'. Select xai or xai_oauth.",
            provider_id=provider,
            kind="unknown_provider",
        )

    ctx = xai_media_auth_context(provider, refresh_if_needed=True) if authenticated else None
    url = _request_url(ctx.base_url if ctx else XAI_API_BASE_URL, path_or_url)
    owns_client = http_client is None
    client = http_client or _new_http_client(timeout)
    try:
        try:
            response = _send_request(
                client,
                method,
                url,
                headers=dict(ctx.headers) if ctx else {},
                json=json,
                timeout=_httpx_timeout(timeout),
                follow_redirects=follow_redirects,
            )
            if authenticated and ctx and ctx.oauth and _status_code(response) == 401:
                ctx = _refresh_oauth_context_once(ctx)
                response = _send_request(
                    client,
                    method,
                    url,
                    headers=dict(ctx.headers),
                    json=json,
                    timeout=_httpx_timeout(timeout),
                    follow_redirects=follow_redirects,
                )
        except httpx.TimeoutException as exc:
            raise XAIMediaError(
                f"{xai_media_label(provider)} media request timed out; no provider result was received, "
                "so completion is unknown. The request was not retried.",
                provider_id=provider,
                kind="timeout",
            ) from exc
        _raise_for_media_status(provider, response)
        return response
    finally:
        if owns_client and hasattr(client, "close"):
            client.close()


def _refresh_oauth_context_once(ctx: XAIAuthContext) -> XAIAuthContext:
    from row_bot.providers import xai_oauth as xai_auth

    if not ctx.refresh_token:
        raise XAIMediaError(
            xai_auth.xai_oauth_reconnect_message("xAI OAuth refresh token is missing."),
            provider_id=ctx.provider_id,
            status_code=401,
            kind="oauth_reconnect_required",
        )
    try:
        refreshed = xai_auth.refresh_xai_oauth_token(ctx.refresh_token)
        xai_auth.save_xai_oauth_tokens(refreshed)
    except Exception as exc:
        raise XAIMediaError(
            xai_auth.xai_oauth_reconnect_message(_redact_detail(str(exc))),
            provider_id=ctx.provider_id,
            status_code=401,
            kind="oauth_reconnect_required",
        ) from exc
    return xai_media_auth_context(ctx.provider_id, refresh_if_needed=False)


def _raise_for_media_status(provider_id: str, response: Any) -> None:
    status_code = _status_code(response)
    if status_code < 400:
        return
    detail = _safe_response_text(response)
    if provider_id == "xai_oauth":
        from row_bot.providers import xai_oauth as xai_auth

        if status_code == 401:
            raise XAIMediaError(
                xai_auth.xai_oauth_reconnect_message(detail),
                provider_id=provider_id,
                status_code=status_code,
                kind="oauth_reconnect_required",
            )
        if status_code == 403:
            raise XAIMediaError(
                xai_auth.xai_oauth_entitlement_message(detail),
                provider_id=provider_id,
                status_code=status_code,
                kind="entitlement_denied",
            )
    label = xai_media_label(provider_id)
    suffix = f": {detail}" if detail else ""
    raise XAIMediaError(
        f"{label} media request failed with HTTP {status_code}{suffix}",
        provider_id=provider_id,
        status_code=status_code,
        kind="http_error",
    )


def _send_request(
    client: Any,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    json: dict[str, Any] | None,
    timeout: Any,
    follow_redirects: bool,
) -> Any:
    kwargs: dict[str, Any] = {
        "headers": headers,
        "timeout": timeout,
    }
    if json is not None:
        kwargs["json"] = json
    if follow_redirects:
        kwargs["follow_redirects"] = True
    request_method = str(method or "GET").upper()
    if hasattr(client, "request"):
        return client.request(request_method, url, **kwargs)
    fn = getattr(client, request_method.lower())
    return fn(url, **kwargs)


def _new_http_client(timeout: float) -> Any:
    import httpx

    return httpx.Client(timeout=_httpx_timeout(timeout))


def _httpx_timeout(read_timeout: float) -> Any:
    import httpx

    return httpx.Timeout(
        connect=XAI_MEDIA_CONNECT_TIMEOUT,
        read=float(read_timeout),
        write=XAI_MEDIA_WRITE_TIMEOUT,
        pool=XAI_MEDIA_POOL_TIMEOUT,
    )


def _request_url(base_url: str, path_or_url: str) -> str:
    raw = str(path_or_url or "").strip()
    if _is_absolute_url(raw):
        return raw
    return f"{str(base_url or XAI_API_BASE_URL).rstrip('/')}/{raw.lstrip('/')}"


def _is_absolute_url(value: str) -> bool:
    parsed = urlparse(str(value or ""))
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _status_code(response: Any) -> int:
    try:
        return int(getattr(response, "status_code", 0) or 0)
    except Exception:
        return 0


def _safe_response_text(response: Any) -> str:
    text = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                text = str(error.get("message") or error.get("code") or error)
            elif error:
                text = str(error)
            elif payload.get("message"):
                text = str(payload.get("message"))
            else:
                text = json_lib.dumps(payload, separators=(",", ":"))
    except Exception:
        try:
            text = str(getattr(response, "text", "") or "")
        except Exception:
            text = ""
    return _redact_detail(text)


def _redact_detail(value: str, *, limit: int = 300) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", text)
    text = re.sub(
        r"(?i)(\"?(?:access_token|refresh_token|id_token|authorization|api_key)\"?\s*[:=]\s*)\"?[^\",\s}]+",
        r"\1[redacted]",
        text,
    )
    return text[:limit]


def _canonical_provider_id(provider_id: str) -> str:
    provider = str(provider_id or "").strip()
    try:
        from row_bot.providers.xai_oauth import normalize_xai_oauth_provider_id

        provider = normalize_xai_oauth_provider_id(provider)
    except Exception:
        pass
    return provider
