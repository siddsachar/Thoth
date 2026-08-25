"""Managed Browser navigation and consequential-action policy."""

from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import parse_qsl, urlparse, urlunparse


_B64_SEGMENT_RE = re.compile(r"[A-Za-z0-9+/=]{100,}")


def history_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    except Exception:
        return "[invalid URL]"


def navigation_policy(url: str, current_url: str = "") -> tuple[str, str]:
    try:
        parsed = urlparse(url)
    except Exception:
        return "block", "Malformed URL."
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "block", "Only explicit HTTP(S) origins are allowed."
    if parsed.username or parsed.password:
        return "block", "Credentials in URLs are not allowed."
    query_and_fragment = parsed.query + (parsed.fragment or "")
    if len(query_and_fragment) > 500 or _B64_SEGMENT_RE.search(query_and_fragment):
        return "block", "The URL may contain encoded data or an unusually long query."
    sensitive = re.compile(r"(?:token|secret|password|passwd|api[_-]?key|auth|session|otp|code)", re.I)
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if sensitive.search(key) and value:
            return "ask", f"URL query parameter '{key}' may contain sensitive data."
    host = parsed.hostname.strip("[]")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if host.casefold() == "localhost" or (address and (address.is_private or address.is_loopback or address.is_link_local)):
        return "ask", "Navigation targets a local or private-network origin."
    if current_url:
        current = urlparse(current_url)
        old_origin = (current.scheme.casefold(), (current.hostname or "").casefold(), current.port)
        new_origin = (parsed.scheme.casefold(), (parsed.hostname or "").casefold(), parsed.port)
        if current.hostname and old_origin != new_origin and parsed.query:
            return "ask", f"Cross-origin navigation with query data: {old_origin[1]} -> {new_origin[1]}."
    return "allow", ""


def consequential_browser_target(metadata: dict[str, Any], *, submit: bool = False) -> str:
    from row_bot.computer_use.policy import is_consequential_label

    label = " ".join(str(metadata.get(key) or "") for key in ("label", "type", "role", "href", "form_action"))
    if submit:
        return "Submitting a form may create an external side effect."
    if str(metadata.get("type") or "").casefold() == "file":
        return "File upload controls require approval."
    if metadata.get("download"):
        return "Downloads require approval."
    if is_consequential_label(label):
        return "The selected page control may create an external side effect."
    return ""
