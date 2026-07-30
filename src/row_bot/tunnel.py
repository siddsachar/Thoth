"""
Row-Bot – Tunnel Manager
=======================
Provider-agnostic tunnel infrastructure for exposing local webhook
ports to the internet.  Channels that need inbound webhooks (e.g. SMS /
Twilio) call ``tunnel_manager.start_tunnel(port)`` to obtain a public
HTTPS URL, and ``tunnel_manager.stop_tunnel(port)`` on shutdown.

Architecture
------------
- **TunnelProvider** – ABC that each backend (ngrok, cloudflare, …)
  implements.
- **NgrokProvider** – Concrete provider using *pyngrok*.
- **TunnelManager** – Thread-safe singleton that delegates to the
  active provider.

The module-level ``tunnel_manager`` instance is created at import time
but stays dormant (no process spawned) until ``start_tunnel()`` is
called.
"""

from __future__ import annotations

import logging
import os
import threading
from abc import ABC, abstractmethod
from typing import Protocol

log = logging.getLogger(__name__)


def _ngrok_authtoken() -> str:
    """Return the ngrok token from Row-Bot's normal secret sources."""
    try:
        from row_bot.api_keys import get_key

        return get_key("NGROK_AUTHTOKEN").strip()
    except Exception as exc:
        log.debug("Unable to read NGROK_AUTHTOKEN from key store: %s", exc)
        return os.environ.get("NGROK_AUTHTOKEN", "").strip()


def ngrok_configuration_status() -> tuple[str, str]:
    """Return ``(status_code, detail)`` for ngrok readiness diagnostics."""
    try:
        import pyngrok  # noqa: F401
    except ImportError:
        return ("error", "pyngrok is not installed. Run: pip install pyngrok")

    try:
        from row_bot.api_keys import get_key, key_status

        if get_key("NGROK_AUTHTOKEN").strip():
            return ("ok", "ngrok available")
        status = key_status("NGROK_AUTHTOKEN")
        if status.get("configured"):
            return (
                "error",
                "ngrok authtoken metadata exists, but the keyring secret is unreadable. "
                "Clear and re-save the authtoken in Settings > System > Tunnel Settings.",
            )
    except Exception as exc:
        log.debug("Unable to inspect NGROK_AUTHTOKEN status: %s", exc)

    if os.environ.get("NGROK_AUTHTOKEN"):
        return ("ok", "ngrok available")
    return ("inactive", "NGROK_AUTHTOKEN is not set")


# ── Exceptions ───────────────────────────────────────────────────────

class TunnelError(Exception):
    """Raised when the tunnel provider cannot start or encounters a
    fatal configuration / connectivity problem."""


class ManagedOriginRegistrar(Protocol):
    """Runtime access-policy boundary used by managed tunnel lifecycles."""

    def register_managed_origin(self, url: object) -> str: ...

    def unregister_managed_origin(self, url: object) -> bool: ...


# ── Provider ABC ─────────────────────────────────────────────────────

class TunnelProvider(ABC):
    """Abstract base for tunnel providers (ngrok, cloudflare, tailscale, …)."""

    @abstractmethod
    def start(self, port: int, label: str = "") -> str:
        """Open a tunnel to *port*.  Return the public HTTPS URL."""

    @abstractmethod
    def stop(self, port: int) -> None:
        """Close the tunnel for *port*."""

    @abstractmethod
    def stop_all(self) -> None:
        """Close every tunnel managed by this provider."""

    @abstractmethod
    def get_url(self, port: int) -> str | None:
        """Current public URL for *port*, or ``None``."""

    @abstractmethod
    def is_available(self) -> bool:
        """``True`` when this provider is configured and ready."""

    @abstractmethod
    def active_tunnels(self) -> dict[int, str]:
        """Map of port → public URL for all active tunnels."""


# ── Ngrok provider ───────────────────────────────────────────────────

