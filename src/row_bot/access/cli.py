"""Parser and dispatch helpers for ``row-bot serve`` and ``row-bot access``."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from typing import Any, TextIO
from urllib.parse import urlsplit

from row_bot.access.config import (
    DEFAULT_ALLOWED_HOSTS,
    AccessConfig,
    DeploymentMode,
    canonical_host,
    canonical_origin,
)
from row_bot.access.diagnostics import DoctorContext, DoctorReport, run_access_doctor
from row_bot.access.models import AccessInvitation, AccessProfile, SessionLifetime
from row_bot.access.service import AccessService
from row_bot.access.store import AccessStore, AccessStoreError, normalize_datetime
from row_bot.data_paths import get_row_bot_data_dir


@dataclass(frozen=True, slots=True)
class ServeOptions:
    """Resolved, side-effect-free server launch options."""

    deployment_mode: DeploymentMode
    host: str
    port: int
    public_url: str | None
    allowed_hosts: tuple[str, ...]
    trusted_proxy_cidrs: tuple[str, ...]
    data_dir: Path
    auto_start_ollama: bool
    workers: int
    open_browser: bool = False
    tray: bool = False
    splash: bool = False
    legacy_compatibility: bool = False

    def to_environment(self) -> dict[str, str]:
        """Return explicit child-process settings without credentials."""
        values = {
            "ROW_BOT_DEPLOYMENT_MODE": DeploymentMode.SERVER.value,
            "ROW_BOT_HOST": self.host,
            "ROW_BOT_PORT": str(self.port),
            "ROW_BOT_ALLOWED_HOSTS": ",".join(self.allowed_hosts),
            "ROW_BOT_TRUSTED_PROXY_CIDRS": ",".join(
                self.trusted_proxy_cidrs
            ),
            "ROW_BOT_DATA_DIR": str(self.data_dir),
            "ROW_BOT_AUTO_START_OLLAMA": (
                "1" if self.auto_start_ollama else "0"
            ),
            "ROW_BOT_NO_OPEN": "1",
            "ROW_BOT_DISABLE_TRAY": "1",
            "ROW_BOT_DISABLE_SPLASH": "1",
            "ROW_BOT_WORKERS": "1",
        }
        if self.public_url:
            values["ROW_BOT_PUBLIC_URL"] = self.public_url
        return values


def add_remote_access_subcommands(subparsers: Any) -> tuple[argparse.ArgumentParser, argparse.ArgumentParser]:
    """Register ``serve`` and ``access`` on a launcher-owned subparser set."""
    return add_serve_parser(subparsers), add_access_parser(subparsers)


def add_serve_parser(subparsers: Any) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "serve",
        help="Run Row-Bot as an authenticated headless server",
        description=(
            "Run Row-Bot without a browser, tray, splash, or automatic Ollama "
            "startup. Every browser must authenticate, including loopback."
        ),
    )
    configure_serve_parser(parser)
    return parser


def configure_serve_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", help="Listen host (safe default: 127.0.0.1)")
    parser.add_argument("--port", type=int, help="Listen port (default: 8080)")
    parser.add_argument("--public-url", help="Canonical public HTTP(S) origin")
    parser.add_argument(
        "--allowed-host",
        action="append",
        dest="allowed_hosts",
        help="Allowed Host authority; repeat for multiple values",
    )
    parser.add_argument(
        "--trusted-proxy",
        action="append",
        dest="trusted_proxy_cidrs",
        help="Trusted reverse-proxy CIDR; repeat for multiple values",
    )
    parser.add_argument("--data-dir", help="Persistent Row-Bot data directory")
    parser.add_argument(
        "--auto-start-ollama",
        action="store_true",
        default=None,
        help="Explicitly opt in to local Ollama auto-start",
    )
    parser.add_argument(
        "--workers",
        type=int,
        help="Worker count; Row-Bot requires exactly one",
    )


def add_access_parser(subparsers: Any) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "access",
        help="Create and manage authenticated device access",
        description="Manage Row-Bot invitations, devices, sessions, and diagnostics.",
    )
    parser.add_argument("--data-dir", help=argparse.SUPPRESS)
    command_parsers = parser.add_subparsers(dest="access_command", required=True)

    invite = command_parsers.add_parser(
        "invite",
        help="Create a one-time device invitation",
    )
    _add_access_common(invite)
    invite.add_argument(
        "--profile",
        choices=("computer", "companion"),
        required=True,
    )
    invite.add_argument("--origin", required=True, help="Canonical Row-Bot origin")
    lifetime = invite.add_mutually_exclusive_group()
    lifetime.add_argument(
        "--temporary",
        action="store_true",
        help="Keep the resulting session for 12 hours",
    )
    lifetime.add_argument(
        "--lifetime",
        choices=("trusted", "temporary"),
        help="Session lifetime preset (default: trusted)",
    )
    invite.add_argument("--name", default="Connected device", help="Invitation label")

    list_parser = command_parsers.add_parser(
        "list",
        help="List devices, sessions, and recent invitations",
    )
    _add_access_common(list_parser)
    list_parser.add_argument(
        "--active-only",
        action="store_true",
        help="Hide revoked devices and sessions",
    )

    revoke = command_parsers.add_parser("revoke", help="Revoke a device or session")
    _add_access_common(revoke)
    revoke.add_argument("target", help="Device ID (or session ID with --session)")
    revoke.add_argument(
        "--session",
        action="store_true",
        help="Interpret target as a session ID",
    )

    revoke_all = command_parsers.add_parser(
        "revoke-all",
        help="Revoke every active session",
    )
    _add_access_common(revoke_all)
    revoke_all.add_argument(
        "--yes",
        action="store_true",
        help="Confirm revocation of every active session",
    )

    doctor = command_parsers.add_parser(
        "doctor",
        help="Check server and remote-access safety",
    )
    _add_access_common(doctor)
    doctor.add_argument(
        "--deployment-mode",
        choices=("desktop", "server"),
    )
    doctor.add_argument("--host")
    doctor.add_argument("--port", type=int)
    doctor.add_argument("--public-url")
    doctor.add_argument("--allowed-host", action="append", dest="allowed_hosts")
    doctor.add_argument(
        "--trusted-proxy",
        action="append",
        dest="trusted_proxy_cidrs",
    )
    doctor.add_argument("--workers", type=int)
    doctor.add_argument("--ephemeral-data", action="store_true", default=None)
    doctor.add_argument(
        "--route-status",
        choices=("active", "disabled", "error", "inactive", "unknown"),
    )
    doctor.add_argument(
        "--tailscale-state",
        choices=(
            "active",
            "conflicting",
            "consent_required",
            "not_installed",
            "ready",
            "signed_out",
            "unknown",
        ),
    )
    return parser


def _add_access_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-dir", help="Persistent Row-Bot data directory")
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit machine-readable JSON",
    )


def build_remote_access_parser(*, prog: str = "row-bot") -> argparse.ArgumentParser:
    """Build a standalone parser useful to launcher integration and tests."""
    parser = argparse.ArgumentParser(prog=prog)
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_remote_access_subcommands(subparsers)
    return parser


def resolve_serve_options(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str] | None = None,
    durable: Mapping[str, Any] | None = None,
    legacy_compatibility: bool = False,
) -> ServeOptions:
    """Resolve CLI > environment > durable config > safe defaults."""
    env = os.environ if environ is None else environ
    saved = durable or {}

    host = str(
        _first(
            getattr(args, "host", None),
            env.get("ROW_BOT_HOST"),
            saved.get("host"),
            "127.0.0.1",
        )
    )
    normalized_host = canonical_host(host, allow_port=False)
    port = int(
        _first(
            getattr(args, "port", None),
            env.get("ROW_BOT_PORT"),
            saved.get("port"),
            8080,
        )
    )
    if not 1 <= port <= 65535:
        raise ValueError("serve port must be within 1..65535")

    raw_public_url = _first(
        getattr(args, "public_url", None),
        env.get("ROW_BOT_PUBLIC_URL"),
        saved.get("public_url"),
        None,
    )
    public_url = canonical_origin(raw_public_url) if raw_public_url else None

    raw_allowed_hosts = _first(
        getattr(args, "allowed_hosts", None),
        _split_csv(env.get("ROW_BOT_ALLOWED_HOSTS")),
        saved.get("allowed_hosts"),
        None,
    )
    if raw_allowed_hosts:
        allowed_hosts = tuple(str(item) for item in raw_allowed_hosts)
    else:
        allowed_hosts = DEFAULT_ALLOWED_HOSTS
        if public_url:
            public_authority = urlsplit(public_url).netloc
            allowed_hosts = (*allowed_hosts, public_authority)

    raw_trusted_proxies = _first(
        getattr(args, "trusted_proxy_cidrs", None),
        _split_csv(
            env.get("ROW_BOT_TRUSTED_PROXY_CIDRS")
            or env.get("ROW_BOT_TRUSTED_PROXIES")
        ),
        saved.get("trusted_proxy_cidrs"),
        (),
    )
    trusted_proxies = tuple(str(item) for item in (raw_trusted_proxies or ()))
    validated = AccessConfig.build(
        deployment_mode=DeploymentMode.SERVER,
        allowed_hosts=allowed_hosts,
        public_origins=(public_url,) if public_url else (),
        trusted_proxy_cidrs=trusted_proxies,
    )

    raw_data_dir = _first(
        getattr(args, "data_dir", None),
        env.get("ROW_BOT_DATA_DIR"),
        saved.get("data_dir"),
        None,
    )
    data_dir = (
        Path(str(raw_data_dir)).expanduser()
        if raw_data_dir
        else get_row_bot_data_dir(create=False)
    )
    auto_start_ollama = _resolve_bool(
        getattr(args, "auto_start_ollama", None),
        env.get("ROW_BOT_AUTO_START_OLLAMA"),
        saved.get("auto_start_ollama"),
        default=False,
    )
    workers = int(
        _first(
            getattr(args, "workers", None),
            env.get("ROW_BOT_WORKERS"),
            saved.get("workers"),
            1,
        )
    )
    if workers != 1:
        raise ValueError("Row-Bot server mode requires exactly one worker")

    return ServeOptions(
        deployment_mode=DeploymentMode.SERVER,
        host=normalized_host,
        port=port,
        public_url=public_url,
        allowed_hosts=validated.allowed_hosts,
        trusted_proxy_cidrs=tuple(str(item) for item in validated.trusted_proxy_cidrs),
        data_dir=data_dir,
        auto_start_ollama=auto_start_ollama,
        workers=workers,
        legacy_compatibility=legacy_compatibility,
    )


def legacy_serve_requested(args: argparse.Namespace) -> bool:
    """Recognize the supported legacy ``--server --no-open`` combination."""
    return bool(
        getattr(args, "server", False)
        and getattr(args, "no_open", False)
    )


def legacy_serve_warning() -> str:
    return (
        "`--server --no-open` is deprecated; use `row-bot serve`. "
        "Compatibility mode still requires authenticated browser sessions."
    )


def serve_startup_lines(options: ServeOptions) -> tuple[str, ...]:
    """Return concise, secret-free operator startup guidance."""
    endpoint = f"http://{options.host}:{options.port}"
    lines = [
        f"Row-Bot server mode listening on {endpoint}",
        f"Persistent data: {options.data_dir}",
        "Browser authentication is required, including from loopback.",
        "Health: /healthz  Readiness: /readyz",
    ]
    if options.public_url:
        lines.append(
            "Create the first owner invitation with: "
            f"row-bot access invite --profile computer --origin {options.public_url}"
        )
    else:
        lines.append(
            "Configure a canonical public URL, then create the first owner "
            "invitation with `row-bot access invite`."
        )
    if options.legacy_compatibility:
        lines.append(legacy_serve_warning())
    return tuple(lines)


def dispatch_access_command(
    args: argparse.Namespace,
    *,
    service: AccessService | None = None,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    now: datetime | None = None,
) -> int:
    """Execute an access subcommand without importing or starting NiceGUI."""
    output = stdout or sys.stdout
    error_output = stderr or sys.stderr
    command = str(getattr(args, "access_command", "") or "")
    json_output = bool(getattr(args, "json_output", False))

    try:
        if command == "doctor":
            report = _doctor_from_args(args, environ=environ)
            _write_doctor(report, json_output=json_output, output=output)
            return 0 if report.ok else 1

        selected_service = service or _service_from_args(args)
        if command == "invite":
            return _dispatch_invite(
                args,
                selected_service,
                output=output,
                json_output=json_output,
                now=now,
            )
        if command == "list":
            return _dispatch_list(
                args,
                selected_service,
                output=output,
                json_output=json_output,
                now=now,
            )
        if command == "revoke":
            return _dispatch_revoke(
                args,
                selected_service,
                output=output,
                json_output=json_output,
                now=now,
            )
        if command == "revoke-all":
            return _dispatch_revoke_all(
                args,
                selected_service,
                output=output,
                error_output=error_output,
                json_output=json_output,
                now=now,
            )
        raise ValueError("an access subcommand is required")
    except (AccessStoreError, OSError, ValueError):
        payload = {
            "ok": False,
            "error": "access_command_failed",
            "detail": "The access command could not be completed safely.",
        }
        _write_payload(
            payload,
            json_output=json_output,
            output=error_output,
            text="Access command failed. Check the command options and data directory.",
        )
        return 2


def _dispatch_invite(
    args: argparse.Namespace,
    service: AccessService,
    *,
    output: TextIO,
    json_output: bool,
    now: datetime | None,
) -> int:
    external_profile = str(args.profile)
    profile = (
        AccessProfile.OWNER
        if external_profile == "computer"
        else AccessProfile.COMPANION
    )
    lifetime = (
        SessionLifetime.TEMPORARY
        if getattr(args, "temporary", False)
        or getattr(args, "lifetime", None) == "temporary"
        else SessionLifetime.TRUSTED
    )
    created = service.create_invitation(
        profile=profile,
        intended_origin=args.origin,
        session_lifetime=lifetime,
        created_by="local_operator",
        access_route="cli",
        now=now,
    )
    invitation_url = created.invitation_url()
    payload = {
        "ok": True,
        "invitation": {
            "id": created.invitation.id,
            "name": str(args.name)[:80],
            "profile": external_profile,
            "access_profile": created.invitation.profile.value,
            "session_lifetime": created.invitation.session_lifetime.value,
            "origin": created.invitation.intended_origin,
            "expires_at": created.invitation.expires_at.isoformat(),
            # Deliberate one-time secret output. It is not emitted elsewhere.
            "invitation_url": invitation_url,
        },
    }
    text = "\n".join(
        (
            f"Invitation: {args.name}",
            f"Profile: {external_profile}",
            f"Session: {created.invitation.session_lifetime.value}",
            f"Origin: {created.invitation.intended_origin}",
            f"Expires: {created.invitation.expires_at.isoformat()}",
            invitation_url,
        )
    )
    _write_payload(payload, json_output=json_output, output=output, text=text)
    return 0


def _dispatch_list(
    args: argparse.Namespace,
    service: AccessService,
    *,
    output: TextIO,
    json_output: bool,
    now: datetime | None,
) -> int:
    include_revoked = not bool(getattr(args, "active_only", False))
    current = normalize_datetime(now)
    devices = [
        device.to_public_dict()
        for device in service.list_devices(include_revoked=include_revoked)
    ]
    sessions = [
        session.to_public_dict()
        for session in service.list_sessions(include_revoked=include_revoked)
    ]
    invitations = [
        {
            **invitation.to_public_dict(),
            "status": _invitation_status(invitation, current),
        }
        for invitation in service.list_invitations()
    ]
    payload = {
        "ok": True,
        "devices": devices,
        "sessions": sessions,
        "invitations": invitations,
    }
    lines = [
        f"Devices: {len(devices)}",
        *[
            f"  {device['id']}  {device['profile']}  "
            f"{'revoked' if device['revoked_at'] else 'active'}  "
            f"{device['display_name']}"
            for device in devices
        ],
        f"Sessions: {len(sessions)}",
        *[
            f"  {session['id']}  device={session['device_id']}  "
            f"{'revoked' if session['revoked_at'] else 'active'}  "
            f"expires={session['expires_at']}"
            for session in sessions
        ],
        f"Invitations: {len(invitations)}",
        *[
            f"  {invitation['id']}  {invitation['profile']}  "
            f"{invitation['status']}  expires={invitation['expires_at']}"
            for invitation in invitations
        ],
    ]
    _write_payload(
        payload,
        json_output=json_output,
        output=output,
        text="\n".join(lines),
    )
    return 0


def _dispatch_revoke(
    args: argparse.Namespace,
    service: AccessService,
    *,
    output: TextIO,
    json_output: bool,
    now: datetime | None,
) -> int:
    target = str(args.target)
    target_type = "session" if args.session else "device"
    revoked = (
        service.revoke_session(target, now=now)
        if args.session
        else service.revoke_device(target, now=now)
    )
    payload = {
        "ok": revoked,
        "revoked": revoked,
        "target_type": target_type,
        "target_id": target,
    }
    _write_payload(
        payload,
        json_output=json_output,
        output=output,
        text=(
            f"Revoked {target_type} {target}."
            if revoked
            else f"No active {target_type} matched {target}."
        ),
    )
    return 0 if revoked else 1


def _dispatch_revoke_all(
    args: argparse.Namespace,
    service: AccessService,
    *,
    output: TextIO,
    error_output: TextIO,
    json_output: bool,
    now: datetime | None,
) -> int:
    if not args.yes:
        _write_payload(
            {
                "ok": False,
                "error": "confirmation_required",
                "detail": "Pass --yes to revoke every active session.",
            },
            json_output=json_output,
            output=error_output,
            text="Refusing to revoke every session without --yes.",
        )
        return 2
    count = service.revoke_all(now=now)
    _write_payload(
        {"ok": True, "revoked_sessions": count},
        json_output=json_output,
        output=output,
        text=f"Revoked {count} active session(s).",
    )
    return 0


def _doctor_from_args(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str] | None,
) -> DoctorReport:
    context = DoctorContext.from_sources(
        environ=environ,
        deployment_mode=getattr(args, "deployment_mode", None),
        listen_host=getattr(args, "host", None),
        port=getattr(args, "port", None),
        public_url=getattr(args, "public_url", None),
        allowed_hosts=(
            tuple(args.allowed_hosts)
            if getattr(args, "allowed_hosts", None) is not None
            else None
        ),
        trusted_proxy_cidrs=(
            tuple(args.trusted_proxy_cidrs)
            if getattr(args, "trusted_proxy_cidrs", None) is not None
            else None
        ),
        data_dir=getattr(args, "data_dir", None),
        active_route_status=getattr(args, "route_status", None),
        tailscale_state=getattr(args, "tailscale_state", None),
        workers=getattr(args, "workers", None),
        ephemeral_data=getattr(args, "ephemeral_data", None),
    )
    return run_access_doctor(context)


def _write_doctor(
    report: DoctorReport,
    *,
    json_output: bool,
    output: TextIO,
) -> None:
    lines = [
        f"[{check.status.value.upper()}] {check.message}"
        for check in report.checks
    ]
    lines.append(
        f"Summary: {report.error_count} error(s), "
        f"{report.warning_count} warning(s)"
    )
    _write_payload(
        report.to_dict(),
        json_output=json_output,
        output=output,
        text="\n".join(lines),
    )


def _service_from_args(args: argparse.Namespace) -> AccessService:
    data_dir = getattr(args, "data_dir", None)
    if data_dir:
        return AccessService(AccessStore(Path(data_dir).expanduser() / "mobile.db"))
    return AccessService()


def _write_payload(
    payload: Mapping[str, Any],
    *,
    json_output: bool,
    output: TextIO,
    text: str,
) -> None:
    output.write(
        f"{json.dumps(payload, sort_keys=True)}\n"
        if json_output
        else f"{text}\n"
    )


def _invitation_status(invitation: AccessInvitation, now: datetime) -> str:
    if invitation.claimed_at:
        return "claimed"
    if invitation.cancelled_at:
        return "cancelled"
    if invitation.expires_at <= now:
        return "expired"
    if invitation.locked_until and invitation.locked_until > now:
        return "locked"
    return "available"


def _first(*values: Any) -> Any:
    for value in values:
        if value is not None and value != () and value != [] and value != "":
            return value
    return None


def _split_csv(value: str | None) -> tuple[str, ...]:
    return tuple(part.strip() for part in str(value or "").split(",") if part.strip())


def _resolve_bool(*values: Any, default: bool) -> bool:
    selected = _first(*values)
    if selected is None:
        return default
    if isinstance(selected, bool):
        return selected
    normalized = str(selected).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("boolean setting is malformed")


__all__ = [
    "ServeOptions",
    "add_access_parser",
    "add_remote_access_subcommands",
    "add_serve_parser",
    "build_remote_access_parser",
    "configure_serve_parser",
    "dispatch_access_command",
    "legacy_serve_requested",
    "legacy_serve_warning",
    "resolve_serve_options",
    "serve_startup_lines",
]
