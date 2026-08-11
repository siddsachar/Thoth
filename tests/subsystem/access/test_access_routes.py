from __future__ import annotations

import asyncio
import json
import socket
import sys
from types import SimpleNamespace

import pytest

from row_bot.access.access_routes import (
    AccessRouteConfig,
    AccessRouteConfigError,
    AccessRouteConfigStore,
    AccessRouteKind,
    ListenMode,
    apply_listen_mode,
    build_route_inventory,
    discover_private_lan_addresses,
    resolve_listen_host,
)


def test_route_config_defaults_local_and_saves_atomically(tmp_path) -> None:
    path = tmp_path / "instance-a" / "access_routes.json"
    store = AccessRouteConfigStore(path)

    assert store.load() == AccessRouteConfig()
    saved = store.set_listen_mode(ListenMode.LOCAL_NETWORK)

    assert saved.lan_enabled is True
    assert store.load() == saved
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "listen_mode": "local_network",
        "version": 1,
    }
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []


def test_failed_atomic_replace_preserves_previous_config(tmp_path, monkeypatch) -> None:
    path = tmp_path / "access_routes.json"
    store = AccessRouteConfigStore(path)
    store.save(AccessRouteConfig(listen_mode=ListenMode.LOCAL_ONLY))

    def fail_replace(_source, _destination):
        raise OSError("injected replace failure")

    monkeypatch.setattr("row_bot.access.access_routes.os.replace", fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        store.save(AccessRouteConfig(listen_mode=ListenMode.LOCAL_NETWORK))

    assert store.load().listen_mode is ListenMode.LOCAL_ONLY
    assert list(tmp_path.glob(f".{path.name}.*.tmp")) == []


def test_malformed_config_fails_closed_with_explicit_recovery_helper(tmp_path) -> None:
    path = tmp_path / "access_routes.json"
    path.write_text('{"listen_mode":"owner_everywhere","version":1}', encoding="utf-8")
    store = AccessRouteConfigStore(path)

    with pytest.raises(AccessRouteConfigError):
        store.load()
    assert store.load_or_default() == AccessRouteConfig()


def test_explicit_and_environment_host_override_durable_lan() -> None:
    durable = AccessRouteConfig(listen_mode=ListenMode.LOCAL_NETWORK)

    explicit = resolve_listen_host(
        explicit_host="127.0.0.9",
        environ={"ROW_BOT_HOST": "127.0.0.8"},
        config=durable,
    )
    environment = resolve_listen_host(
        environ={"ROW_BOT_HOST": "127.0.0.8"},
        config=durable,
    )
    persisted = resolve_listen_host(environ={}, config=durable)
    default = resolve_listen_host(environ={}, config=AccessRouteConfig())

    assert (explicit.host, explicit.source) == ("127.0.0.9", "explicit")
    assert (environment.host, environment.source) == (
        "127.0.0.8",
        "environment",
    )
    assert (persisted.host, persisted.source) == ("0.0.0.0", "durable")
    assert (default.host, default.source) == ("127.0.0.1", "default")


def test_lan_change_persists_and_reports_no_launcher_fallback(tmp_path) -> None:
    store = AccessRouteConfigStore(tmp_path / "access_routes.json")

    result = asyncio.run(apply_listen_mode(store, ListenMode.LOCAL_NETWORK))

    assert result.changed is True
    assert result.restarted is False
    assert result.restart_required is True
    assert result.reason == "launcher_unavailable"
    assert store.load().lan_enabled is True


def test_lan_change_reports_restart_success_timeout_and_crash(tmp_path) -> None:
    async def timeout_restart():
        await asyncio.sleep(0.1)
        return True

    def crash_restart():
        raise RuntimeError("child crashed")

    success_store = AccessRouteConfigStore(tmp_path / "success.json")
    success = asyncio.run(
        apply_listen_mode(
            success_store,
            ListenMode.LOCAL_NETWORK,
            restart_child=lambda: True,
        )
    )
    timeout_store = AccessRouteConfigStore(tmp_path / "timeout.json")
    timeout = asyncio.run(
        apply_listen_mode(
            timeout_store,
            ListenMode.LOCAL_NETWORK,
            restart_child=timeout_restart,
            timeout_seconds=0.01,
        )
    )
    crash_store = AccessRouteConfigStore(tmp_path / "crash.json")
    crash = asyncio.run(
        apply_listen_mode(
            crash_store,
            ListenMode.LOCAL_NETWORK,
            restart_child=crash_restart,
        )
    )

    assert success.restarted is True
    assert success.restart_required is False
    assert timeout.reason == "restart_timeout"
    assert timeout.restart_required is True
    assert crash.reason == "restart_failed"
    assert crash.restart_required is True
    assert timeout_store.load().lan_enabled is True
    assert crash_store.load().lan_enabled is True


def test_route_inventory_is_canonical_private_first_and_separates_ngrok() -> None:
    inventory = build_route_inventory(
        port=8080,
        config=AccessRouteConfig(listen_mode=ListenMode.LOCAL_NETWORK),
        lan_addresses=(
            "192.168.1.20",
            "192.168.1.20",
            "127.0.0.1",
            "not-an-address",
        ),
        tailscale_state={
            "status": "active_owned",
            "origin": "HTTPS://ROW-BOT.TAIL.EXAMPLE:443/",
            "owned": True,
        },
        ngrok_url="https://example.ngrok-free.app/",
        reverse_proxy_origins=("https://row-bot.example/", "javascript:bad"),
    )

    tailscale = inventory.by_kind(AccessRouteKind.TAILSCALE)
    lan = inventory.by_kind(AccessRouteKind.LAN)
    ngrok = inventory.by_kind(AccessRouteKind.NGROK)
    reverse_proxy = inventory.by_kind(AccessRouteKind.REVERSE_PROXY)

    assert tailscale[0].origin == "https://row-bot.tail.example"
    assert tailscale[0].owned is True
    assert lan[0].origin == "http://192.168.1.20:8080"
    assert len(lan) == 1
    assert ngrok[0].private is False
    assert "authentication is still required" in (ngrok[0].warning or "")
    assert reverse_proxy[0].origin == "https://row-bot.example"
    assert len(reverse_proxy) == 1
    assert inventory.preferred_invitation_origin() == tailscale[0].origin


def test_discovery_includes_assigned_unicast_outside_private_ranges(
    monkeypatch,
) -> None:
    entry = SimpleNamespace
    fake_psutil = SimpleNamespace(
        net_if_addrs=lambda: {
            "Ethernet": [
                entry(family=socket.AF_INET, address="8.8.8.8"),
                entry(family=socket.AF_INET, address="8.8.8.8"),
                entry(family=socket.AF_INET, address="172.26.32.1"),
                entry(family=socket.AF_INET, address="100.64.1.10"),
            ],
            "IPv6": [
                entry(family=socket.AF_INET6, address="2001:4860::8888%7"),
                entry(family=socket.AF_INET6, address="fd00::1"),
            ],
            "Excluded": [
                entry(family=socket.AF_INET, address="127.0.0.1"),
                entry(family=socket.AF_INET, address="169.254.20.1"),
                entry(family=socket.AF_INET, address="0.0.0.0"),
                entry(family=socket.AF_INET, address="224.0.0.1"),
                entry(family=socket.AF_INET, address="not-an-address"),
                entry(family=socket.AF_INET6, address="::1"),
                entry(family=socket.AF_INET6, address="fe80::1%3"),
                entry(family=socket.AF_INET6, address="ff02::1"),
                entry(family=object(), address="8.8.4.4"),
            ],
        }
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    assert discover_private_lan_addresses() == (
        "8.8.8.8",
        "172.26.32.1",
        "100.64.1.10",
        "2001:4860::8888",
        "fd00::1",
    )


def test_non_private_interface_routes_are_exact_and_visibly_warned() -> None:
    inventory = build_route_inventory(
        port=8080,
        config=AccessRouteConfig(listen_mode=ListenMode.LOCAL_NETWORK),
        lan_addresses=(
            "8.8.8.8",
            "100.64.1.10",
            "10.0.0.7",
            "127.0.0.1",
            "169.254.1.1",
            "224.0.0.1",
            "0.0.0.0",
        ),
    )

    routes = {route.origin: route for route in inventory.by_kind(AccessRouteKind.LAN)}

    assert set(routes) == {
        "http://8.8.8.8:8080",
        "http://100.64.1.10:8080",
        "http://10.0.0.7:8080",
    }
    assert routes["http://10.0.0.7:8080"].private is True
    assert "LAN HTTP is unencrypted" in (routes["http://10.0.0.7:8080"].warning or "")
    for origin in ("http://8.8.8.8:8080", "http://100.64.1.10:8080"):
        assert routes[origin].private is False
        assert "outside private IP ranges" in (routes[origin].warning or "")
        assert "verify firewall exposure" in (routes[origin].warning or "")


def test_two_instances_keep_independent_listen_state_and_ports(tmp_path) -> None:
    first_store = AccessRouteConfigStore(tmp_path / "first" / "routes.json")
    second_store = AccessRouteConfigStore(tmp_path / "second" / "routes.json")
    first_store.set_listen_mode(ListenMode.LOCAL_NETWORK)
    second_store.set_listen_mode(ListenMode.LOCAL_ONLY)

    first = build_route_inventory(
        port=8081,
        config=first_store.load(),
        lan_addresses=("10.0.0.20",),
    )
    second = build_route_inventory(
        port=8082,
        config=second_store.load(),
        lan_addresses=("10.0.0.20",),
    )

    assert first.by_kind(AccessRouteKind.LAN)[0].available is True
    assert first.by_kind(AccessRouteKind.LAN)[0].origin.endswith(":8081")
    assert second.by_kind(AccessRouteKind.LAN)[0].available is False
    assert second.by_kind(AccessRouteKind.LAN)[0].origin.endswith(":8082")


def test_lan_routes_have_stable_ids_unique_labels_and_deterministic_order() -> None:
    config = AccessRouteConfig(listen_mode=ListenMode.LOCAL_NETWORK)
    first = build_route_inventory(
        port=8080,
        config=config,
        lan_addresses=("172.20.0.2", "192.168.1.20", "10.0.0.7"),
    )
    reversed_discovery = build_route_inventory(
        port=8080,
        config=config,
        lan_addresses=("10.0.0.7", "192.168.1.20", "172.20.0.2"),
    )

    first_lan = first.by_kind(AccessRouteKind.LAN)
    second_lan = reversed_discovery.by_kind(AccessRouteKind.LAN)
    assert [(route.id, route.origin) for route in first_lan] == [
        (route.id, route.origin) for route in second_lan
    ]
    assert len({route.id for route in first_lan}) == 3
    assert len({route.label for route in first_lan}) == 3
    assert first.preferred_invitation_route() is None
    assert first.preferred_invitation_origin() is None


def test_canonical_origins_are_deduplicated_globally_by_kind_precedence() -> None:
    inventory = build_route_inventory(
        port=8080,
        tailscale_state={
            "status": "active_owned",
            "origin": "HTTPS://ROW-BOT.EXAMPLE:443/",
            "owned": True,
        },
        reverse_proxy_origins=("https://row-bot.example/",),
        ngrok_url="https://row-bot.example",
        current_server_origin="https://row-bot.example",
    )

    matching = [
        route for route in inventory.routes if route.origin == "https://row-bot.example"
    ]
    assert len(matching) == 1
    assert matching[0].kind is AccessRouteKind.TAILSCALE


def test_lan_ipv4_and_ipv6_canonicalize_and_duplicate_addresses_collapse() -> None:
    inventory = build_route_inventory(
        port=8080,
        config=AccessRouteConfig(listen_mode=ListenMode.LOCAL_NETWORK),
        lan_addresses=(
            "192.168.1.20",
            "192.168.1.20",
            "fd00:0:0:0:0:0:0:1234",
            "fd00::1234",
        ),
    )

    lan = inventory.by_kind(AccessRouteKind.LAN)
    assert [route.origin for route in lan] == [
        "http://192.168.1.20:8080",
        "http://[fd00::1234]:8080",
    ]
    assert {route.label for route in lan} == {
        "Local network — 192.168.1.20",
        "Local network — [fd00::1234]",
    }


def test_configured_https_route_outranks_lan_without_arbitrary_lan_default() -> None:
    inventory = build_route_inventory(
        port=8080,
        config=AccessRouteConfig(listen_mode=ListenMode.LOCAL_NETWORK),
        lan_addresses=("10.0.0.7", "192.168.1.20"),
        reverse_proxy_origins=("https://row-bot.example",),
    )

    preferred = inventory.preferred_invitation_route()
    assert preferred is not None
    assert preferred.kind is AccessRouteKind.REVERSE_PROXY
    assert preferred.origin == "https://row-bot.example"


def test_localhost_is_diagnostic_only_and_verified_current_server_is_selectable() -> (
    None
):
    desktop = build_route_inventory(port=8080)
    server = build_route_inventory(
        port=8080,
        current_server_origin="HTTPS://SERVER.EXAMPLE:443/",
    )

    assert desktop.available
    assert desktop.invitation_routes == ()
    assert desktop.preferred_invitation_origin() is None
    current = server.by_kind(AccessRouteKind.CURRENT_SERVER)
    assert len(current) == 1
    assert current[0].label == "Current server address — server.example"
    assert server.resolve_invitation_route(current[0].id) == current[0]


def test_route_labels_never_include_credentials_queries_tokens_or_paths() -> None:
    inventory = build_route_inventory(
        port=8080,
        reverse_proxy_origins=(
            "https://user:secret@example.com",
            "https://example.com/path",
            "https://example.com?token=secret",
            "https://safe.example",
        ),
    )

    labels = " ".join(route.label for route in inventory.routes)
    assert "secret" not in labels
    assert "token" not in labels
    assert "/path" not in labels
    assert "safe.example" in labels