class NgrokProvider(TunnelProvider):
    """Uses *pyngrok* to auto-manage ngrok tunnels.

    ``pyngrok`` auto-downloads the ngrok binary on first use, so no
    manual install is required.
    """

    def __init__(self) -> None:
        self._tunnels: dict = {}  # port → pyngrok NgrokTunnel object

    # ── Provider interface ───────────────────────────────────────────

    def is_available(self) -> bool:
        try:
            import pyngrok  # noqa: F401
            return bool(_ngrok_authtoken())
        except ImportError:
            return False

    def start(self, port: int, label: str = "") -> str:
        if port in self._tunnels:
            return self._tunnels[port].public_url

        try:
            from pyngrok import ngrok, conf
        except ImportError:
            raise TunnelError(
                "pyngrok is not installed. Run: pip install pyngrok"
            )

        token = _ngrok_authtoken()
        if not token:
            raise TunnelError(
                "NGROK_AUTHTOKEN not set — configure it in "
                "Settings → System → Tunnel Settings"
            )

        try:
            pyngrok_config = conf.get_default()
            pyngrok_config.auth_token = token
            log.info("Starting ngrok tunnel: port %d%s", port, f" [{label}]" if label else "")
            tunnel = ngrok.connect(port, bind_tls=True)
            self._tunnels[port] = tunnel
            log.info("ngrok tunnel opened: port %d → %s%s",
                     port, tunnel.public_url,
                     f" [{label}]" if label else "")
            return tunnel.public_url
        except Exception as exc:
            raise TunnelError(f"ngrok failed to open tunnel on port {port}: {exc}") from exc

    def stop(self, port: int) -> None:
        tunnel = self._tunnels.pop(port, None)
        if tunnel:
            try:
                from pyngrok import ngrok
                ngrok.disconnect(tunnel.public_url)
                log.info("ngrok tunnel closed: port %d", port)
            except Exception as exc:
                log.warning("Error closing ngrok tunnel on port %d: %s", port, exc)

    def stop_all(self) -> None:
        try:
            from pyngrok import ngrok
            ngrok.kill()
        except Exception as exc:
            log.warning("Error killing ngrok process: %s", exc)
        self._tunnels.clear()
        log.info("All ngrok tunnels closed")

    def get_url(self, port: int) -> str | None:
        t = self._tunnels.get(port)
        return t.public_url if t else None

    def active_tunnels(self) -> dict[int, str]:
        return {p: t.public_url for p, t in self._tunnels.items()}


# ── Tunnel Manager (singleton wrapper) ───────────────────────────────

