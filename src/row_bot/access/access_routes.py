"""Durable listen settings and side-effect-free remote route inventory."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import inspect
from ipaddress import IPv4Address, IPv6Address, ip_address
import json
import os
from pathlib import Path
import socket
import tempfile
from typing import Any
from urllib.parse import urlsplit

from row_bot.access.config import AccessConfigError, canonical_host, canonical_origin
from row_bot.data_paths import get_row_bot_data_dir

ROUTE_CONFIG_VERSION = 2
ROUTE_CONFIG_FILENAME = "access_routes.json"
DEFAULT_LISTEN_HOST = "127.0.0.1"
LAN_LISTEN_HOST = "0.0.0.0"


class AccessRouteConfigError(ValueError):
    """Raised when durable route configuration is malformed."""


class ListenMode(StrEnum):
    LOCAL_ONLY = "local_only"
    LOCAL_NETWORK = "local_network"


class AccessRouteKind(StrEnum):
    LOCALHOST = "localhost"
    TAILSCALE = "tailscale"
    LAN = "lan"
    NGROK = "ngrok"
    REVERSE_PROXY = "reverse_proxy"
    CURRENT_SERVER = "current_server"


_ROUTE_PRIORITY = {
    AccessRouteKind.TAILSCALE: 0,
    AccessRouteKind.REVERSE_PROXY: 1,
    AccessRouteKind.CURRENT_SERVER: 2,
    AccessRouteKind.LAN: 3,
    AccessRouteKind.NGROK: 4,
    AccessRouteKind.LOCALHOST: 9,
}

_PRIVATE_LAN_WARNING = (
    "LAN HTTP is unencrypted. Row-Bot authentication is still required."
)
_NON_PRIVATE_LAN_WARNING = (
    "This interface address is outside private IP ranges and may be externally "
    "routable. LAN HTTP is unencrypted; verify firewall exposure and prefer HTTPS "
    "or Tailscale. Row-Bot authentication is still required."
)


def _normalize_configured_origins(values: Iterable[object]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise AccessRouteConfigError(
            "configured remote access origins must be a collection"
        )
    normalized: list[str] = []
    for value in values:
        try:
            origin = canonical_origin(value)
        except AccessConfigError as exc:
            raise AccessRouteConfigError(
                "configured remote access origin is malformed"
            ) from exc
        if "*" in urlsplit(origin).netloc:
            raise AccessRouteConfigError(
                "configured remote access origins must use an exact host"
            )
        if origin not in normalized:
            normalized.append(origin)
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class AccessRouteConfig:
    version: int = ROUTE_CONFIG_VERSION
    listen_mode: ListenMode = ListenMode.LOCAL_ONLY
    configured_origins: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "configured_origins",
            _normalize_configured_origins(self.configured_origins),
        )

    @property
    def lan_enabled(self) -> bool:
        return self.listen_mode is ListenMode.LOCAL_NETWORK

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "listen_mode": self.listen_mode.value,
            "configured_origins": list(self.configured_origins),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AccessRouteConfig:
        try:
            version = int(value.get("version", 0))
            listen_mode = ListenMode(str(value.get("listen_mode") or ""))
        except (TypeError, ValueError) as exc:
            raise AccessRouteConfigError(
                "remote access route config is malformed"
            ) from exc
        if version not in {1, ROUTE_CONFIG_VERSION}:
            raise AccessRouteConfigError(
                "unsupported remote access route config version"
            )
        configured_origins: object = (
            () if version == 1 else value.get("configured_origins", ())
        )
        if not isinstance(configured_origins, (list, tuple)):
            raise AccessRouteConfigError(
                "configured remote access origins must be a list"
            )
        return cls(
            version=ROUTE_CONFIG_VERSION,
            listen_mode=listen_mode,
            configured_origins=tuple(configured_origins),
        )


class AccessRouteConfigStore:
    """Small atomic JSON store readable before the app database starts."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = (
            Path(path)
            if path is not None
            else get_row_bot_data_dir(create=False) / ROUTE_CONFIG_FILENAME
        )

    def load(self) -> AccessRouteConfig:
        if not self.path.exists():
            return AccessRouteConfig()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AccessRouteConfigError(
                "remote access route config could not be read"
            ) from exc
        if not isinstance(value, Mapping):
            raise AccessRouteConfigError("remote access route config must be an object")
        return AccessRouteConfig.from_dict(value)

    def load_or_default(self) -> AccessRouteConfig:
        try:
            return self.load()
        except AccessRouteConfigError:
            return AccessRouteConfig()

    def save(self, config: AccessRouteConfig) -> None:
        normalized = AccessRouteConfig.from_dict(config.to_dict())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = (
            json.dumps(normalized.to_dict(), indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        temporary_path: Path | None = None
        try:
            descriptor, raw_path = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
            )
            temporary_path = Path(raw_path)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    temporary_path.chmod(0o600)
                except OSError:
                    pass
                os.replace(temporary_path, self.path)
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
        finally:
            if temporary_path is not None and temporary_path.exists():
                try:
                    temporary_path.unlink()
                except OSError:
                    pass

    def set_listen_mode(self, mode: ListenMode | str) -> AccessRouteConfig:
        previous = self.load_or_default()
        config = AccessRouteConfig(
            listen_mode=ListenMode(mode),
            configured_origins=previous.configured_origins,
        )
        self.save(config)
        return config

    def add_configured_origin(self, origin: object) -> AccessRouteConfig:
        previous = self.load_or_default()
        config = AccessRouteConfig(
            listen_mode=previous.listen_mode,
            configured_origins=(*previous.configured_origins, str(origin or "")),
        )
        self.save(config)
        return config

    def remove_configured_origin(self, origin: object) -> AccessRouteConfig:
        normalized = _normalize_configured_origins((origin,))[0]
        previous = self.load_or_default()
        config = AccessRouteConfig(
            listen_mode=previous.listen_mode,
            configured_origins=tuple(
                saved
                for saved in previous.configured_origins
                if saved != normalized
            ),
        )
        self.save(config)
        return config


@dataclass(frozen=True, slots=True)
class ListenHostResolution:
    host: str
    source: str


def resolve_listen_host(
    *,
    explicit_host: str | None = None,
    environ: Mapping[str, str] | None = None,
    config: AccessRouteConfig | None = None,
) -> ListenHostResolution:
    """Resolve explicit CLI > environment > durable UI config > safe default."""
    env = os.environ if environ is None else environ
    if explicit_host:
        return ListenHostResolution(
            canonical_host(explicit_host, allow_port=False),
            "explicit",
        )
    environment_host = str(env.get("ROW_BOT_HOST") or "").strip()
    if environment_host:
        return ListenHostResolution(
            canonical_host(environment_host, allow_port=False),
            "environment",
        )
    if config is not None and config.listen_mode is ListenMode.LOCAL_NETWORK:
        return ListenHostResolution(LAN_LISTEN_HOST, "durable")
    return ListenHostResolution(DEFAULT_LISTEN_HOST, "default")


@dataclass(frozen=True, slots=True)
class ListenModeChange:
    config: AccessRouteConfig
    changed: bool
    restarted: bool
    restart_required: bool
    reason: str


RestartChild = Callable[[], bool | None | Awaitable[bool | None]]


async def apply_listen_mode(
    store: AccessRouteConfigStore,
    mode: ListenMode | str,
    *,
    restart_child: RestartChild | None = None,
    timeout_seconds: float = 10.0,
) -> ListenModeChange:
    """Persist a listen choice, then request an injected launcher restart."""
    import asyncio

    selected_mode = ListenMode(mode)
    previous = store.load_or_default()
    changed = previous.listen_mode is not selected_mode
    config = store.set_listen_mode(selected_mode) if changed else previous
    if not changed:
        return ListenModeChange(
            config=config,
            changed=False,
            restarted=False,
            restart_required=False,
            reason="unchanged",
        )
    if restart_child is None:
        return ListenModeChange(
            config=config,
            changed=True,
            restarted=False,
            restart_required=True,
            reason="launcher_unavailable",
        )
    try:
        result = restart_child()
        if inspect.isawaitable(result):
            result = await asyncio.wait_for(result, timeout=max(0.1, timeout_seconds))
        restarted = result is not False
    except TimeoutError:
        return ListenModeChange(
            config=config,
            changed=True,
            restarted=False,
            restart_required=True,
            reason="restart_timeout",
        )
    except Exception:
        return ListenModeChange(
            config=config,
            changed=True,
            restarted=False,
            restart_required=True,
            reason="restart_failed",
        )
    return ListenModeChange(
        config=config,
        changed=True,
        restarted=restarted,
        restart_required=not restarted,
        reason="restarted" if restarted else "restart_declined",
    )


@dataclass(frozen=True, slots=True)
class AccessRoute:
    id: str
    kind: AccessRouteKind
    label: str
    origin: str
    available: bool
    private: bool
    enabled: bool
    detail: str
    warning: str | None = None
    owned: bool = False
    eligible: bool = False
    priority: int = 99

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "label": self.label,
            "origin": self.origin,
            "available": self.available,
            "private": self.private,
            "enabled": self.enabled,
            "detail": self.detail,
            "warning": self.warning,
            "owned": self.owned,
            "eligible": self.eligible,
            "priority": self.priority,
        }


