"""Cookie helpers for Row-Bot mobile owner sessions."""

from __future__ import annotations

from http.cookies import SimpleCookie
from typing import Mapping

from starlette.requests import Request
from starlette.responses import Response

HTTPS_COOKIE_NAME = "__Host-row_bot_mobile"
HTTP_LAN_COOKIE_NAME = "row_bot_mobile_lan"
HTTPS_MAX_AGE_SECONDS = 60 * 60 * 24 * 60
HTTP_LAN_MAX_AGE_SECONDS = 60 * 60 * 24 * 7


def cookie_name_for_scheme(scheme: str) -> str:
    return HTTPS_COOKIE_NAME if str(scheme or "").lower() == "https" else HTTP_LAN_COOKIE_NAME


def cookie_settings_for_scheme(scheme: str) -> dict[str, object]:
    secure = str(scheme or "").lower() == "https"
    return {
        "key": HTTPS_COOKIE_NAME if secure else HTTP_LAN_COOKIE_NAME,
        "path": "/",
        "httponly": True,
        "secure": secure,
        "samesite": "lax",
        "max_age": HTTPS_MAX_AGE_SECONDS if secure else HTTP_LAN_MAX_AGE_SECONDS,
    }


def extract_cookie_from_header(cookie_header: str | bytes | None, *, scheme: str = "http") -> str:
    if not cookie_header:
        return ""
    header = cookie_header.decode("latin-1", errors="ignore") if isinstance(cookie_header, bytes) else str(cookie_header)
    parsed = SimpleCookie()
    try:
        parsed.load(header)
    except Exception:
        return ""
    preferred = cookie_name_for_scheme(scheme)
    for name in (preferred, HTTPS_COOKIE_NAME, HTTP_LAN_COOKIE_NAME):
        morsel = parsed.get(name)
        if morsel is not None and morsel.value:
            return morsel.value
    return ""


def extract_mobile_cookie(request: Request) -> str:
    preferred = cookie_name_for_scheme(request.url.scheme)
    return request.cookies.get(preferred) or request.cookies.get(HTTPS_COOKIE_NAME) or request.cookies.get(HTTP_LAN_COOKIE_NAME) or ""


def set_mobile_session_cookie(response: Response, token: str, *, scheme: str) -> None:
    settings = cookie_settings_for_scheme(scheme)
    response.set_cookie(value=token, **settings)


def clear_mobile_session_cookies(response: Response) -> None:
    for name in (HTTPS_COOKIE_NAME, HTTP_LAN_COOKIE_NAME):
        response.delete_cookie(key=name, path="/")


def cookie_debug_summary(scheme: str) -> Mapping[str, object]:
    settings = cookie_settings_for_scheme(scheme)
    return {
        "name": settings["key"],
        "httponly": settings["httponly"],
        "secure": settings["secure"],
        "samesite": settings["samesite"],
        "path": settings["path"],
        "max_age": settings["max_age"],
    }
