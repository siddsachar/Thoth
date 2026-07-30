"""Privacy-safe operational diagnostics for remote access and server mode."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import os
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from row_bot.access.config import (
    DEFAULT_ALLOWED_HOSTS,
    AccessConfigError,
    DeploymentMode,
    canonical_host,
    canonical_origin,
    parse_trusted_proxy_cidrs,
)
from row_bot.data_paths import get_row_bot_data_dir

_SENSITIVE_KEYS = {
    "authorization",
    "code",
    "cookie",
    "credential",
    "password",
    "secret",
    "session",
    "token",
}
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "[::1]", "localhost"}
_PUBLIC_BIND_HOSTS = {"0.0.0.0", "::", "[::]"}
_KNOWN_TAILSCALE_STATES = {
    "active",
    "conflicting",
    "consent_required",
    "not_installed",
    "ready",
    "signed_out",
    "unknown",
}
_KNOWN_ROUTE_STATES = {"active", "disabled", "error", "inactive", "unknown"}


class DiagnosticStatus(StrEnum):
    PASS = "pass"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    id: str
    status: DiagnosticStatus
    message: str
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status.value,
            "message": self.message,
            "detail": redact_diagnostic_value(self.detail),
        }


@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.status is not DiagnosticStatus.ERROR for check in self.checks)

    @property
    def warning_count(self) -> int:
        return sum(check.status is DiagnosticStatus.WARNING for check in self.checks)

    @property
    def error_count(self) -> int:
        return sum(check.status is DiagnosticStatus.ERROR for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "warning_count": self.warning_count,
            "error_count": self.error_count,
            "checks": [check.to_dict() for check in self.checks],
        }


@dataclass(frozen=True, slots=True)
class DoctorContext:
    deployment_mode: str
    listen_host: str
    port: int
    public_url: str | None
    allowed_hosts: tuple[str, ...]
    trusted_proxy_cidrs: tuple[str, ...]
    data_dir: Path
    access_db_path: Path
    active_route_status: str = "unknown"
    tailscale_state: str = "unknown"
    workers: int = 1
    ephemeral_data: bool = False

    @classmethod
    def from_sources(
        cls,
        *,
        environ: Mapping[str, str] | None = None,
        deployment_mode: str | None = None,
        listen_host: str | None = None,
        port: int | None = None,
        public_url: str | None = None,
        allowed_hosts: tuple[str, ...] | None = None,
        trusted_proxy_cidrs: tuple[str, ...] | None = None,
        data_dir: str | Path | None = None,
        active_route_status: str | None = None,
        tailscale_state: str | None = None,
        workers: int | None = None,
        ephemeral_data: bool | None = None,
    ) -> DoctorContext:
        env = os.environ if environ is None else environ
        raw_data_dir = data_dir if data_dir is not None else env.get("ROW_BOT_DATA_DIR")
        selected_data_dir = (
            Path(raw_data_dir).expanduser()
            if raw_data_dir is not None
            else get_row_bot_data_dir(create=False)
        )
        selected_allowed = (
            allowed_hosts
            if allowed_hosts is not None
            else _split_csv(env.get("ROW_BOT_ALLOWED_HOSTS")) or DEFAULT_ALLOWED_HOSTS
        )
        selected_proxies = (
            trusted_proxy_cidrs
            if trusted_proxy_cidrs is not None
            else _split_csv(
                env.get("ROW_BOT_TRUSTED_PROXY_CIDRS")
                or env.get("ROW_BOT_TRUSTED_PROXIES")
            )
        )
        return cls(
            deployment_mode=(
                deployment_mode
                or env.get("ROW_BOT_DEPLOYMENT_MODE")
                or DeploymentMode.DESKTOP.value
            ),
            listen_host=listen_host or env.get("ROW_BOT_HOST") or "127.0.0.1",
            port=int(port if port is not None else env.get("ROW_BOT_PORT") or 8080),
            public_url=(
                public_url if public_url is not None else env.get("ROW_BOT_PUBLIC_URL")
            ),
            allowed_hosts=tuple(selected_allowed),
            trusted_proxy_cidrs=tuple(selected_proxies),
            data_dir=selected_data_dir,
            access_db_path=selected_data_dir / "mobile.db",
            active_route_status=_safe_state(
                active_route_status or env.get("ROW_BOT_ACTIVE_ROUTE_STATUS"),
                _KNOWN_ROUTE_STATES,
            ),
            tailscale_state=_safe_state(
                tailscale_state or env.get("ROW_BOT_TAILSCALE_STATE"),
                _KNOWN_TAILSCALE_STATES,
            ),
            workers=int(
                workers if workers is not None else env.get("ROW_BOT_WORKERS") or 1
            ),
            ephemeral_data=(
                ephemeral_data
                if ephemeral_data is not None
                else _env_bool(env.get("ROW_BOT_EPHEMERAL_DATA"), default=False)
            ),
        )


def run_access_doctor(context: DoctorContext) -> DoctorReport:
    """Evaluate local operational state without network or provider calls."""
    checks: list[DoctorCheck] = []

    try:
        mode = DeploymentMode.parse(context.deployment_mode)
    except AccessConfigError:
        mode = None
        checks.append(
            _check(
                "deployment_mode",
                DiagnosticStatus.ERROR,
                "Deployment mode is invalid.",
            )
        )
    else:
        checks.append(
            _check(
                "deployment_mode",
                DiagnosticStatus.PASS,
                f"Deployment mode is {mode.value}.",
                {"deployment_mode": mode.value},
            )
        )

    try:
        listen_host = canonical_host(context.listen_host, allow_port=False)
        listen_valid = True
    except AccessConfigError:
        listen_host = "invalid"
        listen_valid = False
        checks.append(
            _check(
                "listen",
                DiagnosticStatus.ERROR,
                "Listen host is malformed.",
            )
        )
    else:
        checks.append(
            _check(
                "listen",
                DiagnosticStatus.INFO,
                "Listen endpoint is configured.",
                {"host": listen_host, "port": context.port},
            )
        )

    normalized_public_url: str | None = None
    if context.public_url:
        try:
            normalized_public_url = canonical_origin(context.public_url)
        except AccessConfigError:
            checks.append(
                _check(
                    "public_url",
                    DiagnosticStatus.ERROR,
                    "Canonical public URL is malformed.",
                )
            )
        else:
            checks.append(
                _check(
                    "public_url",
                    DiagnosticStatus.PASS,
                    "Canonical public URL is configured.",
                    {"public_url": normalized_public_url},
                )
            )
    else:
        checks.append(
            _check(
                "public_url",
                DiagnosticStatus.INFO,
                "No canonical public URL is configured.",
            )
        )

    normalized_hosts: list[str] = []
    host_error = not context.allowed_hosts
    for raw_host in context.allowed_hosts:
        try:
            normalized_hosts.append(canonical_host(raw_host))
        except AccessConfigError:
            host_error = True
    checks.append(
        _check(
            "allowed_hosts",
            DiagnosticStatus.ERROR if host_error else DiagnosticStatus.PASS,
            (
                "One or more allowed hosts are malformed."
                if host_error
                else "Allowed hosts are valid."
            ),
            {"count": len(normalized_hosts), "hosts": normalized_hosts},
        )
    )

    try:
        trusted_proxies = parse_trusted_proxy_cidrs(context.trusted_proxy_cidrs)
    except AccessConfigError:
        checks.append(
            _check(
                "trusted_proxies",
                DiagnosticStatus.ERROR,
                "One or more trusted proxy CIDRs are malformed.",
            )
        )
    else:
        checks.append(
            _check(
                "trusted_proxies",
                DiagnosticStatus.PASS,
                "Trusted proxy configuration is valid.",
                {
                    "count": len(trusted_proxies),
                    "cidrs": [str(network) for network in trusted_proxies],
                },
            )
        )

    writable = _directory_writable(context.data_dir)
    checks.append(
        _check(
            "data_directory",
            DiagnosticStatus.PASS if writable else DiagnosticStatus.ERROR,
            (
                "Persistent data directory is writable."
                if writable
                else "Persistent data directory is not writable."
            ),
            {"path": str(context.data_dir), "ephemeral": context.ephemeral_data},
        )
    )
    checks.append(_database_check(context.access_db_path))

    checks.append(
        _check(
            "route",
            (
                DiagnosticStatus.PASS
                if context.active_route_status == "active"
                else DiagnosticStatus.WARNING
                if context.active_route_status == "error"
                else DiagnosticStatus.INFO
            ),
            f"Remote access route state is {context.active_route_status}.",
            {"state": context.active_route_status},
        )
    )
    checks.append(
        _check(
            "owner_recovery",
            DiagnosticStatus.PASS if writable else DiagnosticStatus.ERROR,
            (
                "Owner recovery is available through `row-bot access invite`."
                if writable
                else "Owner recovery is unavailable until the data directory is writable."
            ),
        )
    )

    if (
        listen_valid
        and listen_host not in _LOOPBACK_HOSTS
        and normalized_public_url is not None
        and normalized_public_url.startswith("http://")
    ):
        checks.append(
            _check(
                "insecure_http",
                DiagnosticStatus.WARNING,
                "Remote access is using unencrypted HTTP.",
            )
        )
    if (
        listen_valid
        and listen_host in _PUBLIC_BIND_HOSTS
        and normalized_public_url is None
    ):
        checks.append(
            _check(
                "public_binding",
                DiagnosticStatus.ERROR,
                "Public binding requires a canonical public URL.",
            )
        )

    tailscale_status = (
        DiagnosticStatus.WARNING
        if context.tailscale_state in {"conflicting", "consent_required"}
        else DiagnosticStatus.PASS
        if context.tailscale_state in {"active", "ready"}
        else DiagnosticStatus.INFO
    )
    checks.append(
        _check(
            "tailscale",
            tailscale_status,
            f"Tailscale Serve state is {context.tailscale_state}.",
            {"state": context.tailscale_state},
        )
    )

    checks.append(
        _check(
            "workers",
            DiagnosticStatus.PASS if context.workers == 1 else DiagnosticStatus.ERROR,
            (
                "Single-worker mode is configured."
                if context.workers == 1
                else "Row-Bot server mode supports exactly one worker."
            ),
            {"workers": context.workers},
        )
    )
    if context.ephemeral_data:
        checks.append(
            _check(
                "persistent_data",
                DiagnosticStatus.ERROR,
                "Server data is marked ephemeral; sessions will not survive replacement.",
            )
        )

    return DoctorReport(checks=tuple(checks))


def redact_diagnostic_value(value: Any, *, key: str = "") -> Any:
    """Recursively remove likely credentials from diagnostic serialization."""
    lowered_key = key.lower()
    if any(fragment in lowered_key for fragment in _SENSITIVE_KEYS):
        return "[redacted]"
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_diagnostic_value(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_diagnostic_value(item) for item in value]
    return value


def _database_check(path: Path) -> DoctorCheck:
    if not path.exists():
        return _check(
            "access_database",
            DiagnosticStatus.INFO,
            "Access database has not been created yet.",
            {"exists": False},
        )
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error:
        return _check(
            "access_database",
            DiagnosticStatus.ERROR,
            "Access database could not be opened safely.",
            {"exists": True},
        )
    healthy = bool(result and result[0] == "ok")
    return _check(
        "access_database",
        DiagnosticStatus.PASS if healthy else DiagnosticStatus.ERROR,
        (
            "Access database passed an integrity check."
            if healthy
            else "Access database integrity check failed."
        ),
        {"exists": True},
    )


def _directory_writable(path: Path) -> bool:
    existing = path
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    return existing.is_dir() and os.access(existing, os.W_OK)


def _check(
    check_id: str,
    status: DiagnosticStatus,
    message: str,
    detail: dict[str, Any] | None = None,
) -> DoctorCheck:
    return DoctorCheck(
        id=check_id,
        status=status,
        message=message,
        detail=detail or {},
    )


def _split_csv(value: str | None) -> tuple[str, ...]:
    return tuple(part.strip() for part in str(value or "").split(",") if part.strip())


def _env_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _safe_state(value: str | None, allowed: set[str]) -> str:
    normalized = str(value or "unknown").strip().lower()
    return normalized if normalized in allowed else "unknown"


__all__ = [
    "DiagnosticStatus",
    "DoctorCheck",
    "DoctorContext",
    "DoctorReport",
    "redact_diagnostic_value",
    "run_access_doctor",
]