@dataclass(frozen=True, slots=True)
class AccessRouteInventory:
    routes: tuple[AccessRoute, ...]

    @property
    def available(self) -> tuple[AccessRoute, ...]:
        return tuple(route for route in self.routes if route.available)

    def by_kind(self, kind: AccessRouteKind | str) -> tuple[AccessRoute, ...]:
        selected = AccessRouteKind(kind)
        return tuple(route for route in self.routes if route.kind is selected)

    @property
    def invitation_routes(self) -> tuple[AccessRoute, ...]:
        return tuple(
            route
            for route in self.routes
            if route.available and route.eligible and route.origin
        )

    def preferred_invitation_route(self) -> AccessRoute | None:
        candidates = self.invitation_routes
        if not candidates:
            return None
        best_priority = min(route.priority for route in candidates)
        preferred = tuple(
            route for route in candidates if route.priority == best_priority
        )
        # Never silently select between equally preferred routes. This matters
        # most for hosts with several LAN/VPN/virtual adapters.
        return preferred[0] if len(preferred) == 1 else None

    def preferred_invitation_origin(self) -> str | None:
        route = self.preferred_invitation_route()
        return route.origin if route is not None else None

    def resolve_invitation_route(self, route_id: object) -> AccessRoute | None:
        selected_id = str(route_id or "").strip()
        if not selected_id:
            return None
        return next(
            (route for route in self.invitation_routes if route.id == selected_id),
            None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "routes": [route.to_dict() for route in self.routes],
            "preferred_invitation_origin": self.preferred_invitation_origin(),
        }


