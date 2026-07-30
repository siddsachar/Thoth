"""Consent-gated management of one Row-Bot Tailscale Serve route.

Detection and planning execute read-only Tailscale commands.  Configuration is
changed only by :meth:`TailscaleServeController.apply` or
:meth:`TailscaleServeController.disable_owned`.  The controller records a
fingerprint of the complete Serve configuration after creation and refuses to
remove the listener if that configuration later changes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit
from uuid import uuid4

from row_bot.access.config import AccessConfig, parse_trusted_proxy_cidrs
from row_bot.data_paths import get_row_bot_data_dir


READ_ONLY_TIMEOUT_SECONDS = 4.0
MUTATION_TIMEOUT_SECONDS = 30.0
RECONCILIATION_TIMEOUT_SECONDS = 12.0
RECONCILIATION_BACKOFF_SECONDS = (0.25, 0.5, 1.0, 2.0)
# Kept as a compatibility alias for callers that still pass ``timeout=``.
DEFAULT_TIMEOUT_SECONDS = READ_ONLY_TIMEOUT_SECONDS
MAX_COMMAND_OUTPUT_CHARS = 64 * 1024
OWNERSHIP_SCHEMA_VERSION = 1
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_CONSENT_URL_RE = re.compile(
    r"https://login\.tailscale\.com/[^\s\"'<>]+",
    re.IGNORECASE,
)
_SIGNED_OUT_MARKERS = (
    "needslogin",
    "needs login",
    "not logged in",
    "logged out",
    "login.tailscale.com/start",
)
_DAEMON_MARKERS = (
    "failed to connect to local tailscaled",
    "cannot connect to local tailscaled",
    "tailscaled is not running",
    "no such file or directory",
    'backendstate": "stopped',
)
_UNSUPPORTED_MARKERS = (
    "unknown flag",
    "unknown command",
    "flag provided but not defined",
    "serve is not supported",
)


class TailscaleState(StrEnum):
    """Stable states rendered by Remote Access settings."""

    CLI_NOT_FOUND = "cli_not_found"
    SIGNED_OUT = "signed_out"
    DAEMON_UNAVAILABLE = "daemon_unavailable"
    READY = "ready"
    CONSENT_REQUIRED = "consent_required"
    ROUTE_CONFLICT = "route_conflict"
    ACTIVE_OWNED = "active_owned"
    ACTIVE_UNOWNED = "active_unowned"
    FUNNEL_ACTIVE = "funnel_active"
    UNSUPPORTED_CLI = "unsupported_cli"
    OUTCOME_UNVERIFIED = "outcome_unverified"
    ERROR = "error"


class TailscalePlanAction(StrEnum):
    """Non-mutating recommendation returned by :meth:`plan`."""

    ENABLE = "enable"
    ALREADY_ACTIVE = "already_active"
    INSTALL_REQUIRED = "install_required"
    SIGN_IN_REQUIRED = "sign_in_required"
    RETRY_REQUIRED = "retry_required"
    CONSENT_REQUIRED = "consent_required"
    RESOLVE_CONFLICT = "resolve_conflict"
    INSPECT_MANUALLY = "inspect_manually"
    REFUSE_FUNNEL = "refuse_funnel"
    UPGRADE_REQUIRED = "upgrade_required"


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Bounded result of one argv-based subprocess call."""

    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


CommandRunner = Callable[[Sequence[str], float], CommandResult]


@dataclass(frozen=True, slots=True)
class TailscaleRoute:
    """Normalized portion of a Tailscale Serve configuration."""

    origin: str
    path: str
    target: str
    endpoint: str


@dataclass(frozen=True, slots=True)
class TailscaleOwnership:
    """Durable proof of the exact Serve configuration Row-Bot created."""

    schema_version: int
    config_fingerprint: str
    origin: str
    target: str
    path: str
    https_port: int

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "config_fingerprint": self.config_fingerprint,
            "origin": self.origin,
            "target": self.target,
            "path": self.path,
            "https_port": self.https_port,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> TailscaleOwnership:
        schema_version = int(value.get("schema_version") or 0)
        https_port = int(value.get("https_port") or 0)
        fingerprint = str(value.get("config_fingerprint") or "")
        origin = str(value.get("origin") or "")
        target = str(value.get("target") or "")
        path = str(value.get("path") or "")
        if (
            schema_version != OWNERSHIP_SCHEMA_VERSION
            or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
            or not _verified_https_origin(origin)
            or not _is_loopback_target(target)
            or path != "/"
            or https_port != 443
        ):
            raise ValueError("invalid Tailscale ownership record")
        return cls(
            schema_version=schema_version,
            config_fingerprint=fingerprint,
            origin=origin,
            target=target,
            path=path,
            https_port=https_port,
        )


def augment_access_config_for_owned_tailscale(
    config: AccessConfig,
    *,
    ownership: TailscaleOwnership | Mapping[str, object] | None,
    app_port: int,
) -> AccessConfig:
    """Trust an exact owned Serve route when it targets this app instance.

    This helper is deliberately pure: startup code supplies already-loaded
    ownership data, and no Tailscale command, file write, or network operation
    occurs here.
    """

    if ownership is None:
        return config
    try:
        raw_ownership = (
            ownership.to_dict()
            if isinstance(ownership, TailscaleOwnership)
            else ownership
        )
        if not isinstance(raw_ownership, Mapping):
            return config
        verified = TailscaleOwnership.from_dict(raw_ownership)
        expected_port = int(app_port)
        if isinstance(app_port, bool) or not 1 <= expected_port <= 65535:
            return config
        parsed_target = urlsplit(verified.target)
        if parsed_target.hostname != "127.0.0.1" or parsed_target.port != expected_port:
            return config
        host = _normalize_dns_name(urlsplit(verified.origin).hostname or "")
        if not host or not host.endswith(".ts.net"):
            return config
    except (TypeError, ValueError):
        return config

    loopback_proxies = parse_trusted_proxy_cidrs(("127.0.0.1/32", "::1/128"))
    trusted_proxy_cidrs = tuple(
        dict.fromkeys((*config.trusted_proxy_cidrs, *loopback_proxies))
    )
    allowed_hosts = tuple(dict.fromkeys((*config.allowed_hosts, host)))
    public_origins = tuple(dict.fromkeys((*config.public_origins, verified.origin)))
    return replace(
        config,
        trusted_proxy_cidrs=trusted_proxy_cidrs,
        allowed_hosts=allowed_hosts,
        public_origins=public_origins,
    )


