"""Access URL candidate helpers for Row-Bot Mobile Access settings."""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
import socket
from typing import Any

from row_bot.app_port import get_app_host, get_app_port
from row_bot.mobile.tailscale import TailscaleState

_BIND_HOST_UNSET = object()


@dataclass(frozen=True)
class AccessCandidate:
    id: str
    label: str
    url: str
    access_mode: str
    available: bool
    security: str
    detail: str
    warning: str = ""
    requires_bind: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "url": self.url,
            "access_mode": self.access_mode,
            "available": self.available,
            "security": self.security,
            "detail": self.detail,
            "warning": self.warning,
            "requires_bind": self.requires_bind,
        }


def _is_non_loopback_ip(value: str) -> bool:
    try:
        parsed = ip_address(value)
    except ValueError:
        return False
    return not (parsed.is_loopback or parsed.is_link_local or parsed.is_multicast or parsed.is_unspecified)


def discover_lan_ips() -> tuple[str, ...]:
    candidates: list[str] = []
    try:
        hostname = socket.gethostname()
        infos = socket.getaddrinfo(hostname, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
    except OSError:
        infos = []
    for info in infos:
        ip = str(info[4][0])
        if _is_non_loopback_ip(ip) and ip not in candidates:
            candidates.append(ip)
    return tuple(candidates)


def bind_allows_remote(bind_host: str | None) -> bool:
    host = str(bind_host or "").strip().lower()
    if host in {"0.0.0.0", "::"}:
        return True
    return _is_non_loopback_ip(host)


def build_access_candidates(
    *,
    port: int | None = None,
    bind_host: str | None | object = _BIND_HOST_UNSET,
    lan_ips: tuple[str, ...] | list[str] | None = None,
    ngrok_url: str | None = None,
    tailscale_state: TailscaleState | None = None,
) -> list[AccessCandidate]:
    resolved_port = get_app_port() if port is None else int(port)
    host = get_app_host() if bind_host is _BIND_HOST_UNSET else bind_host
    allows_remote = bind_allows_remote(host)
    candidates: list[AccessCandidate] = [
        AccessCandidate(
            id="localhost",
            label="Localhost desktop",
            url=f"http://127.0.0.1:{resolved_port}",
            access_mode="localhost",
            available=True,
            security="local",
            detail="Direct desktop browser/native-window requests bypass mobile auth only from true localhost.",
        )
    ]

    for ip in lan_ips if lan_ips is not None else discover_lan_ips():
        candidates.append(
            AccessCandidate(
                id=f"lan-{ip}",
                label=f"LAN direct ({ip})",
                url=f"http://{ip}:{resolved_port}",
                access_mode="lan",
                available=allows_remote,
                security="http-lan",
                detail="Requires Row-Bot to bind to a reachable interface and mobile pairing.",
                warning="HTTP LAN traffic is not encrypted. Use only on trusted local networks.",
                requires_bind=not allows_remote,
            )
        )

    if tailscale_state and tailscale_state.installed:
        for ip in tailscale_state.tailscale_ips:
            candidates.append(
                AccessCandidate(
                    id=f"tailscale-{ip}",
                    label=f"Tailscale direct ({ip})",
                    url=f"http://{ip}:{resolved_port}",
                    access_mode="tailscale-direct",
                    available=bool(tailscale_state.logged_in and allows_remote),
                    security="tailnet",
                    detail="Private to your tailnet but still requires mobile pairing.",
                    requires_bind=not allows_remote,
                )
            )
        for url in tailscale_state.magicdns_url_candidates:
            candidates.append(
                AccessCandidate(
                    id=f"tailscale-magicdns-{len(candidates)}",
                    label="Tailscale MagicDNS direct",
                    url=url,
                    access_mode="tailscale-direct",
                    available=bool(tailscale_state.logged_in and allows_remote),
                    security="tailnet",
                    detail="Private MagicDNS URL for devices in your tailnet; requires mobile pairing.",
                    requires_bind=not allows_remote,
                )
            )
        if tailscale_state.serve_url:
            candidates.append(
                AccessCandidate(
                    id="tailscale-serve",
                    label="Tailscale Serve",
                    url=tailscale_state.serve_url,
                    access_mode="tailscale-serve",
                    available=bool(tailscale_state.serve_enabled),
                    security="tailnet-https",
                    detail="Preferred private mode: HTTPS inside your tailnet while Row-Bot can stay localhost-bound.",
                )
            )

    if ngrok_url:
        candidates.append(
            AccessCandidate(
                id="ngrok",
                label="ngrok public tunnel",
                url=ngrok_url.rstrip("/"),
                access_mode="ngrok",
                available=True,
                security="public-https",
                detail="Public HTTPS exposure protected by Row-Bot mobile pairing.",
                warning="Anyone with the URL can reach the pairing gate. Keep the URL private.",
            )
        )
    return candidates


def build_access_info(
    *,
    port: int | None = None,
    bind_host: str | None | object = _BIND_HOST_UNSET,
    lan_ips: tuple[str, ...] | list[str] | None = None,
    ngrok_url: str | None = None,
    tailscale_state: TailscaleState | None = None,
) -> dict[str, Any]:
    resolved_port = get_app_port() if port is None else int(port)
    host = get_app_host() if bind_host is _BIND_HOST_UNSET else bind_host
    candidates = build_access_candidates(
        port=resolved_port,
        bind_host=host,
        lan_ips=lan_ips,
        ngrok_url=ngrok_url,
        tailscale_state=tailscale_state,
    )
    return {
        "port": resolved_port,
        "bind_host": host or "localhost",
        "remote_bind_enabled": bind_allows_remote(host),
        "candidates": [candidate.to_dict() for candidate in candidates],
    }