class TunnelManager:
    """Thread-safe facade wrapping the active :class:`TunnelProvider`.

    Channels interact with this class — never with providers directly.
    """

    def __init__(
        self,
        *,
        managed_origin_registrar: ManagedOriginRegistrar | None = None,
    ) -> None:
        self._provider: TunnelProvider | None = None
        self._managed_origin_registrar = managed_origin_registrar
        self._registered_origins: dict[int, str] = {}
        self._lock = threading.Lock()

    # ── Provider management ──────────────────────────────────────────

    def set_provider(self, provider: TunnelProvider) -> None:
        with self._lock:
            if self._registered_origins:
                raise TunnelError(
                    "cannot replace the tunnel provider while managed tunnels are active"
                )
            self._provider = provider

    def set_managed_origin_registrar(
        self,
        registrar: ManagedOriginRegistrar,
    ) -> None:
        """Inject the process-local access policy before any tunnel can start."""

        with self._lock:
            if self._registered_origins:
                raise TunnelError(
                    "cannot replace the access policy while managed tunnels are active"
                )
            self._managed_origin_registrar = registrar

    def _ensure_provider(self) -> None:
        """Lazy-init: create the default provider if none set yet."""
        if self._provider is not None:
            return
        try:
            from row_bot.channels import config as ch_config
            name = ch_config.get("tunnel", "provider", "ngrok")
        except Exception:
            name = "ngrok"
        if name == "ngrok":
            self._provider = NgrokProvider()
        else:
            log.warning("Unknown tunnel provider '%s', falling back to ngrok", name)
            self._provider = NgrokProvider()

    # ── Public API ───────────────────────────────────────────────────

    def start_tunnel(self, port: int, label: str = "") -> str:
        """Open a tunnel for *port*.  Returns the public HTTPS URL.

        Raises :class:`TunnelError` on failure.
        """
        with self._lock:
            self._ensure_provider()
            registrar = self._managed_origin_registrar
            if registrar is None:
                raise TunnelError(
                    "managed tunnel access policy is unavailable; restart Row-Bot"
                )

            existing_origin = self._registered_origins.get(port)
            existing_url = self._provider.get_url(port)
            if existing_origin is not None and existing_url is not None:
                return existing_origin

            started_new = existing_url is None
            public_url = self._provider.start(port, label)
            try:
                origin = registrar.register_managed_origin(public_url)
            except Exception as exc:
                if started_new:
                    try:
                        self._provider.stop(port)
                    except Exception:
                        log.warning(
                            "Unable to close tunnel after access-policy rejection "
                            "on port %d",
                            port,
                            exc_info=True,
                        )
                raise TunnelError(
                    "managed tunnel URL was rejected by the access policy"
                ) from exc

            previous = self._registered_origins.get(port)
            self._registered_origins[port] = origin
            if previous is not None and previous != origin:
                registrar.unregister_managed_origin(previous)
            return origin

    def stop_tunnel(self, port: int) -> None:
        """Close the tunnel for *port* (no-op if not open)."""
        with self._lock:
            origin = self._registered_origins.pop(port, None)
            try:
                if self._provider:
                    self._provider.stop(port)
            finally:
                if origin is not None and self._managed_origin_registrar is not None:
                    self._managed_origin_registrar.unregister_managed_origin(origin)

    def stop_all(self) -> None:
        """Close **all** active tunnels and kill the provider process."""
        with self._lock:
            origins = tuple(self._registered_origins.values())
            self._registered_origins.clear()
            try:
                if self._provider:
                    self._provider.stop_all()
            finally:
                registrar = self._managed_origin_registrar
                if registrar is not None:
                    for origin in origins:
                        registrar.unregister_managed_origin(origin)

    def get_url(self, port: int) -> str | None:
        """Return the current public URL for *port*, or ``None``."""
        with self._lock:
            return self._registered_origins.get(port)

    def is_available(self) -> bool:
        """``True`` if the provider is configured and ready to create
        tunnels (e.g. authtoken is set and library is installed)."""
        with self._lock:
            self._ensure_provider()
            return self._provider.is_available()

    def active_tunnels(self) -> dict[int, str]:
        """Map of port → public URL for every open tunnel."""
        with self._lock:
            return dict(self._registered_origins)

    def status(self) -> tuple[str, str]:
        """Return ``(status_code, detail)`` for health-check display.

        *status_code* is one of ``"ok"``, ``"inactive"``, ``"error"``.
        """
        try:
            with self._lock:
                self._ensure_provider()
                config_status, config_detail = ngrok_configuration_status()
                if config_status != "ok":
                    return (config_status, config_detail)
                active = dict(self._registered_origins)
                if active:
                    urls = ", ".join(f"{p}→{u}" for p, u in active.items())
                    return ("ok", f"{len(active)} active: {urls}")
                return ("inactive", "Ready (no active tunnels)")
        except Exception as exc:
            return ("error", str(exc))


# ── Module-level singleton ───────────────────────────────────────────

tunnel_manager = TunnelManager()


def kill_stale_ngrok() -> None:
    """Kill any orphaned ngrok processes from previous runs.

    Called once at app startup to avoid hitting the free-tier session
    limit (3 simultaneous agents).
    """
    import subprocess as _sp
    import sys as _sys
    try:
        if _sys.platform == "win32":
            # taskkill is idempotent — returns 0 even if no process found
            _sp.run(
                ["taskkill", "/F", "/IM", "ngrok.exe"],
                capture_output=True, timeout=5,
            )
        else:
            _sp.run(["pkill", "-f", "ngrok"], capture_output=True, timeout=5)
        log.info("Killed stale ngrok processes (if any)")
    except Exception as exc:
        log.debug("ngrok cleanup skipped: %s", exc)
