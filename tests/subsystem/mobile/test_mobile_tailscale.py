from __future__ import annotations

import json

from row_bot.mobile.tailscale import (
    CommandResult,
    detect_tailscale,
    parse_tailscale_ips,
    parse_tailscale_status_json,
)


def test_parse_tailscale_status_json_logged_in_magicdns() -> None:
    payload = {
        "BackendState": "Running",
        "CurrentTailnet": {"Name": "example.ts.net"},
        "Self": {
            "HostName": "desktop",
            "DNSName": "desktop.example.ts.net.",
            "TailscaleIPs": ["100.64.0.10"],
        },
    }

    state = parse_tailscale_status_json(payload, port=8765)

    assert state.installed is True
    assert state.logged_in is True
    assert state.tailnet_name == "example.ts.net"
    assert state.device_name == "desktop"
    assert state.tailscale_ips == ("100.64.0.10",)
    assert state.magicdns_url_candidates == ("http://desktop.example.ts.net:8765",)


def test_parse_tailscale_ips_deduplicates_lines() -> None:
    assert parse_tailscale_ips("100.64.0.10\n100.64.0.10\n100.64.0.11\n") == (
        "100.64.0.10",
        "100.64.0.11",
    )


def test_detect_tailscale_not_installed_does_not_run_commands() -> None:
    calls: list[list[str]] = []

    state = detect_tailscale(
        which=lambda _name: None,
        runner=lambda argv: calls.append(argv) or CommandResult(0),
    )

    assert state.installed is False
    assert calls == []


def test_detect_tailscale_uses_argv_runner_and_parses_serve() -> None:
    status = {
        "BackendState": "Running",
        "CurrentTailnet": {"Name": "example.ts.net"},
        "Self": {
            "HostName": "desktop",
            "DNSName": "desktop.example.ts.net.",
            "TailscaleIPs": [],
        },
    }
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> CommandResult:
        calls.append(argv)
        if argv[1:] == ["status", "--json"]:
            return CommandResult(0, json.dumps(status))
        if argv[1:] == ["ip", "-4"]:
            return CommandResult(0, "100.64.0.10\n")
        if argv[1:] == ["serve", "status", "--json"]:
            return CommandResult(
                0,
                json.dumps(
                    {
                        "TCP": {"443": {"HTTPS": True}},
                        "Web": {
                            "desktop.example.ts.net:443": {
                                "Handlers": {"/": {"Proxy": "http://127.0.0.1:8765"}}
                            }
                        },
                        "AllowFunnel": {"desktop.example.ts.net:443": False},
                    }
                ),
            )
        raise AssertionError(argv)

    state = detect_tailscale(port=8765, which=lambda _name: "tailscale", runner=runner)

    assert calls == [
        ["tailscale", "status", "--json"],
        ["tailscale", "serve", "status", "--json"],
        ["tailscale", "ip", "-4"],
    ]
    assert state.tailscale_ips == ("100.64.0.10",)
    assert state.serve_enabled is True
    assert state.serve_url == "https://desktop.example.ts.net"
