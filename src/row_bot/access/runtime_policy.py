"""Thread-safe runtime access policy for Row-Bot-managed proxy origins."""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_network
import threading
from typing import Mapping
from urllib.parse import urlsplit

from row_bot.access.config import (
    AccessConfig,
    AccessConfigError,
    canonical_host,
    canonical_origin,
)

MANAGED_PROXY_LOOPBACK_CIDRS = ("127.0.0.1/32", "::1/128")


def canonical_managed_https_origin(value: object) -> str:
    """Return one exact HTTPS origin suitable for a managed tunnel."""

    origin = canonical_origin(value)
    parsed = urlsplit(origin)
    if parsed.scheme != "https":
        raise AccessConfigError("managed tunnel origin must use https")
    authority = canonical_host(parsed.netloc)
    if "*" in authority:
        raise AccessConfigError("managed tunnel origin must use an exact host")
    return f"https://{authority}"


@dataclass(frozen=True, slots=True)
class RuntimeAccessPolicySnapshot:
    """One coherent immutable access-policy view."""

    base_config: AccessConfig
    managed_origins: tuple[str, ...]
    managed_route_configs: tuple[tuple[str, AccessConfig], ...] = ()

    def config_for_scope(self, scope: Mapping[str, object]) -> AccessConfig:
        """Select an exact managed-route overlay or the immutable base config."""

        raw_hosts = [
            bytes(value).decode("latin-1", errors="ignore")
            for name, value in scope.get("headers", []) or []
            if bytes(name).lower() == b"host"
        ]
        if len(raw_hosts) != 1:
            return self.base_config
        try:
            authority = canonical_host(raw_hosts[0])
            candidate_origin = canonical_origin(f"https://{authority}")
        except AccessConfigError:
            return self.base_config
        for managed_origin, config in self.managed_route_configs:
            if candidate_origin == managed_origin:
                return config
        return self.base_config


class RuntimeAccessPolicy:
    """Merge exact active managed origins into one immutable base config.

    The registry is process-local and dependency-injected into the singleton
    tunnel manager. It is never persisted and performs no discovery or network
    access. Each mutation replaces the cached immutable snapshot so HTTP and
    WebSocket admission can retain one coherent view per request.
    """

    def __init__(self, base_config: AccessConfig) -> None:
        if not isinstance(base_config, AccessConfig):
            raise TypeError("base_config must be an AccessConfig")
        self._base_config = base_config
        self._managed_origins: set[str] = set()
        self._lock = threading.RLock()
        self._snapshot = RuntimeAccessPolicySnapshot(base_config, ())

    @property
    def base_config(self) -> AccessConfig:
        return self._base_config

    def snapshot(self) -> RuntimeAccessPolicySnapshot:
        """Return the current immutable policy snapshot without side effects."""

        with self._lock:
            return self._snapshot

    def register_managed_origin(self, url: object) -> str:
        """Register one exact managed HTTPS origin and return its canonical form."""

        origin = canonical_managed_https_origin(url)
        with self._lock:
            if origin not in self._managed_origins:
                self._managed_origins.add(origin)
                self._rebuild_snapshot_locked()
        return origin

    def unregister_managed_origin(self, url: object) -> bool:
        """Remove one exact managed origin, returning whether it was present."""

        origin = canonical_managed_https_origin(url)
        with self._lock:
            if origin not in self._managed_origins:
                return False
            self._managed_origins.remove(origin)
            self._rebuild_snapshot_locked()
            return True

    def clear_managed_origins(self) -> None:
        """Remove every ephemeral managed origin while preserving base config."""

        with self._lock:
            if not self._managed_origins:
                return
            self._managed_origins.clear()
            self._rebuild_snapshot_locked()

    def _rebuild_snapshot_locked(self) -> None:
        managed_origins = tuple(sorted(self._managed_origins))
        if not managed_origins:
            self._snapshot = RuntimeAccessPolicySnapshot(self._base_config, ())
            return

        loopback_networks = tuple(
            ip_network(cidr, strict=True) for cidr in MANAGED_PROXY_LOOPBACK_CIDRS
        )
        managed_route_configs = tuple(
            (
                origin,
                AccessConfig(
                    deployment_mode=self._base_config.deployment_mode,
                    trusted_proxy_cidrs=loopback_networks,
                    allowed_hosts=(authority,),
                    public_origins=(origin,),
                    untrusted_forwarded_action=(
                        self._base_config.untrusted_forwarded_action
                    ),
                ),
            )
            for origin in managed_origins
            for authority in (canonical_host(urlsplit(origin).netloc),)
        )
        self._snapshot = RuntimeAccessPolicySnapshot(
            self._base_config,
            managed_origins,
            managed_route_configs,
        )


__all__ = [
    "MANAGED_PROXY_LOOPBACK_CIDRS",
    "RuntimeAccessPolicy",
    "RuntimeAccessPolicySnapshot",
    "canonical_managed_https_origin",
]