def build_route_inventory(
    *,
    port: int,
    config: AccessRouteConfig | None = None,
    lan_addresses: Iterable[str] = (),
    tailscale_state: object | None = None,
    ngrok_url: str | None = None,
    reverse_proxy_origins: Iterable[str] = (),
    current_server_origin: str | None = None,
) -> AccessRouteInventory:
    """Build route candidates from injected, already-verified state.

    ``current_server_origin`` must have passed the normal request Host/proxy
    validation before it reaches this side-effect-free builder.
    """
    if not 1 <= int(port) <= 65535:
        raise ValueError("port must be within 1..65535")
    selected_config = config or AccessRouteConfig()
    routes: list[AccessRoute] = [
        AccessRoute(
            id=_route_id(
                AccessRouteKind.LOCALHOST,
                canonical_origin(f"http://127.0.0.1:{int(port)}"),
            ),
            kind=AccessRouteKind.LOCALHOST,
            label="This computer",
            origin=canonical_origin(f"http://127.0.0.1:{int(port)}"),
            available=True,
            private=True,
            enabled=True,
            detail="Direct desktop access on this computer.",
            eligible=False,
            priority=_ROUTE_PRIORITY[AccessRouteKind.LOCALHOST],
        )
    ]

    tailscale_route = _tailscale_route(tailscale_state)
    if tailscale_route is not None:
        routes.append(tailscale_route)

    for raw_address in lan_addresses:
        address = _usable_interface_address(raw_address)
        if address is None:
            continue
        host = f"[{address.compressed}]" if address.version == 6 else address.compressed
        origin = canonical_origin(f"http://{host}:{int(port)}")
        is_private = address.is_private
        routes.append(
            AccessRoute(
                id=_route_id(AccessRouteKind.LAN, origin),
                kind=AccessRouteKind.LAN,
                label=f"Local network — {_origin_authority(origin, include_port=False)}",
                origin=origin,
                available=selected_config.lan_enabled,
                private=is_private,
                enabled=selected_config.lan_enabled,
                detail=(
                    "Address assigned to this computer; reachability depends on the "
                    "network and firewall."
                ),
                warning=(
                    _PRIVATE_LAN_WARNING if is_private else _NON_PRIVATE_LAN_WARNING
                ),
                eligible=True,
                priority=_ROUTE_PRIORITY[AccessRouteKind.LAN],
            )
        )

    normalized_ngrok = _optional_origin(ngrok_url)
    if normalized_ngrok:
        routes.append(
            AccessRoute(
                id=_route_id(AccessRouteKind.NGROK, normalized_ngrok),
                kind=AccessRouteKind.NGROK,
                label=(
                    "Public tunnel — "
                    f"{_origin_authority(normalized_ngrok, include_port=True)}"
                ),
                origin=normalized_ngrok,
                available=True,
                private=False,
                enabled=True,
                detail="Temporary public tunnel managed separately from private routes.",
                warning=(
                    "The public endpoint is reachable from the internet; Row-Bot "
                    "authentication is still required."
                ),
                eligible=True,
                priority=_ROUTE_PRIORITY[AccessRouteKind.NGROK],
            )
        )

    for raw_origin in reverse_proxy_origins:
        origin = _optional_origin(raw_origin)
        if not origin:
            continue
        routes.append(
            AccessRoute(
                id=_route_id(AccessRouteKind.REVERSE_PROXY, origin),
                kind=AccessRouteKind.REVERSE_PROXY,
                label=(
                    f"{'HTTPS' if origin.startswith('https://') else 'HTTP'} address"
                    f" — {_origin_authority(origin, include_port=True)}"
                ),
                origin=origin,
                available=True,
                private=origin.startswith("https://"),
                enabled=True,
                detail="Configured browser-facing origin.",
                warning=(
                    None
                    if origin.startswith("https://")
                    else "This address uses unencrypted HTTP."
                ),
                eligible=True,
                priority=_ROUTE_PRIORITY[AccessRouteKind.REVERSE_PROXY],
            )
        )

    normalized_current = _optional_origin(current_server_origin)
    if normalized_current:
        routes.append(
            AccessRoute(
                id=_route_id(AccessRouteKind.CURRENT_SERVER, normalized_current),
                kind=AccessRouteKind.CURRENT_SERVER,
                label=(
                    "Current server address — "
                    f"{_origin_authority(normalized_current, include_port=True)}"
                ),
                origin=normalized_current,
                available=True,
                private=_origin_is_private(normalized_current),
                enabled=True,
                detail="Verified browser-facing address for this Row-Bot server.",
                warning=(
                    None
                    if normalized_current.startswith("https://")
                    else "This server address uses unencrypted HTTP."
                ),
                eligible=True,
                priority=_ROUTE_PRIORITY[AccessRouteKind.CURRENT_SERVER],
            )
        )
    return AccessRouteInventory(routes=_finalize_routes(routes))


