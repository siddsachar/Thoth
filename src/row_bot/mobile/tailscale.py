"""Optional Tailscale detection for Row-Bot mobile access."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import shutil
import subprocess
from typing import Any, Callable

from row_bot.access.tailscale import (
    CommandResult as AccessCommandResult,
    TailscaleServeController,
    TailscaleState as AccessTailscaleState,
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class TailscaleState:
    installed: bool = False
    logged_in: bool = False
    tailnet_name: str = ""
    device_name: str = ""
    tailscale_ips: tuple[str, ...] = ()
    magicdns_url_candidates: tuple[str, ...] = ()
    serve_enabled: bool = False
    serve_url: str = ""
    funnel_enabled: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "installed": self.installed,
            "logged_in": self.logged_in,
            "tailnet_name": self.tailnet_name,
            "device_name": self.device_name,
            "tailscale_ips": list(self.tailscale_ips),
            "magicdns_url_candidates": list(self.magicdns_url_candidates),
            "serve_enabled": self.serve_enabled,
            "serve_url": self.serve_url,
            "funnel_enabled": self.funnel_enabled,
            "error": self.error,
        }


Runner = Callable[[list[str]], CommandResult]


def _run(argv: list[str], *, timeout: float) -> CommandResult:
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return CommandResult(
        completed.returncode, completed.stdout or "", completed.stderr or ""
    )


def parse_tailscale_ips(output: str) -> tuple[str, ...]:
    ips: list[str] = []
    for line in str(output or "").splitlines():
        text = line.strip()
        if text and text not in ips:
            ips.append(text)
    return tuple(ips)


def parse_tailscale_status_json(
    payload: str | dict[str, Any], *, port: int | None = None
) -> TailscaleState:
    try:
        data = json.loads(payload) if isinstance(payload, str) else dict(payload)
    except Exception as exc:
        return TailscaleState(
            installed=True, error=f"Could not parse tailscale status: {exc}"
        )

    self_info = data.get("Self") if isinstance(data.get("Self"), dict) else {}
    backend_state = str(data.get("BackendState") or "")
    logged_in = bool(self_info) and backend_state.lower() not in {
        "stopped",
        "needslogin",
        "no state",
    }
    tailnet = (
        data.get("CurrentTailnet")
        if isinstance(data.get("CurrentTailnet"), dict)
        else {}
    )
    tailnet_name = str(tailnet.get("Name") or tailnet.get("MagicDNSSuffix") or "")
    device_name = str(
        self_info.get("HostName") or self_info.get("DNSName") or ""
    ).strip(".")
    ips = tuple(
        str(ip) for ip in self_info.get("TailscaleIPs") or [] if str(ip).strip()
    )
    dns_name = str(self_info.get("DNSName") or "").strip(".")
    magicdns: list[str] = []
    if dns_name:
        if port:
            magicdns.append(f"http://{dns_name}:{port}")
        else:
            magicdns.append(f"http://{dns_name}")
    return TailscaleState(
        installed=True,
        logged_in=logged_in,
        tailnet_name=tailnet_name,
        device_name=device_name,
        tailscale_ips=ips,
        magicdns_url_candidates=tuple(magicdns),
    )


def parse_tailscale_serve_status(
    output: str | dict[str, Any],
) -> tuple[bool, str, bool]:
    text = json.dumps(output) if isinstance(output, dict) else str(output or "")
    urls = re.findall(r"https://[A-Za-z0-9_.-]+(?:/[^\s\"']*)?", text)
    funnel_enabled = "funnel" in text.lower()
    return (bool(urls), urls[0] if urls else "", funnel_enabled)


def detect_tailscale(
    *,
    port: int | None = None,
    timeout: float = 2.0,
    runner: Runner | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> TailscaleState:
    """Compatibility view backed by the canonical access controller."""
    legacy_runner = runner or (lambda argv: _run(argv, timeout=timeout))

    def access_runner(argv, _timeout: float) -> AccessCommandResult:
        result = legacy_runner(list(argv))
        return AccessCommandResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    controller = TailscaleServeController(
        runner=access_runner,
        which=which,
        timeout=timeout,
    )
    status = controller.detect(port=port or 8080)
    installed = status.state is not AccessTailscaleState.CLI_NOT_FOUND
    logged_in = status.state not in {
        AccessTailscaleState.CLI_NOT_FOUND,
        AccessTailscaleState.SIGNED_OUT,
        AccessTailscaleState.DAEMON_UNAVAILABLE,
    }
    ips: tuple[str, ...] = ()
    if installed and status.binary:
        try:
            ip_result = legacy_runner([status.binary, "ip", "-4"])
            if ip_result.returncode == 0:
                ips = parse_tailscale_ips(ip_result.stdout)
        except Exception:
            pass
    dns_name = status.dns_name.strip(".")
    tailnet_name = dns_name.split(".", 1)[1] if "." in dns_name else ""
    active_states = {
        AccessTailscaleState.ACTIVE_OWNED,
        AccessTailscaleState.ACTIVE_UNOWNED,
    }
    return TailscaleState(
        installed=installed,
        logged_in=logged_in,
        tailnet_name=tailnet_name,
        device_name=dns_name.split(".", 1)[0] if dns_name else "",
        tailscale_ips=ips,
        magicdns_url_candidates=(
            (f"http://{dns_name}:{port}",)
            if dns_name and port
            else (f"http://{dns_name}",)
            if dns_name
            else ()
        ),
        serve_enabled=status.state in active_states,
        serve_url=status.serve_url,
        funnel_enabled=status.state is AccessTailscaleState.FUNNEL_ACTIVE,
        error=status.detail
        if status.state
        in {
            AccessTailscaleState.DAEMON_UNAVAILABLE,
            AccessTailscaleState.ERROR,
            AccessTailscaleState.UNSUPPORTED_CLI,
        }
        else "",
    )