@dataclass(frozen=True, slots=True)
class TailscaleStatus:
    """Read-only status with private URLs available only as explicit fields."""

    state: TailscaleState
    binary: str | None = None
    backend_state: str = ""
    dns_name: str = ""
    serve_url: str = ""
    consent_url: str = ""
    routes: tuple[TailscaleRoute, ...] = ()
    config_fingerprint: str = ""
    config_complete: bool = False
    detail: str = ""

    @property
    def installed(self) -> bool:
        return self.state is not TailscaleState.CLI_NOT_FOUND

    @property
    def signed_in(self) -> bool:
        return self.state not in {
            TailscaleState.CLI_NOT_FOUND,
            TailscaleState.SIGNED_OUT,
            TailscaleState.DAEMON_UNAVAILABLE,
        }

    @property
    def owned(self) -> bool:
        return self.state is TailscaleState.ACTIVE_OWNED

    def to_public_dict(self) -> dict[str, object]:
        """Return status data without subprocess output or ownership internals."""

        return {
            "state": self.state.value,
            "installed": self.installed,
            "signed_in": self.signed_in,
            "serve_url": self.serve_url,
            "consent_url": self.consent_url,
            "owned": self.owned,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class VerifiedTailscaleStatus:
    """One explicit Tailscale probe retained for this process only."""

    status: TailscaleStatus
    verified_at: datetime
    ownership: TailscaleOwnership | None


class TailscaleStatusCache:
    """Process-local cache for explicitly verified Tailscale status.

    Callers provide the current validated ownership record on every read and
    write. A changed or malformed ownership record invalidates the snapshot;
    this cache never substitutes for the controller's exact ownership checks.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._snapshots: dict[tuple[str, int], VerifiedTailscaleStatus] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _key(instance_key: object, port: int) -> tuple[str, int]:
        normalized_instance = str(instance_key or "").strip()
        normalized_port = int(port)
        if (
            not normalized_instance
            or isinstance(port, bool)
            or not 1 <= normalized_port <= 65535
        ):
            raise ValueError("instance key and port must identify one Row-Bot app")
        return normalized_instance, normalized_port

    @staticmethod
    def _cacheable(
        status: object,
        ownership: TailscaleOwnership | None,
    ) -> bool:
        if not isinstance(status, TailscaleStatus):
            return False
        if status.state not in {
            TailscaleState.ACTIVE_OWNED,
            TailscaleState.ACTIVE_UNOWNED,
        }:
            return True

        origin = _verified_https_origin(status.serve_url, dns_name=status.dns_name)
        matching_routes = tuple(
            route
            for route in status.routes
            if route.origin == origin and route.path == "/" and bool(route.target)
        )
        if not origin or origin != status.serve_url or len(matching_routes) != 1:
            return False
        if status.state is TailscaleState.ACTIVE_UNOWNED:
            return True
        return bool(
            ownership
            and len(status.routes) == 1
            and status.config_complete
            and status.config_fingerprint == ownership.config_fingerprint
            and origin == ownership.origin
            and matching_routes[0].target == ownership.target
            and matching_routes[0].path == ownership.path
            and ownership.https_port == 443
        )

    def remember(
        self,
        *,
        instance_key: object,
        port: int,
        status: object | None,
        ownership: TailscaleOwnership | None,
    ) -> VerifiedTailscaleStatus | None:
        """Replace one snapshot, or invalidate it for an unverified value."""

        key = self._key(instance_key, port)
        with self._lock:
            if not self._cacheable(status, ownership):
                self._snapshots.pop(key, None)
                return None
            verified_at = self._clock()
            if verified_at.tzinfo is None:
                verified_at = verified_at.replace(tzinfo=timezone.utc)
            snapshot = VerifiedTailscaleStatus(
                status=status,
                verified_at=verified_at.astimezone(timezone.utc),
                ownership=ownership,
            )
            self._snapshots[key] = snapshot
            return snapshot

    def get(
        self,
        *,
        instance_key: object,
        port: int,
        ownership: TailscaleOwnership | None,
    ) -> VerifiedTailscaleStatus | None:
        """Return a snapshot only while its ownership context is unchanged."""

        key = self._key(instance_key, port)
        with self._lock:
            snapshot = self._snapshots.get(key)
            if (
                snapshot is None
                or snapshot.ownership != ownership
                or not self._cacheable(snapshot.status, ownership)
            ):
                self._snapshots.pop(key, None)
                return None
            return snapshot

    def invalidate(self, *, instance_key: object, port: int) -> None:
        """Forget one app instance without touching durable ownership."""

        key = self._key(instance_key, port)
        with self._lock:
            self._snapshots.pop(key, None)


@dataclass(frozen=True, slots=True)
class TailscaleServePlan:
    """A pure description of the next action; constructing it never mutates."""

    action: TailscalePlanAction
    status: TailscaleStatus
    port: int
    target: str
    binary: str = ""
    dns_name: str = ""
    baseline_fingerprint: str = ""
    https_port: int = 443
    cli_version: str = ""
    command: tuple[str, ...] = ()
    description: str = ""

    @property
    def can_apply(self) -> bool:
        return self.action is TailscalePlanAction.ENABLE and bool(self.command)


@dataclass(frozen=True, slots=True)
class TailscaleOperationResult:
    """Result returned after an explicitly requested mutation."""

    success: bool
    status: TailscaleStatus
    ownership: TailscaleOwnership | None = None
    error: str = ""
    restarted: bool = False
    restart_required: bool = False
    restart_reason: str = ""


def _bounded(value: object, limit: int = MAX_COMMAND_OUTPUT_CHARS) -> str:
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value or "")
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n[output truncated]"


def redact_command_detail(value: object) -> str:
    """Return bounded, single-line command detail with every URL removed."""

    text = _URL_RE.sub("[redacted-url]", _bounded(value, 2_000))
    return " ".join(text.replace("\x00", "").split())[:500]


def _default_runner(argv: Sequence[str], timeout: float) -> CommandResult:
    completed = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        shell=False,
    )
    return CommandResult(
        returncode=completed.returncode,
        stdout=_bounded(completed.stdout),
        stderr=_bounded(completed.stderr),
    )


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _target_for_port(port: int) -> str:
    if isinstance(port, bool) or not 1 <= int(port) <= 65535:
        raise ValueError("port must be in the range 1..65535")
    return f"http://127.0.0.1:{int(port)}"


def _is_loopback_target(value: object) -> bool:
    try:
        parsed = urlsplit(str(value))
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname == "127.0.0.1"
        and port is not None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
    )


def _normalize_target(value: object) -> str:
    text = str(value or "").strip().rstrip("/")
    if text.startswith("127.0.0.1:"):
        text = f"http://{text}"
    if not _is_loopback_target(text):
        return text
    parsed = urlsplit(text)
    return f"http://127.0.0.1:{parsed.port}"


def _normalize_dns_name(value: object) -> str:
    text = str(value or "").strip().rstrip(".").lower()
    if (
        not text
        or "://" in text
        or any(char.isspace() for char in text)
        or any(char in text for char in "/\\@?#")
    ):
        return ""
    return text


def _verified_https_origin(value: object, *, dns_name: str = "") -> str:
    text = str(value or "").strip()
    if "://" not in text:
        text = f"https://{text}"
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        return ""
    host = _normalize_dns_name(parsed.hostname or "")
    expected = _normalize_dns_name(dns_name)
    if (
        parsed.scheme.lower() != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        return ""
    if expected:
        if host != expected:
            return ""
    elif not host.endswith(".ts.net"):
        return ""
    return f"https://{host}"


def _extract_consent_url(value: object) -> str:
    match = _CONSENT_URL_RE.search(_bounded(value, 8_000))
    if not match:
        return ""
    return match.group(0).rstrip(").,;")


def _extract_result_consent_url(result: CommandResult) -> str:
    """Inspect each bounded stream so a large stdout cannot hide stderr consent."""

    return _extract_consent_url(result.stdout) or _extract_consent_url(result.stderr)


def _contains_marker(value: object, markers: Sequence[str]) -> bool:
    lowered = str(value or "").casefold()
    return any(marker in lowered for marker in markers)


def _truthy_nested(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(_truthy_nested(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_truthy_nested(item) for item in value)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().casefold() not in {"", "false", "off", "none", "0"}


def _casefold_get(value: Mapping[str, Any], key: str) -> Any:
    wanted = key.casefold()
    for current, item in value.items():
        if str(current).casefold() == wanted:
            return item
    return None


@dataclass(frozen=True, slots=True)
class _NodeStatus:
    state: TailscaleState | None
    backend_state: str
    dns_name: str
    detail: str = ""


def _parse_node_status_json(payload: object) -> _NodeStatus:
    if isinstance(payload, str):
        parsed = json.loads(_bounded(payload))
    else:
        parsed = payload
    if not isinstance(parsed, Mapping):
        raise ValueError("tailscale status JSON is not an object")

    backend_state = str(parsed.get("BackendState") or "").strip()
    lowered = backend_state.casefold().replace("_", "")
    self_info = parsed.get("Self")
    if not isinstance(self_info, Mapping):
        self_info = {}
    dns_name = _normalize_dns_name(
        self_info.get("DNSName") or self_info.get("HostName") or ""
    )

    if lowered in {"needslogin", "needsmachineauth"}:
        return _NodeStatus(TailscaleState.SIGNED_OUT, backend_state, dns_name)
    if lowered in {"stopped", "nostate"}:
        return _NodeStatus(
            TailscaleState.DAEMON_UNAVAILABLE,
            backend_state,
            dns_name,
        )
    if lowered in {"running", "starting"} or self_info:
        return _NodeStatus(None, backend_state or "Running", dns_name)
    return _NodeStatus(
        TailscaleState.DAEMON_UNAVAILABLE,
        backend_state,
        dns_name,
        "Tailscale daemon is not ready.",
    )


def _parse_node_status_text(payload: object) -> _NodeStatus:
    text = _bounded(payload)
    if _contains_marker(text, _SIGNED_OUT_MARKERS):
        return _NodeStatus(TailscaleState.SIGNED_OUT, "NeedsLogin", "")
    if _contains_marker(text, _DAEMON_MARKERS):
        return _NodeStatus(TailscaleState.DAEMON_UNAVAILABLE, "Stopped", "")
    if re.search(r"(?m)^\s*100\.\d+\.\d+\.\d+\s+\S+", text):
        return _NodeStatus(None, "Running", "")
    raise ValueError("could not understand tailscale status output")


@dataclass(frozen=True, slots=True)
class _ServeConfig:
    routes: tuple[TailscaleRoute, ...]
    funnel: bool
    has_config: bool
    fingerprint: str
    complete: bool


def _endpoint_origin(endpoint: object, dns_name: str) -> str:
    text = str(endpoint or "").strip()
    if text.isdigit() and dns_name:
        text = f"{dns_name}:{text}"
    return _verified_https_origin(text, dns_name=dns_name)


def _handler_target(handler: object) -> str:
    if not isinstance(handler, Mapping):
        return ""
    proxy = _casefold_get(handler, "Proxy")
    if isinstance(proxy, str):
        return _normalize_target(proxy)
    if isinstance(proxy, Mapping):
        target = (
            _casefold_get(proxy, "URL")
            or _casefold_get(proxy, "Target")
            or _casefold_get(proxy, "Proxy")
        )
        if target:
            return _normalize_target(target)
    return ""


def parse_serve_status_json(payload: object, *, dns_name: str = "") -> _ServeConfig:
    """Parse the structured ``tailscale serve status --json`` contract."""

    if isinstance(payload, str):
        parsed = json.loads(_bounded(payload))
    else:
        parsed = payload
    if not isinstance(parsed, Mapping):
        raise ValueError("tailscale serve JSON is not an object")

    routes: list[TailscaleRoute] = []
    web = _casefold_get(parsed, "Web")
    if isinstance(web, Mapping):
        for endpoint_value, web_config in web.items():
            origin = _endpoint_origin(endpoint_value, dns_name)
            if not isinstance(web_config, Mapping):
                continue
            handlers = _casefold_get(web_config, "Handlers")
            if not isinstance(handlers, Mapping):
                continue
            for path_value, handler in handlers.items():
                path = str(path_value or "/").strip() or "/"
                if not path.startswith("/"):
                    path = f"/{path}"
                target = _handler_target(handler)
                if not target:
                    target = "[non-proxy-handler]"
                route = TailscaleRoute(
                    origin=origin,
                    path=path,
                    target=target,
                    endpoint=str(endpoint_value),
                )
                if route not in routes:
                    routes.append(route)

    allow_funnel = _casefold_get(parsed, "AllowFunnel")
    funnel = _truthy_nested(allow_funnel)
    tcp = _casefold_get(parsed, "TCP")
    recognized_keys = {"web", "allowfunnel", "tcp"}
    unknown_config = {
        str(key): value
        for key, value in parsed.items()
        if str(key).casefold() not in recognized_keys
    }
    has_config = (
        bool(routes) or _truthy_nested(tcp) or funnel or _truthy_nested(unknown_config)
    )
    web_is_mapping = web is None or isinstance(web, Mapping)
    tcp_is_mapping = tcp is None or isinstance(tcp, Mapping)
    funnel_is_mapping = allow_funnel is None or isinstance(allow_funnel, Mapping)
    complete = (
        web_is_mapping
        and tcp_is_mapping
        and funnel_is_mapping
        and not _truthy_nested(unknown_config)
    )
    if complete and routes:
        tcp_items = list(tcp.items()) if isinstance(tcp, Mapping) else []
        complete = (
            len(routes) == 1
            and len(tcp_items) == 1
            and str(tcp_items[0][0]) == "443"
            and isinstance(tcp_items[0][1], Mapping)
            and _truthy_nested(_casefold_get(tcp_items[0][1], "HTTPS"))
        )
    return _ServeConfig(
        routes=tuple(routes),
        funnel=funnel,
        has_config=has_config,
        fingerprint=_fingerprint(parsed),
        complete=complete,
    )


def parse_serve_status_text(payload: object, *, dns_name: str = "") -> _ServeConfig:
    """Bounded compatibility parser for older human-readable CLI output."""

    text = _bounded(payload)
    routes: list[TailscaleRoute] = []
    current_origin = ""
    for raw_line in text.splitlines()[:1_000]:
        line = raw_line.strip()
        url_match = _URL_RE.search(line)
        if url_match and url_match.group(0).lower().startswith("https://"):
            verified = _verified_https_origin(
                url_match.group(0).rstrip("/"),
                dns_name=dns_name,
            )
            if verified:
                current_origin = verified
        proxy_match = re.search(r"\bproxy\s+(https?://\S+)", line, re.IGNORECASE)
        if proxy_match and current_origin:
            route = TailscaleRoute(
                origin=current_origin,
                path="/",
                target=_normalize_target(proxy_match.group(1)),
                endpoint=urlsplit(current_origin).netloc,
            )
            if route not in routes:
                routes.append(route)

    lowered = text.casefold()
    funnel = bool(
        re.search(r"\bfunnel\b[^\n]*(?:\bon\b|\benabled\b)", lowered)
        or "available on the internet" in lowered
    )
    no_config = any(
        phrase in lowered
        for phrase in (
            "no serve config",
            "serve is not running",
            "no serve configuration",
        )
    )
    has_config = (
        bool(routes) or funnel or "available on your tailnet" in lowered
    ) and not no_config
    normalized = {
        "routes": [
            {
                "origin": route.origin,
                "path": route.path,
                "target": route.target,
                "endpoint": route.endpoint,
            }
            for route in routes
        ],
        "funnel": funnel,
        "has_config": has_config,
    }
    return _ServeConfig(
        routes=tuple(routes),
        funnel=funnel,
        has_config=has_config,
        fingerprint=_fingerprint(normalized),
        complete=False,
    )


def _ownership_matches(
    ownership: TailscaleOwnership | None,
    config: _ServeConfig,
    route: TailscaleRoute,
) -> bool:
    return bool(
        ownership
        and ownership.config_fingerprint == config.fingerprint
        and ownership.origin == route.origin
        and ownership.target == route.target
        and ownership.path == route.path
        and ownership.https_port == 443
    )


class TailscaleOwnershipStore:
    """Small atomic JSON store containing no bearer credential."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (
            get_row_bot_data_dir(create=False) / "tailscale_serve_ownership.json"
        )

    def load(self) -> TailscaleOwnership | None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, Mapping):
                return None
            return TailscaleOwnership.from_dict(value)
        except (
            FileNotFoundError,
            OSError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ):
            return None

    def save(self, ownership: TailscaleOwnership) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(ownership.to_dict(), handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary, self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


@dataclass(frozen=True, slots=True)
class _CliCapabilities:
    supported: bool
    version: str = ""
    detail: str = ""


class TailscaleServeController:
    """Detect and explicitly manage one private HTTPS Serve listener."""

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        which: Callable[[str], str | None] = shutil.which,
        timeout: float | None = None,
        read_timeout: float = READ_ONLY_TIMEOUT_SECONDS,
        mutation_timeout: float = MUTATION_TIMEOUT_SECONDS,
        reconciliation_timeout: float = RECONCILIATION_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        backoff: Sequence[float] = RECONCILIATION_BACKOFF_SECONDS,
        ownership_path: Path | None = None,
    ) -> None:
        # ``timeout`` is the old public spelling and now affects reads only.
        if timeout is not None:
            read_timeout = timeout
        if read_timeout <= 0 or read_timeout > 30:
            raise ValueError(
                "read timeout must be greater than zero and at most 30 seconds"
            )
        if mutation_timeout <= 0 or mutation_timeout > 120:
            raise ValueError(
                "mutation timeout must be greater than zero and at most 120 seconds"
            )
        if reconciliation_timeout <= 0 or reconciliation_timeout > 60:
            raise ValueError(
                "reconciliation timeout must be greater than zero and at most 60 seconds"
            )
        normalized_backoff = tuple(float(value) for value in backoff)
        if not normalized_backoff or any(
            value <= 0 or value > reconciliation_timeout for value in normalized_backoff
        ):
            raise ValueError(
                "backoff must contain positive values within the reconciliation timeout"
            )
        self._runner = runner or _default_runner
        self._which = which
        self._read_timeout = float(read_timeout)
        self._mutation_timeout = float(mutation_timeout)
        self._reconciliation_timeout = float(reconciliation_timeout)
        self._clock = clock
        self._sleeper = sleeper
        self._backoff = normalized_backoff
        self._ownership_store = TailscaleOwnershipStore(ownership_path)

    def _run(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> CommandResult:
        command_timeout = self._read_timeout if timeout is None else timeout
        try:
            result = self._runner(tuple(argv), command_timeout)
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                returncode=124,
                stdout=_bounded(exc.stdout),
                stderr=_bounded(exc.stderr),
                timed_out=True,
            )
        except (FileNotFoundError, OSError) as exc:
            return CommandResult(
                returncode=127,
                stderr=redact_command_detail(exc),
            )
        return CommandResult(
            returncode=int(result.returncode),
            stdout=_bounded(result.stdout),
            stderr=_bounded(result.stderr),
            timed_out=bool(result.timed_out),
        )

    @staticmethod
    def _command_error(result: CommandResult, fallback: str) -> str:
        if result.timed_out:
            return "Tailscale command timed out."
        return redact_command_detail(result.stderr or result.stdout or fallback)

    def _detect_node(self, binary: str) -> _NodeStatus:
        result = self._run((binary, "status", "--json"))
        combined = f"{result.stdout}\n{result.stderr}"
        if result.returncode != 0:
            if _contains_marker(combined, _SIGNED_OUT_MARKERS):
                return _NodeStatus(TailscaleState.SIGNED_OUT, "NeedsLogin", "")
            if _contains_marker(combined, _DAEMON_MARKERS) or result.timed_out:
                return _NodeStatus(
                    TailscaleState.DAEMON_UNAVAILABLE,
                    "Stopped",
                    "",
                    self._command_error(result, "Tailscale daemon is unavailable."),
                )
            if _contains_marker(combined, _UNSUPPORTED_MARKERS):
                fallback = self._run((binary, "status"))
                if fallback.returncode == 0:
                    try:
                        return _parse_node_status_text(fallback.stdout)
                    except ValueError:
                        pass
                return _NodeStatus(
                    TailscaleState.UNSUPPORTED_CLI,
                    "",
                    "",
                    "This Tailscale CLI does not provide a supported status format.",
                )
            return _NodeStatus(
                TailscaleState.ERROR,
                "",
                "",
                self._command_error(result, "Tailscale status failed."),
            )

        try:
            return _parse_node_status_json(result.stdout)
        except (json.JSONDecodeError, TypeError, ValueError):
            fallback = self._run((binary, "status"))
            if fallback.returncode == 0:
                try:
                    return _parse_node_status_text(fallback.stdout)
                except ValueError:
                    pass
            return _NodeStatus(
                TailscaleState.UNSUPPORTED_CLI,
                "",
                "",
                "This Tailscale CLI returned an unsupported status format.",
            )

    def _detect_serve(
        self,
        binary: str,
        *,
        dns_name: str,
    ) -> tuple[_ServeConfig | None, TailscaleStatus | None]:
        result = self._run((binary, "serve", "status", "--json"))
        combined = f"{result.stdout}\n{result.stderr}"
        consent_url = _extract_result_consent_url(result)
        if consent_url:
            return None, TailscaleStatus(
                state=TailscaleState.CONSENT_REQUIRED,
                binary=binary,
                dns_name=dns_name,
                consent_url=consent_url,
                detail="Tailscale requires HTTPS Serve consent.",
            )
        if result.returncode == 0:
            try:
                return parse_serve_status_json(result.stdout, dns_name=dns_name), None
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        if (
            result.returncode == 0
            or _contains_marker(combined, _UNSUPPORTED_MARKERS)
            or "no serve config" in combined.casefold()
        ):
            fallback = self._run((binary, "serve", "status"))
            fallback_combined = f"{fallback.stdout}\n{fallback.stderr}"
            consent_url = _extract_result_consent_url(fallback)
            if consent_url:
                return None, TailscaleStatus(
                    state=TailscaleState.CONSENT_REQUIRED,
                    binary=binary,
                    dns_name=dns_name,
                    consent_url=consent_url,
                    detail="Tailscale requires HTTPS Serve consent.",
                )
            if (
                fallback.returncode == 0
                or "no serve config" in fallback_combined.casefold()
            ):
                return (
                    parse_serve_status_text(
                        fallback.stdout or fallback.stderr,
                        dns_name=dns_name,
                    ),
                    None,
                )
            combined = fallback_combined
            result = fallback

        if _contains_marker(combined, _DAEMON_MARKERS) or result.timed_out:
            state = TailscaleState.DAEMON_UNAVAILABLE
            fallback_detail = "Tailscale daemon is unavailable."
        elif _contains_marker(combined, _UNSUPPORTED_MARKERS):
            state = TailscaleState.UNSUPPORTED_CLI
            fallback_detail = "This Tailscale CLI does not support managed Serve."
        else:
            state = TailscaleState.ERROR
            fallback_detail = "Tailscale Serve status failed."
        return None, TailscaleStatus(
            state=state,
            binary=binary,
            dns_name=dns_name,
            detail=self._command_error(result, fallback_detail),
        )

    def detect(
        self,
        *,
        port: int,
        ownership: TailscaleOwnership | None = None,
    ) -> TailscaleStatus:
        """Read local CLI state without changing Tailscale configuration."""

        target = _target_for_port(port)
        binary = self._which("tailscale")
        if not binary:
            return TailscaleStatus(
                state=TailscaleState.CLI_NOT_FOUND,
                detail="Tailscale is not installed or is not on PATH.",
            )

        node = self._detect_node(binary)
        if node.state is not None:
            return TailscaleStatus(
                state=node.state,
                binary=binary,
                backend_state=node.backend_state,
                dns_name=node.dns_name,
                detail=node.detail,
            )

        config, terminal = self._detect_serve(binary, dns_name=node.dns_name)
        if terminal is not None:
            return replace(terminal, backend_state=node.backend_state)
        assert config is not None

        common = {
            "binary": binary,
            "backend_state": node.backend_state,
            "dns_name": node.dns_name,
            "routes": config.routes,
            "config_fingerprint": config.fingerprint,
            "config_complete": config.complete,
        }
        if config.funnel:
            return TailscaleStatus(
                state=TailscaleState.FUNNEL_ACTIVE,
                detail="Tailscale Funnel is active; Row-Bot will not manage this route.",
                **common,
            )

        matches = tuple(
            route
            for route in config.routes
            if route.path == "/" and route.target == target and bool(route.origin)
        )
        if matches:
            route = matches[0]
            effective_ownership = ownership or self._ownership_store.load()
            state = (
                TailscaleState.ACTIVE_OWNED
                if len(config.routes) == 1
                and _ownership_matches(effective_ownership, config, route)
                else TailscaleState.ACTIVE_UNOWNED
            )
            detail = (
                "Row-Bot's private Tailscale route is active."
                if state is TailscaleState.ACTIVE_OWNED
                else "A matching Tailscale route exists but Row-Bot does not own it."
            )
            return TailscaleStatus(
                state=state,
                serve_url=route.origin,
                detail=detail,
                **common,
            )

        if config.has_config:
            return TailscaleStatus(
                state=TailscaleState.ROUTE_CONFLICT,
                detail="Existing Tailscale Serve configuration must be handled manually.",
                **common,
            )

        return TailscaleStatus(
            state=TailscaleState.READY,
            detail="Tailscale is ready for a private Serve route.",
            **common,
        )

    def _probe_capabilities(self, binary: str) -> _CliCapabilities:
        version_result = self._run((binary, "version", "--json"))
        version = ""
        if version_result.returncode == 0:
            try:
                value = json.loads(version_result.stdout)
                if isinstance(value, Mapping):
                    version = str(
                        value.get("longVersion")
                        or value.get("LongVersion")
                        or value.get("long")
                        or value.get("short")
                        or value.get("majorMinorPatch")
                        or value.get("version")
                        or value.get("Version")
                        or ""
                    ).strip()
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        if not version:
            fallback = self._run((binary, "version"))
            if fallback.returncode == 0:
                version = (fallback.stdout or fallback.stderr).splitlines()[0].strip()

        help_result = self._run((binary, "serve", "--help"))
        help_text = f"{help_result.stdout}\n{help_result.stderr}"
        required_flags = ("--bg", "--yes", "--https")
        supported = (
            help_result.returncode == 0
            and all(flag in help_text for flag in required_flags)
            and "serve" in help_text.casefold()
        )
        if supported:
            return _CliCapabilities(True, version=version)
        return _CliCapabilities(
            False,
            version=version,
            detail=(
                "This Tailscale CLI does not advertise the required "
                "Serve --bg, --yes, and --https flags."
            ),
        )

    @staticmethod
    def _plan_actions(
        status: TailscaleStatus,
    ) -> tuple[TailscalePlanAction, str]:
        actions = {
            TailscaleState.CLI_NOT_FOUND: (
                TailscalePlanAction.INSTALL_REQUIRED,
                "Install Tailscale yourself, then recheck.",
            ),
            TailscaleState.SIGNED_OUT: (
                TailscalePlanAction.SIGN_IN_REQUIRED,
                "Sign in with Tailscale outside Row-Bot, then recheck.",
            ),
            TailscaleState.DAEMON_UNAVAILABLE: (
                TailscalePlanAction.RETRY_REQUIRED,
                "Start or repair the local Tailscale daemon, then recheck.",
            ),
            TailscaleState.CONSENT_REQUIRED: (
                TailscalePlanAction.CONSENT_REQUIRED,
                "Grant HTTPS Serve consent with Tailscale, then recheck.",
            ),
            TailscaleState.ROUTE_CONFLICT: (
                TailscalePlanAction.RESOLVE_CONFLICT,
                "Inspect the existing Serve routes; Row-Bot will not overwrite them.",
            ),
            TailscaleState.ACTIVE_OWNED: (
                TailscalePlanAction.ALREADY_ACTIVE,
                "The Row-Bot-owned private route is already active.",
            ),
            TailscaleState.ACTIVE_UNOWNED: (
                TailscalePlanAction.INSPECT_MANUALLY,
                "The matching route is read-only because Row-Bot did not create it.",
            ),
            TailscaleState.FUNNEL_ACTIVE: (
                TailscalePlanAction.REFUSE_FUNNEL,
                "Funnel is public and cannot be managed from this private access flow.",
            ),
            TailscaleState.UNSUPPORTED_CLI: (
                TailscalePlanAction.UPGRADE_REQUIRED,
                "Upgrade Tailscale or configure Serve manually.",
            ),
            TailscaleState.OUTCOME_UNVERIFIED: (
                TailscalePlanAction.INSPECT_MANUALLY,
                "Inspect Tailscale Serve status before trying another operation.",
            ),
            TailscaleState.ERROR: (
                TailscalePlanAction.RETRY_REQUIRED,
                "Resolve the reported Tailscale error, then recheck.",
            ),
        }
        return actions[status.state]

    def plan(self, *, port: int) -> TailscaleServePlan:
        """Return an exact, non-mutating plan for the current state."""

        target = _target_for_port(port)
        status = self.detect(port=port)
        version = ""
        if status.state is TailscaleState.READY:
            if (
                not status.binary
                or not status.dns_name
                or not status.config_fingerprint
                or not status.config_complete
            ):
                status = replace(
                    status,
                    state=TailscaleState.UNSUPPORTED_CLI,
                    detail=(
                        "Tailscale did not provide complete structured identity "
                        "and Serve status data."
                    ),
                )
            else:
                capabilities = self._probe_capabilities(status.binary)
                version = capabilities.version
                if capabilities.supported:
                    command = (
                        status.binary,
                        "serve",
                        "--bg",
                        "--yes",
                        "--https=443",
                        target,
                    )
                    return TailscaleServePlan(
                        action=TailscalePlanAction.ENABLE,
                        status=status,
                        port=port,
                        target=target,
                        binary=status.binary,
                        dns_name=status.dns_name,
                        baseline_fingerprint=status.config_fingerprint,
                        https_port=443,
                        cli_version=version,
                        command=command,
                        description=(
                            "Create one background, tailnet-only HTTPS Serve route to "
                            f"Row-Bot on local port {port}."
                        ),
                    )
                status = replace(
                    status,
                    state=TailscaleState.UNSUPPORTED_CLI,
                    detail=capabilities.detail,
                )

        action, description = self._plan_actions(status)
        return TailscaleServePlan(
            action=action,
            status=status,
            port=port,
            target=target,
            binary=status.binary or "",
            dns_name=status.dns_name,
            baseline_fingerprint=status.config_fingerprint,
            cli_version=version,
            description=description,
        )

    @staticmethod
    def _exact_enabled(
        status: TailscaleStatus,
        *,
        binary: str,
        dns_name: str,
        target: str,
    ) -> bool:
        expected_origin = _verified_https_origin(dns_name, dns_name=dns_name)
        if (
            status.state
            not in {TailscaleState.ACTIVE_OWNED, TailscaleState.ACTIVE_UNOWNED}
            or status.binary != binary
            or status.dns_name != dns_name
            or status.serve_url != expected_origin
            or len(status.routes) != 1
            or not status.config_complete
            or not status.config_fingerprint
        ):
            return False
        route = status.routes[0]
        return (
            route.origin == expected_origin
            and route.path == "/"
            and route.target == target
        )

    def _poll_again(
        self,
        *,
        deadline: float,
        attempt: int,
        max_attempts: int,
    ) -> bool:
        if attempt >= max_attempts:
            return False
        now = self._clock()
        if now >= deadline:
            return False
        delay = self._backoff[min(attempt - 1, len(self._backoff) - 1)]
        self._sleeper(min(delay, deadline - now))
        return True

    def _max_reconciliation_attempts(self) -> int:
        return 2 + int(self._reconciliation_timeout / min(self._backoff))

    @staticmethod
    def _unverified(status: TailscaleStatus) -> TailscaleStatus:
        return replace(
            status,
            state=TailscaleState.OUTCOME_UNVERIFIED,
            detail=(
                "Tailscale Serve may have changed, but the final state could "
                "not be verified."
            ),
        )

    def _reconcile_enable(
        self,
        plan: TailscaleServePlan,
        mutation: CommandResult,
    ) -> TailscaleOperationResult:
        deadline = self._clock() + self._reconciliation_timeout
        max_attempts = self._max_reconciliation_attempts()
        consent_url = _extract_result_consent_url(mutation)
        last = plan.status
        attempt = 0
        while True:
            attempt += 1
            last = self.detect(port=plan.port, ownership=None)
            if self._exact_enabled(
                last,
                binary=plan.binary,
                dns_name=plan.dns_name,
                target=plan.target,
            ):
                ownership = TailscaleOwnership(
                    schema_version=OWNERSHIP_SCHEMA_VERSION,
                    config_fingerprint=last.config_fingerprint,
                    origin=last.serve_url,
                    target=plan.target,
                    path="/",
                    https_port=plan.https_port,
                )
                try:
                    self._ownership_store.save(ownership)
                except OSError:
                    return TailscaleOperationResult(
                        success=False,
                        status=last,
                        error=(
                            "The route is active, but Row-Bot could not persist "
                            "ownership metadata."
                        ),
                    )
                return TailscaleOperationResult(
                    success=True,
                    status=replace(
                        last,
                        state=TailscaleState.ACTIVE_OWNED,
                        detail="Row-Bot's private Tailscale route is active.",
                    ),
                    ownership=ownership,
                )

            if last.state in {
                TailscaleState.FUNNEL_ACTIVE,
                TailscaleState.ROUTE_CONFLICT,
                TailscaleState.ACTIVE_OWNED,
                TailscaleState.ACTIVE_UNOWNED,
                TailscaleState.CONSENT_REQUIRED,
            }:
                return TailscaleOperationResult(
                    success=False,
                    status=last,
                    error=(
                        "Tailscale reported an unexpected or unsafe Serve state; "
                        "Row-Bot made no cleanup change."
                    ),
                )
            if consent_url:
                return TailscaleOperationResult(
                    success=False,
                    status=replace(
                        last,
                        state=TailscaleState.CONSENT_REQUIRED,
                        consent_url=consent_url,
                        detail="Tailscale requires HTTPS Serve consent.",
                    ),
                    error="Tailscale requires HTTPS Serve consent.",
                )
            if not self._poll_again(
                deadline=deadline,
                attempt=attempt,
                max_attempts=max_attempts,
            ):
                break

        if last.state is TailscaleState.READY and last.config_complete:
            command_error = self._command_error(
                mutation,
                "Tailscale did not create the requested Serve route.",
            )
            return TailscaleOperationResult(
                success=False,
                status=last,
                error=(
                    "Tailscale Serve remained unchanged after one command. "
                    f"{command_error}"
                ),
            )
        return TailscaleOperationResult(
            success=False,
            status=self._unverified(last),
            error=(
                "The Tailscale Serve command ran once, but its outcome could "
                "not be verified. Inspect Tailscale before retrying."
            ),
        )

    def apply(self, plan: TailscaleServePlan) -> TailscaleOperationResult:
        """Apply one explicitly confirmed enable plan and reconcile its result."""

        try:
            expected_target = _target_for_port(plan.port)
        except (TypeError, ValueError):
            expected_target = ""
        expected = (
            plan.binary,
            "serve",
            "--bg",
            "--yes",
            "--https=443",
            plan.target,
        )
        valid_plan = (
            plan.can_apply
            and plan.status.state is TailscaleState.READY
            and bool(plan.binary)
            and bool(plan.dns_name)
            and bool(plan.baseline_fingerprint)
            and plan.https_port == 443
            and plan.target == expected_target
            and plan.command == expected
            and plan.status.binary == plan.binary
            and plan.status.dns_name == plan.dns_name
            and plan.status.config_fingerprint == plan.baseline_fingerprint
            and plan.status.config_complete
        )
        if not valid_plan:
            return TailscaleOperationResult(
                success=False,
                status=plan.status,
                error="Refused an inapplicable or altered Tailscale plan.",
            )

        # Close the planning/apply TOCTOU window immediately before mutation.
        current = self.detect(port=plan.port, ownership=None)
        if (
            current.state is not TailscaleState.READY
            or current.binary != plan.binary
            or current.dns_name != plan.dns_name
            or current.config_fingerprint != plan.baseline_fingerprint
            or not current.config_complete
        ):
            return TailscaleOperationResult(
                success=False,
                status=current,
                error=(
                    "Tailscale changed after planning; Row-Bot did not run the "
                    "Serve command."
                ),
            )

        mutation = self._run(plan.command, timeout=self._mutation_timeout)
        return self._reconcile_enable(plan, mutation)

    def _reconcile_disable(
        self,
        *,
        ownership: TailscaleOwnership,
        binary: str,
        port: int,
        mutation: CommandResult,
    ) -> TailscaleOperationResult:
        deadline = self._clock() + self._reconciliation_timeout
        max_attempts = self._max_reconciliation_attempts()
        last = TailscaleStatus(state=TailscaleState.ERROR)
        attempt = 0
        while True:
            attempt += 1
            last = self.detect(port=port, ownership=ownership)
            if (
                last.state is TailscaleState.READY
                and last.binary == binary
                and last.config_complete
                and not last.routes
            ):
                try:
                    self._ownership_store.clear()
                except OSError:
                    return TailscaleOperationResult(
                        success=False,
                        status=last,
                        ownership=ownership,
                        error=(
                            "The route is disabled, but ownership metadata "
                            "could not be cleared."
                        ),
                    )
                return TailscaleOperationResult(success=True, status=last)

            if last.state in {
                TailscaleState.FUNNEL_ACTIVE,
                TailscaleState.ROUTE_CONFLICT,
                TailscaleState.ACTIVE_UNOWNED,
                TailscaleState.CONSENT_REQUIRED,
            }:
                return TailscaleOperationResult(
                    success=False,
                    status=last,
                    ownership=ownership,
                    error=(
                        "Tailscale changed unexpectedly during disable; "
                        "ownership was retained and no cleanup was attempted."
                    ),
                )
            if not self._poll_again(
                deadline=deadline,
                attempt=attempt,
                max_attempts=max_attempts,
            ):
                break

        if last.state is TailscaleState.ACTIVE_OWNED:
            return TailscaleOperationResult(
                success=False,
                status=last,
                ownership=ownership,
                error=(
                    "Tailscale still reports the owned route after one disable "
                    f"command. {self._command_error(mutation, 'Disable had no effect.')}"
                ),
            )
        return TailscaleOperationResult(
            success=False,
            status=self._unverified(last),
            ownership=ownership,
            error=(
                "The Tailscale disable command ran once, but route absence "
                "could not be verified. Ownership was retained."
            ),
        )

    def disable_owned(self) -> TailscaleOperationResult:
        """Disable one unchanged owned listener, then verify route absence."""

        ownership = self._ownership_store.load()
        if ownership is None:
            return TailscaleOperationResult(
                success=False,
                status=TailscaleStatus(
                    state=TailscaleState.ACTIVE_UNOWNED,
                    detail="No valid Row-Bot Tailscale ownership record exists.",
                ),
                error="Refused to change an unowned Tailscale route.",
            )
        parsed_target = urlsplit(ownership.target)
        assert parsed_target.port is not None
        port = parsed_target.port
        current = self.detect(port=port, ownership=ownership)
        if (
            current.state is not TailscaleState.ACTIVE_OWNED
            or not current.binary
            or not current.config_complete
            or current.config_fingerprint != ownership.config_fingerprint
            or len(current.routes) != 1
        ):
            return TailscaleOperationResult(
                success=False,
                status=current,
                ownership=ownership,
                error="The Tailscale configuration changed; Row-Bot will not remove it.",
            )

        command = (
            current.binary,
            "serve",
            f"--https={ownership.https_port}",
            "off",
        )
        mutation = self._run(command, timeout=self._mutation_timeout)
        return self._reconcile_disable(
            ownership=ownership,
            binary=current.binary,
            port=port,
            mutation=mutation,
        )