def discover_private_lan_addresses() -> tuple[str, ...]:
    """Read usable assigned interface addresses without DNS or network probes."""
    try:
        import psutil
    except ImportError:
        return ()
    addresses: list[str] = []
    try:
        interface_addresses = psutil.net_if_addrs()
    except Exception:
        return ()
    for entries in interface_addresses.values():
        for entry in entries:
            if entry.family not in {socket.AF_INET, socket.AF_INET6}:
                continue
            address = _usable_interface_address(entry.address)
            if address is None:
                continue
            normalized = address.compressed
            if normalized not in addresses:
                addresses.append(normalized)
    return tuple(addresses)


def _usable_interface_address(
    value: object,
) -> IPv4Address | IPv6Address | None:
    """Return an assigned unicast address suitable for an exact LAN route."""
    raw = str(value or "").strip().split("%", 1)[0]
    try:
        address = ip_address(raw)
    except ValueError:
        return None
    if (
        address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
    ):
        return None
    return address


def _optional_origin(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return canonical_origin(text)
    except AccessConfigError:
        return None


def _route_id(kind: AccessRouteKind, origin: str) -> str:
    digest = hashlib.sha256(f"{kind.value}\0{origin}".encode("utf-8")).hexdigest()[:16]
    return f"{kind.value}-{digest}"


def _origin_authority(origin: str, *, include_port: bool) -> str:
    parsed = urlsplit(origin)
    if include_port:
        return parsed.netloc
    host = str(parsed.hostname or "")
    try:
        address = ip_address(host)
    except ValueError:
        return host
    return f"[{address.compressed}]" if address.version == 6 else address.compressed


def _origin_is_private(origin: str) -> bool:
    try:
        return ip_address(str(urlsplit(origin).hostname or "")).is_private
    except ValueError:
        return False


def _route_sort_key(route: AccessRoute) -> tuple[int, int, str, str]:
    return (
        0 if route.available and route.eligible else 1,
        route.priority,
        route.origin,
        route.id,
    )


def _finalize_routes(routes: Iterable[AccessRoute]) -> tuple[AccessRoute, ...]:
    """Globally deduplicate canonical origins and make labels deterministic."""
    deduplicated: list[AccessRoute] = []
    seen_origins: set[str] = set()
    for route in sorted(routes, key=_route_sort_key):
        if route.origin:
            if route.origin in seen_origins:
                continue
            seen_origins.add(route.origin)
        deduplicated.append(route)

    label_counts: dict[str, int] = {}
    for route in deduplicated:
        label_counts[route.label] = label_counts.get(route.label, 0) + 1
    finalized: list[AccessRoute] = []
    for route in deduplicated:
        if label_counts[route.label] == 1:
            finalized.append(route)
            continue
        scheme = (
            urlsplit(route.origin).scheme.upper() if route.origin else route.kind.value
        )
        finalized.append(
            AccessRoute(
                id=route.id,
                kind=route.kind,
                label=f"{route.label} ({scheme})",
                origin=route.origin,
                available=route.available,
                private=route.private,
                enabled=route.enabled,
                detail=route.detail,
                warning=route.warning,
                owned=route.owned,
                eligible=route.eligible,
                priority=route.priority,
            )
        )
    return tuple(finalized)


def _tailscale_route(state: object | None) -> AccessRoute | None:
    if state is None:
        return None
    if isinstance(state, Mapping):
        status = str(state.get("status") or state.get("state") or "unknown").lower()
        raw_origin = (
            state.get("serve_url")
            or state.get("origin")
            or state.get("https_origin")
            or state.get("url")
        )
        owned = bool(state.get("owned") or state.get("row_bot_owned"))
    else:
        status = str(
            getattr(state, "status", None) or getattr(state, "state", None) or "unknown"
        ).lower()
        raw_origin = (
            getattr(state, "serve_url", None)
            or getattr(state, "origin", None)
            or getattr(state, "https_origin", None)
            or getattr(state, "url", None)
        )
        owned = bool(
            getattr(state, "owned", False) or getattr(state, "row_bot_owned", False)
        )
    origin = _optional_origin(raw_origin)
    active = status in {"active", "active_owned", "active_unowned"} and bool(origin)
    return AccessRoute(
        id=(
            _route_id(AccessRouteKind.TAILSCALE, origin)
            if origin
            else "tailscale-unavailable"
        ),
        kind=AccessRouteKind.TAILSCALE,
        label=(
            f"Tailscale — {_origin_authority(origin, include_port=True)}"
            if origin
            else "Tailscale — Recommended"
        ),
        origin=origin or "",
        available=active,
        private=True,
        enabled=active,
        detail=(
            "Private tailnet HTTPS route is active."
            if active
            else "Tailscale is optional and needs explicit setup."
        ),
        warning=(
            "This route is not owned by Row-Bot and will not be changed automatically."
            if active and not owned
            else None
        ),
        owned=owned,
        eligible=True,
        priority=_ROUTE_PRIORITY[AccessRouteKind.TAILSCALE],
    )


__all__ = [
    "AccessRoute",
    "AccessRouteConfig",
    "AccessRouteConfigError",
    "AccessRouteConfigStore",
    "AccessRouteInventory",
    "AccessRouteKind",
    "ListenHostResolution",
    "ListenMode",
    "ListenModeChange",
    "apply_listen_mode",
    "build_route_inventory",
    "discover_private_lan_addresses",
    "resolve_listen_host",
]
